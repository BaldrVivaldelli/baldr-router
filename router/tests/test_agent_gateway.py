from __future__ import annotations

import json
import subprocess
import threading
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
import baldr_router.agent_gateway as agent_gateway_module
import baldr_router.provider_registry as provider_registry_module

from baldr_router.agent_api import (
    AgentContractError,
    AgentDigestMismatchError,
    AgentInvocation,
    AgentManifest,
    AgentRef,
    AgentResolutionContext,
    ResolvedAgent,
)
from baldr_router.agent_gateway import (
    AgentGateway,
    AgentPolicyError,
    ProviderAgentConnector,
    reset_agent_gateway,
    external_agent_catalog_status,
)
from baldr_router.agent_registry import (
    LocalAgentRegistry,
    agent_registry_status,
    registry_document,
)
from baldr_router.agent_http import HttpJsonAgentConnector, JsonHttpClient
from baldr_router.provider_api import ProviderCapabilities
from baldr_router.provider_registry import ProviderRegistry, run_provider_role
from baldr_router.config import (
    AppConfig,
    ExecutionProfileConfig,
    RoleConfig,
    load_config,
    save_config,
)
from baldr_router.durability.engine import (
    DurableWorkflowEngine,
    SimulatedProcessCrash,
    _resolved_snapshot,
)
from baldr_router.durability.recovery import recover_stale_runs
from baldr_router.durability.store import DurableStore
from baldr_router.workflows import run_workflow_impl


class KiroFixtureProvider:
    name = "kiro-cli"
    aliases = ("kiro",)
    capabilities = ProviderCapabilities(
        supports_read_only=True,
        supports_workspace_write=True,
        supports_structured_output=True,
        supports_sessions=False,
        read_only_enforcement="advisory",
        write_enforcement="advisory",
    )

    def __init__(self) -> None:
        self.requests = []

    def status(self):
        return {"ok": True, "version": "fixture", "capabilities": self.capabilities.to_dict()}

    def run(self, request):
        self.requests.append(request)
        return {"ok": True, "final_report": {"status": "approved", "summary": "ok"}}


def _kiro_manifest(*, effect_mode: str = "read-only") -> AgentManifest:
    return AgentManifest(
        reference=AgentRef.parse("local://kiro/security-reviewer@1.0.0"),
        owner="cyber-platform",
        transport="provider",
        target={"provider": "kiro-cli", "agent": "security-reviewer", "effort": "high"},
        capabilities=("workspace.read",)
        if effect_mode == "read-only"
        else ("workspace.read", "workspace.write"),
        input_schema="baldr.Task/v1",
        output_schema="cyber.SecurityReview/v1",
        effect_mode=effect_mode,
    )


def _write_registry(path: Path, manifest: AgentManifest) -> None:
    path.write_text(
        json.dumps(registry_document([manifest]), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _registry_with_manifest(tmp_path: Path, manifest: AgentManifest) -> Path:
    path = tmp_path / "agents.json"
    _write_registry(path, manifest)
    return path


def _init_repo(path: Path) -> None:
    path.mkdir()
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    (path / "README.md").write_text("fixture\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(path), "add", "-A"], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(path),
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.invalid",
            "-c",
            "commit.gpgsign=false",
            "commit",
            "-qm",
            "initial",
        ],
        check=True,
    )


def _phase_report(status: str, summary: str) -> dict:
    return {
        "status": status,
        "summary": summary,
        "files_modified": [],
        "commands_run": [],
        "tests_run": [],
        "verification_needed": [],
        "risks": [],
        "follow_up": [],
        "decisions": {"write_authorization": "not_required"},
    }


def test_agent_references_are_exact_and_canonical() -> None:
    reference = AgentRef.parse("LOCAL://KIRO/SECURITY-REVIEWER@1.0.0")
    assert str(reference) == "local://kiro/security-reviewer@1.0.0"
    for invalid in (
        "kiro/security-reviewer@1",
        "local://kiro/security-reviewer",
        "local://kiro/security-reviewer@latest",
        "local://kiro/security-reviewer@latest?unsafe=true",
    ):
        with pytest.raises(AgentContractError):
            AgentRef.parse(invalid)


def test_agent_manifests_are_immutable_and_reject_inline_credentials() -> None:
    target = {"provider": "kiro-cli", "agent": "security-reviewer"}
    manifest = AgentManifest(
        reference=AgentRef.parse("local://kiro/security-reviewer@1.0.0"),
        owner="cyber-platform",
        transport="PROVIDER",
        target=target,
        capabilities=("workspace.read",),
    )
    digest = manifest.digest
    target["agent"] = "mutated-outside-the-manifest"

    assert manifest.transport == "provider"
    assert manifest.target["agent"] == "security-reviewer"
    assert manifest.digest == digest
    with pytest.raises(TypeError):
        manifest.target["agent"] = "mutated-inside-the-manifest"
    with pytest.raises(AgentContractError, match="inline credentials"):
        AgentManifest(
            reference=AgentRef.parse("local://kiro/unsafe@1.0.0"),
            owner="cyber-platform",
            transport="provider",
            target={"provider": "kiro-cli", "token": "secret"},
        )


def test_local_registry_verifies_the_durable_manifest_digest(tmp_path: Path) -> None:
    path = tmp_path / "agents.json"
    original = _kiro_manifest()
    _write_registry(path, original)
    registry = LocalAgentRegistry(path)
    reference = original.reference

    resolved = registry.resolve(
        reference,
        context=AgentResolutionContext(),
        expected_digest=original.digest,
    )
    assert resolved.manifest.digest == original.digest

    changed = AgentManifest(
        **{
            **original.__dict__,
            "target": {**original.target, "agent": "silently-replaced"},
        }
    )
    _write_registry(path, changed)
    with pytest.raises(AgentDigestMismatchError):
        registry.resolve(
            reference,
            context=AgentResolutionContext(),
            expected_digest=original.digest,
        )

    status = agent_registry_status(path)
    assert status["ok"] is True
    assert status["agent_count"] == 1
    assert status["agents"][0]["ref"] == str(original.reference)
    assert "target" not in status["agents"][0]


def test_gateway_invokes_a_registry_owned_kiro_agent_without_hosting_it(
    tmp_path: Path,
) -> None:
    path = tmp_path / "agents.json"
    manifest = _kiro_manifest()
    _write_registry(path, manifest)
    kiro = KiroFixtureProvider()
    providers = ProviderRegistry([kiro])
    gateway = AgentGateway(
        resolver=LocalAgentRegistry(path),
        connectors=[ProviderAgentConnector(lambda: providers)],
    )

    result = gateway.invoke(
        manifest.reference,
        AgentInvocation(
            cwd=tmp_path,
            task="Review this workspace",
            workflow="fixture",
            step_name="security-review",
            report_kind="review",
            can_write=False,
            sandbox="read-only",
            requested_capabilities=("workspace.read",),
        ),
        expected_digest=manifest.digest,
    )

    assert result["ok"] is True
    assert result["agent_ref"] == str(manifest.reference)
    assert result["agent_manifest_digest"] == manifest.digest
    assert kiro.requests[0].agent == "security-reviewer"
    assert kiro.requests[0].role_name == "security-review"


def test_unavailable_manager_does_not_hide_healthy_local_agents(
    tmp_path: Path, monkeypatch
) -> None:
    import baldr_router.agent_diagnostics as diagnostics_module

    manifest = AgentManifest(
        reference=AgentRef.parse("local://codex/reviewer@1.0.0"),
        owner="local",
        transport="provider",
        target={"provider": "codex"},
        capabilities=("workspace.read",),
    )
    registry_path = _registry_with_manifest(tmp_path, manifest)
    monkeypatch.setenv("BALDR_AGENT_REGISTRY_PATH", str(registry_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setattr(
        agent_gateway_module,
        "agent_manager_status",
        lambda config: {
            "ok": False,
            "configured": True,
            "agents": [],
            "reason": "manager unavailable",
        },
    )
    monkeypatch.setattr(
        diagnostics_module,
        "diagnose_agent_manifest",
        lambda *args, **kwargs: {"state": "ready", "ready": True},
    )

    status = external_agent_catalog_status(workspace_root=tmp_path)

    assert status["ok"] is True
    assert status["degraded"] is True
    assert status["agents"][0]["ref"] == str(manifest.reference)


def test_provider_connector_attests_global_kiro_definition_and_rejects_shadowing(
    tmp_path: Path, monkeypatch
) -> None:
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    definition_path = home / ".kiro" / "agents" / "baldr-worker.json"
    definition_path.parent.mkdir(parents=True)
    definition = b'{"name":"baldr-worker","tools":["read"]}\n'
    definition_path.write_bytes(definition)
    monkeypatch.setenv("HOME", str(home))
    manifest = AgentManifest(
        reference=AgentRef.parse("local://kiro/baldr-worker@1.0.0"),
        owner="external-team",
        transport="provider",
        target={
            "provider": "kiro-cli",
            "agent": "baldr-worker",
            "definition_scope": "global",
            "definition_digest": (
                "sha256:" + __import__("hashlib").sha256(definition).hexdigest()
            ),
        },
        capabilities=("workspace.read",),
    )
    provider = KiroFixtureProvider()
    connector = ProviderAgentConnector(lambda: ProviderRegistry([provider]))
    invocation = AgentInvocation(
        cwd=workspace,
        task="Review",
        workflow="fixture",
        step_name="reviewer",
        report_kind="review",
        can_write=False,
        sandbox="read-only",
        requested_capabilities=("workspace.read",),
    )

    result = connector.invoke(
        ResolvedAgent(manifest=manifest, source="test"), invocation
    )
    assert result["ok"] is True
    assert len(provider.requests) == 1

    shadow = workspace / ".kiro" / "agents" / "baldr-worker.json"
    shadow.parent.mkdir(parents=True)
    shadow.write_bytes(definition)
    with pytest.raises(AgentDigestMismatchError, match="shadows"):
        connector.invoke(
            ResolvedAgent(manifest=manifest, source="test"), invocation
        )

    shadow.unlink()
    definition_path.write_text('{"name":"baldr-worker","tools":["write"]}\n')
    with pytest.raises(AgentDigestMismatchError, match="definition digest mismatch"):
        connector.invoke(
            ResolvedAgent(manifest=manifest, source="test"), invocation
        )


def test_gateway_intersects_write_permission_with_manifest_capabilities(
    tmp_path: Path,
) -> None:
    path = tmp_path / "agents.json"
    manifest = _kiro_manifest(effect_mode="read-only")
    _write_registry(path, manifest)
    gateway = AgentGateway(
        resolver=LocalAgentRegistry(path),
        connectors=[ProviderAgentConnector(lambda: ProviderRegistry([KiroFixtureProvider()]))],
    )

    with pytest.raises(AgentPolicyError, match="not approved for workspace writes"):
        gateway.invoke(
            manifest.reference,
            AgentInvocation(
                cwd=tmp_path,
                task="Modify this workspace",
                workflow="fixture",
                step_name="implementation",
                report_kind="implementation",
                can_write=True,
                sandbox="workspace-write",
                requested_capabilities=("workspace.read", "workspace.write"),
            ),
        )


def test_http_json_connector_invokes_an_external_agent_without_provider_registry(
    tmp_path: Path,
) -> None:
    received: list[dict] = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            length = int(self.headers.get("Content-Length") or 0)
            received.append(json.loads(self.rfile.read(length)))
            payload = json.dumps(
                {
                    "contract": "baldr-agent-result",
                    "version": 1,
                    "result": {
                        "ok": True,
                        "run_id": "http-pilot-1",
                        "agent_ref": "manager://spoofed/identity@9.9.9",
                        "agent_manifest_digest": "sha256:" + "0" * 64,
                        "final_report": _phase_report(
                            "approved", "external HTTP agent"
                        ),
                    },
                }
            ).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, format, *args):
            del format, args

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        manifest = AgentManifest(
            reference=AgentRef.parse("local://pilot/http-reviewer@1.0.0"),
            owner="external-pilot",
            transport="http-json",
            target={
                "endpoint": f"http://127.0.0.1:{server.server_port}/invoke",
                "timeout_seconds": "5",
            },
            capabilities=("workspace.read",),
            effect_mode="read-only",
        )
        gateway = AgentGateway(
            resolver=LocalAgentRegistry(
                _registry_with_manifest(tmp_path, manifest)
            ),
            connectors=[
                HttpJsonAgentConnector(
                    JsonHttpClient(allow_insecure_loopback=True)
                )
            ],
        )
        result = gateway.invoke(
            manifest.reference,
            AgentInvocation(
                cwd=tmp_path,
                task="Review through HTTP",
                workflow="fixture",
                step_name="reviewer",
                report_kind="review",
                can_write=False,
                sandbox="read-only",
                requested_capabilities=("workspace.read",),
                extra_env={"MUST_NOT_LEAVE_BALDR": "secret"},
            ),
            expected_digest=manifest.digest,
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    assert result["ok"] is True
    assert result["agent_ref"] == str(manifest.reference)
    assert result["agent_manifest_digest"] == manifest.digest
    assert result["agent_transport"] == "http-json"
    assert received[0]["agent"] == {
        "ref": str(manifest.reference),
        "digest": manifest.digest,
    }
    assert received[0]["invocation"]["task"] == "Review through HTTP"
    assert "extra_env" not in received[0]["invocation"]
    assert "workspace_root" not in received[0]["invocation"]


def test_http_json_connector_rejects_unapproved_plain_http(tmp_path: Path) -> None:
    manifest = AgentManifest(
        reference=AgentRef.parse("local://pilot/http-reviewer@1.0.0"),
        owner="external-pilot",
        transport="http-json",
        target={"endpoint": "http://127.0.0.1:8080/invoke"},
        capabilities=("workspace.read",),
    )
    connector = HttpJsonAgentConnector(JsonHttpClient())
    with pytest.raises(AgentContractError, match="require HTTPS"):
        connector.invoke(
            ResolvedAgent(manifest=manifest, source="test"),
            AgentInvocation(
                cwd=tmp_path,
                task="Review",
                workflow="fixture",
                step_name="reviewer",
                report_kind="review",
                can_write=False,
                sandbox="read-only",
            ),
        )


def test_legacy_provider_profile_bypasses_the_external_agent_gateway(
    tmp_path: Path, monkeypatch
) -> None:
    provider = KiroFixtureProvider()
    providers = ProviderRegistry([provider])
    monkeypatch.setattr(provider_registry_module, "get_provider_registry", lambda: providers)

    class UnexpectedGateway:
        def invoke(self, *args, **kwargs):
            raise AssertionError("Legacy profiles must not enter AgentGateway.")

    monkeypatch.setattr(agent_gateway_module, "_DEFAULT_GATEWAY", UnexpectedGateway())
    result = run_provider_role(
        provider="kiro-cli",
        role_name="reviewer",
        role=RoleConfig(provider="kiro-cli"),
        cwd=tmp_path,
        prompt="Review without AgentRef",
        workflow="fixture",
        report_kind="review",
    )

    assert result["ok"] is True
    assert len(provider.requests) == 1
    assert provider.requests[0].prompt == "Review without AgentRef"


def test_workflow_snapshot_freezes_the_external_agent_identity(
    tmp_path: Path, monkeypatch
) -> None:
    path = tmp_path / "agents.json"
    manifest = _kiro_manifest()
    _write_registry(path, manifest)
    monkeypatch.setenv("BALDR_AGENT_REGISTRY_PATH", str(path))
    reset_agent_gateway()

    cfg = AppConfig.defaults()
    cfg.execution_profiles["external-review"] = ExecutionProfileConfig(
        agent_ref=str(manifest.reference),
        agent_manifest_digest=manifest.digest,
    )
    cfg.roles["reviewer"].profiles = ["external-review"]
    snapshot = _resolved_snapshot(
        cfg,
        architect_provider=None,
        implementer_provider=None,
        reviewer_provider=None,
        max_rounds=0,
    )
    profile = snapshot["role_plans"]["reviewer"]["profiles"][0]
    assert profile["agent_ref"] == str(manifest.reference)
    assert profile["agent_manifest_digest"] == manifest.digest
    assert profile["agent_transport"] == "provider"
    assert profile["agent_registry"] == "local"
    assert profile["provider"] == "kiro-cli"

    replaced = AgentManifest(
        **{
            **manifest.__dict__,
            "target": {**manifest.target, "agent": "replacement"},
        }
    )
    _write_registry(path, replaced)
    reset_agent_gateway()
    with pytest.raises(AgentDigestMismatchError):
        _resolved_snapshot(
            cfg,
            architect_provider=None,
            implementer_provider=None,
            reviewer_provider=None,
            max_rounds=0,
        )


def test_registry_owned_kiro_agent_runs_through_the_durable_workflow(
    tmp_path: Path, monkeypatch
) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    registry_path = tmp_path / "agents.json"
    manifest = AgentManifest(
        reference=AgentRef.parse("local://kiro/baldr-worker@1.0.0"),
        owner="external-team",
        transport="provider",
        target={"provider": "kiro-cli", "agent": "baldr-worker"},
        capabilities=("workspace.read", "workspace.write"),
        effect_mode="workspace-write",
    )
    _write_registry(registry_path, manifest)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setenv("BALDR_AGENT_REGISTRY_PATH", str(registry_path))
    monkeypatch.setenv(
        "BALDR_TRUSTED_WORKSPACE_ROOTS_JSON", json.dumps([str(repo)])
    )

    class DurableKiroFixtureProvider(KiroFixtureProvider):
        capabilities = ProviderCapabilities(
            supports_read_only=True,
            supports_workspace_write=True,
            supports_structured_output=True,
            read_only_enforcement="enforced",
            write_enforcement="enforced",
        )

        def run(self, request):
            self.requests.append(request)
            status = {
                "architect": "planned",
                "implementer": "implemented",
                "reviewer": "approved",
            }[request.role_name]
            if request.role_name == "implementer":
                (request.cwd / "agent-pilot.txt").write_text(
                    "external agent\n", encoding="utf-8"
                )
            return {
                "ok": True,
                "run_id": f"kiro-{request.role_name}",
                "final_report": _phase_report(status, request.role_name),
            }

    provider = DurableKiroFixtureProvider()
    gateway = AgentGateway(
        resolver=LocalAgentRegistry(registry_path),
        connectors=[
            ProviderAgentConnector(lambda: ProviderRegistry([provider]))
        ],
    )
    monkeypatch.setattr(agent_gateway_module, "_DEFAULT_GATEWAY", gateway)
    cfg = load_config()
    cfg.execution_profiles = {
        "external": ExecutionProfileConfig(
            agent_ref=str(manifest.reference),
            agent_manifest_digest=manifest.digest,
        )
    }
    for role in cfg.roles.values():
        role.profiles = ["external"]
    save_config(cfg)

    result = run_workflow_impl(
        workspace_root=str(repo),
        task="External agent pilot",
        workspace_mode="current",
        max_rounds=0,
    )

    assert result["ok"] is True
    assert (repo / "agent-pilot.txt").read_text(encoding="utf-8") == (
        "external agent\n"
    )
    participants = [
        profile for step in result["steps"] for profile in step["profiles"]
    ]
    assert len(participants) == 3
    assert {profile["agent_ref"] for profile in participants} == {
        str(manifest.reference)
    }
    assert {profile["agent_manifest_digest"] for profile in participants} == {
        manifest.digest
    }


def test_external_write_agent_crash_requires_reconciliation_without_replay(
    tmp_path: Path, monkeypatch
) -> None:
    repo = tmp_path / "repo-crash"
    _init_repo(repo)
    registry_path = tmp_path / "agents-crash.json"
    manifest = AgentManifest(
        reference=AgentRef.parse("local://kiro/write-agent@1.0.0"),
        owner="external-team",
        transport="provider",
        target={"provider": "kiro-cli", "agent": "write-agent"},
        capabilities=("workspace.read", "workspace.write"),
        effect_mode="workspace-write",
    )
    _write_registry(registry_path, manifest)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setenv("BALDR_AGENT_REGISTRY_PATH", str(registry_path))

    class CrashingKiroProvider(KiroFixtureProvider):
        capabilities = ProviderCapabilities(
            supports_read_only=True,
            supports_workspace_write=True,
            supports_structured_output=True,
            read_only_enforcement="enforced",
            write_enforcement="enforced",
        )

        def run(self, request):
            self.requests.append(request)
            if request.role_name == "implementer":
                (request.cwd / "uncertain-effect.txt").write_text(
                    "written before process loss\n", encoding="utf-8"
                )
                raise SimulatedProcessCrash("external-agent-process-loss")
            return {
                "ok": True,
                "run_id": f"kiro-{request.role_name}",
                "final_report": _phase_report("planned", request.role_name),
            }

    provider = CrashingKiroProvider()
    gateway = AgentGateway(
        resolver=LocalAgentRegistry(registry_path),
        connectors=[
            ProviderAgentConnector(lambda: ProviderRegistry([provider]))
        ],
    )
    monkeypatch.setattr(agent_gateway_module, "_DEFAULT_GATEWAY", gateway)
    cfg = AppConfig.defaults()
    cfg.execution_profiles = {
        "external": ExecutionProfileConfig(
            agent_ref=str(manifest.reference),
            agent_manifest_digest=manifest.digest,
        )
    }
    for role in cfg.roles.values():
        role.profiles = ["external"]
    snapshot = _resolved_snapshot(
        cfg,
        architect_provider=None,
        implementer_provider=None,
        reviewer_provider=None,
        max_rounds=0,
        workspace_mode="current",
    )
    store = DurableStore(path=tmp_path / "crash-state.sqlite3")

    with pytest.raises(SimulatedProcessCrash):
        DurableWorkflowEngine(store=store).run(
            workspace_root=repo,
            task="External write crash",
            extra_context="",
            config_snapshot=snapshot,
            context7_libraries=None,
            client_name="test",
            idempotency_key="external-write-crash",
        )

    run_row = store.connect().execute(
        "SELECT id FROM workflow_runs ORDER BY created_at LIMIT 1"
    ).fetchone()
    assert run_row is not None
    run_id = str(run_row["id"])
    with store.connect() as connection:
        connection.execute(
            "UPDATE workflow_runs SET lease_expires_at = ? WHERE id = ?",
            ((datetime.now(timezone.utc) - timedelta(seconds=5)).isoformat(), run_id),
        )
    recover_stale_runs(store)

    recovered = store.snapshot_run(run_id)
    assert recovered["run"]["status"] == "awaiting_reconciliation"
    write_step = next(
        step for step in recovered["steps"] if step["phase"] == "implementer"
    )
    assert write_step["status"] == "unknown"
    assert write_step["participants"][0]["agent_ref"] == str(manifest.reference)
    assert write_step["participants"][0]["agent_manifest_digest"] == manifest.digest
    assert write_step["participants"][0]["attempts"][0]["status"] == "unknown"
    assert [request.role_name for request in provider.requests].count("implementer") == 1

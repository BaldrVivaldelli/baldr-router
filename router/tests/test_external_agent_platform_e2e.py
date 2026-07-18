from __future__ import annotations

import os
import subprocess
import threading
from pathlib import Path

from baldr_router.agent_api import AgentManifest, AgentRef
from baldr_router.agent_gateway import reset_agent_gateway
from baldr_router.agent_http import JsonHttpClient
from baldr_router.agent_manager import HttpAgentManagerAdmin, HttpAgentManagerResolver
from baldr_router.agent_manager_service import build_agent_manager_server
from baldr_router.agent_registry import LocalAgentRegistry
from baldr_router.agent_sources import AgentManagerSource, AgentSourceContext
from baldr_router.agent_sync import AgentCatalogSynchronizer
from baldr_router.config import AgentManagerConfig, AppConfig
from baldr_router.durability.engine import DurableWorkflowEngine, _resolved_snapshot
from baldr_router.durability.store import DurableStore
from baldr_router.team_resolution import resolve_team


def _repo(path: Path) -> Path:
    path.mkdir()
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    (path / "README.md").write_text("external agent platform\n", encoding="utf-8")
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
    return path


def _report(role: str, provider: str) -> dict:
    status = {
        "architect": "planned",
        "implementer": "implemented",
        "reviewer": "approved",
    }[role]
    return {
        "status": status,
        "summary": f"{role} completed by external {provider}",
        "files_modified": ["external-result.txt"] if role == "implementer" else [],
        "commands_run": [],
        "tests_run": [],
        "verification_needed": [],
        "risks": [],
        "follow_up": [],
        "decisions": {"write_authorization": "not_required"},
        "review_decision": "approved" if role == "reviewer" else "not_applicable",
    }


def _catalog(registry: LocalAgentRegistry) -> dict:
    return {
        "agents": [
            {
                "ref": str(manifest.reference),
                "version": manifest.reference.version,
                "digest": manifest.digest,
                "transport": manifest.transport,
                "capabilities": list(manifest.capabilities),
                "effect_mode": manifest.effect_mode,
                "ready": True,
                "enabled": True,
                "state": "ready",
                "reason": None,
            }
            for manifest in registry.manifests()
        ]
    }


def test_external_codex_and_kiro_complete_the_managed_platform_flow(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    token_env = "BALDR_PLATFORM_E2E_MANAGER_TOKEN"
    monkeypatch.setenv(token_env, "platform-e2e-manager-credential")
    registry_path = tmp_path / "local-agents.json"
    monkeypatch.setenv("BALDR_AGENT_REGISTRY_PATH", str(registry_path))
    repo = _repo(tmp_path / "repo")

    server = build_agent_manager_server(
        host="127.0.0.1",
        port=0,
        database=tmp_path / "manager.sqlite3",
        registry="company",
        authorization_env=token_env,
    )
    config = AgentManagerConfig(
        enabled=True,
        registry="company",
        base_url=f"http://127.0.0.1:{server.server_port}",
        authorization_env=token_env,
        allow_insecure_loopback=True,
    )
    client = JsonHttpClient(allow_insecure_loopback=True)
    admin = HttpAgentManagerAdmin(config, client=client)
    resolver = HttpAgentManagerResolver(config, client=client)
    codex = AgentManifest(
        reference=AgentRef.parse("company://product/codex-advisor@1.0.0"),
        owner="product-team",
        transport="provider",
        target={"provider": "codex", "runner": "exec-json"},
        capabilities=("workspace.read", "role.architect", "role.reviewer"),
        effect_mode="read-only",
    )
    kiro = AgentManifest(
        reference=AgentRef.parse("company://product/kiro-writer@1.0.0"),
        owner="product-team",
        transport="provider",
        target={"provider": "kiro-cli", "agent": "baldr-worker"},
        capabilities=("workspace.read", "workspace.write", "role.implementer"),
        effect_mode="workspace-write",
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    reset_agent_gateway()
    try:
        assert admin.publish(codex)["created"] is True
        assert admin.publish(kiro)["created"] is True

        discovered = AgentManagerSource(config, resolver=resolver).discover(
            context=AgentSourceContext(workspace_root=repo)
        )
        assert {candidate.manifest.target["provider"] for candidate in discovered.candidates} == {
            "codex",
            "kiro-cli",
        }

        synchronizer = AgentCatalogSynchronizer(registry_path)
        preview = synchronizer.preview(discovered)
        applied = synchronizer.apply(discovered, actor="platform-e2e")
        assert preview.summary["new"] == 2
        assert len(applied["registry"]["created"]) == 2
        assert synchronizer.apply(discovered, actor="platform-e2e")["changed"] is False

        local_registry = LocalAgentRegistry(registry_path)
        catalog = _catalog(local_registry)
        base_cfg = AppConfig.defaults()
        role_plans = {
            role: {
                "role": role,
                "strategy": "first-success",
                "min_successes": 1,
                "min_approvals": 1,
                "resolution": "first-success",
                "can_write": role == "implementer",
                "sandbox": "workspace-write" if role == "implementer" else "read-only",
                "profiles": [
                    {
                        "name": f"fallback-{role}",
                        "provider": "codex",
                        "model": "",
                        "reasoning_effort": "",
                        "agent": "",
                        "effort": "",
                        "runner": "exec-json",
                        "session_scope": "workspace",
                        "can_write": role == "implementer",
                        "sandbox": "workspace-write"
                        if role == "implementer"
                        else "read-only",
                        "agent_ref": "",
                        "agent_manifest_digest": "",
                    }
                ],
            }
            for role in ("architect", "implementer", "reviewer")
        }
        assigned = resolve_team(role_plans, catalog, mode="automatic")
        assert assigned.roles["architect"].agent_ref == str(codex.reference)
        assert assigned.roles["reviewer"].agent_ref == str(codex.reference)
        assert assigned.roles["implementer"].agent_ref == str(kiro.reference)

        monkeypatch.setattr(
            "baldr_router.durability.engine.external_agent_catalog_status",
            lambda workspace_root: catalog,
        )
        base_cfg.workflows["architect-implement-review"].max_rounds = 0
        snapshot = _resolved_snapshot(
            base_cfg,
            architect_provider=None,
            implementer_provider=None,
            reviewer_provider=None,
            max_rounds=0,
            workspace_mode="current",
            team_mode="automatic",
            workspace_root=repo,
        )
        calls: list[tuple[str, str, str]] = []

        def provider(**kwargs):
            calls.append(
                (kwargs["role_name"], kwargs["provider"], kwargs["agent_ref"])
            )
            if kwargs["role_name"] == "implementer":
                (kwargs["cwd"] / "external-result.txt").write_text(
                    "written by managed external Kiro\n", encoding="utf-8"
                )
            return {
                "ok": True,
                "run_id": f"external-{kwargs['role_name']}",
                "final_report": _report(kwargs["role_name"], kwargs["provider"]),
            }

        store = DurableStore(path=tmp_path / "workflow.sqlite3")
        result = DurableWorkflowEngine(store=store, provider_runner=provider).run(
            workspace_root=repo,
            task="Coordinate managed external Codex and Kiro agents",
            extra_context="",
            config_snapshot=snapshot,
            context7_libraries=None,
            client_name="platform-e2e",
        )
        assert result["ok"] is True
        assert calls == [
            ("architect", "codex", str(codex.reference)),
            ("implementer", "kiro-cli", str(kiro.reference)),
            ("reviewer", "codex", str(codex.reference)),
        ]
        durable = store.snapshot_run(result["run_id"])
        participants = [
            participant
            for step in durable["steps"]
            for participant in step["participants"]
        ]
        assert {participant["agent_ref"] for participant in participants} == {
            str(codex.reference),
            str(kiro.reference),
        }
        assert {participant["agent_manifest_digest"] for participant in participants} == {
            codex.digest,
            kiro.digest,
        }
        assert (repo / "external-result.txt").read_text(encoding="utf-8") == (
            "written by managed external Kiro\n"
        )

        assert admin.set_enabled(kiro.reference, enabled=False)["enabled"] is False
        after_disable = AgentManagerSource(config, resolver=resolver).discover(
            context=AgentSourceContext(workspace_root=repo)
        )
        assert [candidate.manifest.reference for candidate in after_disable.candidates] == [
            codex.reference
        ]
        assert admin.audit(limit=200)["events"]
    finally:
        reset_agent_gateway()
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        os.environ.pop("BALDR_AGENT_REGISTRY_PATH", None)

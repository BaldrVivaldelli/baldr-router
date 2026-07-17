from __future__ import annotations

import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from baldr_router.agent_api import (
    AgentDigestMismatchError,
    AgentContractError,
    AgentNotFoundError,
    AgentTransportError,
    AgentInvocation,
    AgentManifest,
    AgentRef,
    AgentResolutionContext,
)
from baldr_router.agent_gateway import AgentGateway
from baldr_router.agent_http import HttpJsonAgentConnector, JsonHttpClient
from baldr_router.agent_manager import (
    HttpAgentManagerAdmin,
    HttpAgentManagerResolver,
    agent_manager_status,
)
from baldr_router.agent_manager_service import (
    AgentManagerStore,
    build_agent_manager_server,
)
from baldr_router.agent_registry import registry_document
from baldr_router.config import AgentManagerConfig, AppConfig, load_config, save_config


def _report() -> dict:
    return {
        "status": "approved",
        "summary": "agent manager pilot",
        "files_modified": [],
        "commands_run": [],
        "tests_run": [],
        "verification_needed": [],
        "risks": [],
        "follow_up": [],
        "decisions": {},
    }


def test_http_agent_manager_resolves_catalogs_checks_health_and_invokes(
    tmp_path: Path,
) -> None:
    received: list[dict] = []
    server: ThreadingHTTPServer

    class Handler(BaseHTTPRequestHandler):
        def _json(self, payload: dict, status: int = 200) -> None:
            body = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.path == "/v1/health":
                self._json(
                    {
                        "contract": "baldr-agent-manager-health",
                        "version": 1,
                        "status": "ok",
                        "service_version": "fixture-1",
                    }
                )
                return
            if self.path == "/v1/agents?limit=25":
                self._json(
                    {
                        "contract": "baldr-agent-catalog",
                        "version": 1,
                        "agents": [manifest_document],
                    }
                )
                return
            if self.path == "/v1/agents/platform/http-reviewer/versions/1.0.0":
                self._json(
                    {
                        "contract": "baldr-agent-resolution",
                        "version": 1,
                        "manifest": manifest_document,
                    }
                )
                return
            self._json({"error": "not found"}, status=404)

        def do_POST(self):
            if self.path != "/invoke":
                self._json({"error": "not found"}, status=404)
                return
            length = int(self.headers.get("Content-Length") or 0)
            received.append(json.loads(self.rfile.read(length)))
            self._json(
                {
                    "contract": "baldr-agent-result",
                    "version": 1,
                    "result": {
                        "ok": True,
                        "run_id": "manager-pilot-1",
                        "final_report": _report(),
                    },
                }
            )

        def log_message(self, format, *args):
            del format, args

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    manifest = AgentManifest(
        reference=AgentRef.parse("manager://platform/http-reviewer@1.0.0"),
        owner="platform-team",
        transport="http-json",
        target={
            "endpoint": f"http://127.0.0.1:{server.server_port}/invoke",
            "timeout_seconds": "5",
        },
        capabilities=("workspace.read",),
        effect_mode="read-only",
    )
    manifest_document = registry_document([manifest])["agents"][0]
    config = AgentManagerConfig(
        enabled=True,
        registry="manager",
        base_url=f"http://127.0.0.1:{server.server_port}",
        timeout_seconds=5,
        allow_insecure_loopback=True,
        catalog_limit=25,
    )
    client = JsonHttpClient(allow_insecure_loopback=True)
    resolver = HttpAgentManagerResolver(config, client=client)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        status = agent_manager_status(config, client=client)
        resolved = resolver.resolve(
            manifest.reference,
            context=AgentResolutionContext(
                workspace_root=tmp_path,
                requested_capabilities=("workspace.read",),
            ),
            expected_digest=manifest.digest,
        )
        gateway = AgentGateway(
            resolver=resolver,
            connectors=[HttpJsonAgentConnector(client)],
        )
        result = gateway.invoke(
            manifest.reference,
            AgentInvocation(
                cwd=tmp_path,
                task="Review through the Agent Manager",
                workflow="fixture",
                step_name="reviewer",
                report_kind="review",
                can_write=False,
                sandbox="read-only",
                requested_capabilities=("workspace.read",),
            ),
            expected_digest=manifest.digest,
        )
        with pytest.raises(AgentDigestMismatchError):
            resolver.resolve(
                manifest.reference,
                context=AgentResolutionContext(),
                expected_digest="sha256:" + "0" * 64,
            )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    assert status["ok"] is True
    assert status["health"] == {
        "status": "ok",
        "service_version": "fixture-1",
    }
    assert status["agents"][0]["ref"] == str(manifest.reference)
    assert "target" not in status["agents"][0]
    assert resolved.manifest.digest == manifest.digest
    assert result["ok"] is True
    assert result["agent_registry"] == "manager"
    assert received[0]["agent"]["digest"] == manifest.digest


def test_disabled_agent_manager_is_optional() -> None:
    status = agent_manager_status(AgentManagerConfig())
    assert status == {
        "ok": True,
        "configured": False,
        "registry": "manager",
        "agent_count": 0,
        "agents": [],
    }


def test_agent_manager_configuration_round_trips_without_a_secret_value(
    tmp_path: Path,
) -> None:
    path = tmp_path / "config.toml"
    cfg = AppConfig.defaults()
    cfg.agent_manager = AgentManagerConfig(
        enabled=True,
        registry="company",
        base_url="https://agents.example.test",
        authorization_env="BALDR_AGENT_MANAGER_TOKEN",
        timeout_seconds=12,
        catalog_limit=75,
    )
    save_config(cfg, path)
    loaded = load_config(path)

    assert loaded.agent_manager == cfg.agent_manager
    text = path.read_text(encoding="utf-8")
    assert "[agent_manager]" in text
    assert "BALDR_AGENT_MANAGER_TOKEN" in text
    assert "secret-value" not in text


def test_persistent_manager_service_admin_lifecycle_and_exact_resolution(
    tmp_path: Path,
) -> None:
    token_env = "BALDR_TEST_AGENT_MANAGER_TOKEN"
    previous = os.environ.get(token_env)
    os.environ[token_env] = "test-manager-token"
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
    manifest = AgentManifest(
        reference=AgentRef.parse("company://product/reviewer@2.0.0"),
        owner="product",
        transport="provider",
        target={"provider": "codex", "runner": "exec-json"},
        capabilities=("workspace.read",),
        effect_mode="read-only",
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        assert admin.publish(manifest)["created"] is True
        assert admin.publish(manifest)["created"] is False
        resolved = resolver.resolve(
            manifest.reference,
            context=AgentResolutionContext(),
            expected_digest=manifest.digest,
        )
        assert resolved.manifest.digest == manifest.digest
        assert admin.set_enabled(manifest.reference, enabled=False)["enabled"] is False
        with pytest.raises(AgentNotFoundError):
            resolver.resolve(
                manifest.reference,
                context=AgentResolutionContext(),
            )
        assert admin.set_enabled(manifest.reference, enabled=True)["enabled"] is True
        assert admin.revoke(manifest.reference)["revoked"] is True
        with pytest.raises(AgentTransportError):
            admin.set_enabled(manifest.reference, enabled=True)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        if previous is None:
            os.environ.pop(token_env, None)
        else:
            os.environ[token_env] = previous

    reopened = AgentManagerStore(tmp_path / "manager.sqlite3", registry="company")
    assert reopened.resolve(manifest.reference) is None
    assert reopened.health() == {"total": 1, "enabled": 0, "revoked": 1}


def test_manager_rejects_mutating_an_existing_exact_version(tmp_path: Path) -> None:
    store = AgentManagerStore(tmp_path / "manager.sqlite3", registry="company")
    first = AgentManifest(
        reference=AgentRef.parse("company://product/reviewer@1.0.0"),
        owner="product",
        transport="provider",
        target={"provider": "codex"},
        capabilities=("workspace.read",),
    )
    changed = AgentManifest(
        reference=first.reference,
        owner="another-owner",
        transport="provider",
        target={"provider": "codex"},
        capabilities=("workspace.read",),
    )
    store.publish(first)
    with pytest.raises(AgentContractError, match="immutable"):
        store.publish(changed)


def test_manager_service_requires_an_environment_credential(tmp_path: Path) -> None:
    missing = "BALDR_TEST_MISSING_MANAGER_TOKEN"
    previous = os.environ.pop(missing, None)
    try:
        with pytest.raises(AgentContractError, match="unavailable"):
            build_agent_manager_server(
                host="127.0.0.1",
                port=0,
                database=tmp_path / "manager.sqlite3",
                registry="company",
                authorization_env=missing,
            )
    finally:
        if previous is not None:
            os.environ[missing] = previous

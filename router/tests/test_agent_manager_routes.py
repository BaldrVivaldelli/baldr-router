"""Characterization tests for the Agent Manager HTTP surface.

The route handling used to live inside a 509-line factory with a closure
handler, and the only coverage went through the typed admin/resolver clients.
These tests speak raw HTTP so the status codes, contracts and audit records of
every route are pinned independently of the client library.
"""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

import pytest

from baldr_router.agent_api import AgentManifest, AgentRef
from baldr_router.agent_manager_service import (
    AGENT_MANAGER_SCHEMA_VERSION,
    AgentManagerHTTPServer,
    AgentManagerRequestHandler,
    build_agent_manager_server,
)

TOKEN = "route-fixture-credential"
TOKEN_ENV = "BALDR_ROUTE_FIXTURE_TOKEN"


def _manifest(name: str = "reviewer") -> dict[str, Any]:
    return AgentManifest(
        reference=AgentRef.parse(f"company://product/{name}@1.0.0"),
        owner="product-team",
        transport="provider",
        target={"provider": "codex", "runner": "exec-json"},
        capabilities=("workspace.read",),
        effect_mode="read-only",
    ).canonical_payload()


class _Service:
    def __init__(self, tmp_path: Path) -> None:
        self.server = build_agent_manager_server(
            host="127.0.0.1",
            port=0,
            database=tmp_path / "manager.sqlite3",
            registry="company",
            authorization_env=TOKEN_ENV,
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"

    def close(self) -> None:
        self.server.shutdown()
        self.thread.join(timeout=5)
        self.server.server_close()

    def call(
        self,
        method: str,
        path: str,
        *,
        payload: dict[str, Any] | None = None,
        token: str | None = TOKEN,
    ) -> tuple[int, dict[str, Any]]:
        body = json.dumps(payload).encode("utf-8") if payload is not None else None
        request = urllib.request.Request(f"{self.base}{path}", data=body, method=method)
        if token is not None:
            request.add_header("Authorization", f"Bearer {token}")
        if body is not None:
            request.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return response.status, json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as error:
            return error.code, json.loads(error.read().decode("utf-8"))


@pytest.fixture()
def service(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv(TOKEN_ENV, TOKEN)
    running = _Service(tmp_path)
    try:
        yield running
    finally:
        running.close()


def test_probes_answer_without_authentication(service: _Service) -> None:
    status, live = service.call("GET", "/livez", token=None)
    assert (status, live["probe"], live["status"]) == (200, "live", "ok")

    status, ready = service.call("GET", "/readyz", token=None)
    assert (status, ready["probe"], ready["status"]) == (200, "ready", "ok")


def test_every_authenticated_route_answers_its_contract(service: _Service) -> None:
    status, health = service.call("GET", "/v1/health")
    assert status == 200
    assert health["schema_version"] == AGENT_MANAGER_SCHEMA_VERSION
    assert health["registry"] == "company"
    assert isinstance(health["uptime_seconds"], int)

    assert service.call("GET", "/v1/agents")[1]["agents"] == []
    # Reading the audit log records its own decision, so earlier calls appear.
    assert [event["action"] for event in service.call("GET", "/v1/audit")[1]["events"]] == [
        "health",
        "catalog",
    ]
    assert service.call("GET", "/v1/metrics")[1]["registry"] == "company"


def test_publish_then_resolve_and_disable_through_raw_http(service: _Service) -> None:
    status, published = service.call(
        "POST", "/v1/agents", payload={"manifest": _manifest()}
    )
    assert (status, published["ok"]) == (201, True)
    assert published["tenant"] == "product"

    path = "/v1/agents/product/reviewer/versions/1.0.0"
    status, resolved = service.call("GET", path)
    assert status == 200
    assert resolved["manifest"]["owner"] == "product-team"

    status, disabled = service.call("POST", f"{path}/disable", payload={})
    assert (status, disabled["ok"]) == (200, True)


def test_unauthenticated_and_unauthorized_requests_are_rejected(
    service: _Service,
) -> None:
    status, denied = service.call("GET", "/v1/agents", token=None)
    assert (status, denied["error"]["code"]) == (401, "unauthorized")

    status, wrong = service.call("GET", "/v1/agents", token="not-the-token")
    assert (status, wrong["error"]["code"]) == (401, "unauthorized")


def test_malformed_requests_report_the_precise_failure(service: _Service) -> None:
    status, bad_limit = service.call("GET", "/v1/agents?limit=abc")
    assert (status, bad_limit["error"]["code"]) == (400, "invalid_limit")

    status, bad_cursor = service.call("GET", "/v1/audit?after=abc")
    assert (status, bad_cursor["error"]["code"]) == (400, "invalid_cursor")

    status, missing = service.call("GET", "/v1/agents/product/ghost/versions/9.9.9")
    assert (status, missing["error"]["code"]) == (404, "agent_not_found")

    status, no_manifest = service.call("POST", "/v1/agents", payload={})
    assert (status, no_manifest["error"]["code"]) == (409, "agent_contract_error")

    status, unknown_route = service.call("POST", "/v1/unknown", payload={})
    assert (status, unknown_route["error"]["code"]) == (404, "route_not_found")


def test_lifecycle_failures_are_audited_as_lifecycle_actions(
    service: _Service,
) -> None:
    """The suffix loop used to overwrite the audit action with "/disable"."""
    path = "/v1/agents/product/ghost/versions/1.0.0/disable"
    status, missing = service.call("POST", path, payload={})
    assert (status, missing["error"]["code"]) == (404, "agent_not_found")

    events = service.call("GET", "/v1/audit")[1]["events"]
    lifecycle = [event for event in events if event["outcome"] == "failed"]
    assert lifecycle, "the failed lifecycle attempt was not audited"
    assert lifecycle[-1]["action"] == "lifecycle"
    assert not any(event["action"].startswith("/") for event in events)


def test_audit_records_every_route_decision(service: _Service) -> None:
    service.call("GET", "/v1/health")
    service.call("GET", "/v1/agents")
    service.call("GET", "/v1/agents", token=None)
    service.call("POST", "/v1/agents", payload={"manifest": _manifest("auditable")})

    events = service.call("GET", "/v1/audit")[1]["events"]
    actions = {event["action"] for event in events}
    assert {"health", "catalog", "publish"} <= actions
    denied = [event for event in events if event["outcome"] == "denied"]
    assert denied and denied[0]["detail_code"] == "authentication_required"
    # Audit records stay free of credential material.
    assert TOKEN not in json.dumps(events)


def test_handler_reads_its_collaborators_from_the_server(service: _Service) -> None:
    """The handler is a module-level class, not a closure over the factory."""
    assert isinstance(service.server, AgentManagerHTTPServer)
    assert service.server.RequestHandlerClass is AgentManagerRequestHandler
    assert service.server.store.registry == "company"
    assert service.server.policy.registry == "company"
    assert AgentManagerRequestHandler.__module__.endswith("agent_manager_service")

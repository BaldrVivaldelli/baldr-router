from __future__ import annotations

import json
import os
import sqlite3
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator
from referencing import Registry, Resource

from baldr_router import cli
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
    AgentManagerPublisher,
    HttpAgentManagerAdmin,
    HttpAgentManagerResolver,
    agent_manager_status,
    load_agent_publication,
    write_agent_publication,
)
from baldr_router.agent_manager_policy import AgentManagerPolicy
from baldr_router.agent_manager_service import (
    AGENT_MANAGER_SCHEMA_VERSION,
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


def _manager_config(*, port: int, credential_env: str) -> AgentManagerConfig:
    return AgentManagerConfig(
        enabled=True,
        registry="company",
        base_url=f"http://127.0.0.1:{port}",
        authorization_env=credential_env,
        allow_insecure_loopback=True,
    )


def _managed_manifest(*, tenant: str, owner: str, name: str) -> AgentManifest:
    return AgentManifest(
        reference=AgentRef.parse(f"company://{tenant}/{name}@1.0.0"),
        owner=owner,
        transport="provider",
        target={"provider": "codex", "runner": "exec-json"},
        capabilities=("workspace.read",),
        effect_mode="read-only",
    )


def test_manager_policy_enforces_rbac_tenancy_ownership_and_safe_audit(
    tmp_path: Path,
    monkeypatch,
) -> None:
    credentials = {
        "BALDR_MANAGER_ADMIN": "admin-fixture-credential",
        "BALDR_MANAGER_PRODUCT_PUBLISHER": "publisher-fixture-credential",
        "BALDR_MANAGER_PRODUCT_OPERATOR": "operator-fixture-credential",
        "BALDR_MANAGER_PRODUCT_AUDITOR": "auditor-fixture-credential",
        "BALDR_MANAGER_CYBER_READER": "reader-fixture-credential",
    }
    for name, value in credentials.items():
        monkeypatch.setenv(name, value)
    policy_path = tmp_path / "manager-policy.json"
    policy_path.write_text(
        json.dumps(
            {
                "contract": "baldr-agent-manager-policy",
                "version": 1,
                "registry": "company",
                "principals": [
                    {
                        "id": "root-admin",
                        "credential_env": "BALDR_MANAGER_ADMIN",
                        "roles": ["admin"],
                        "tenants": ["*"],
                        "owners": ["*"],
                    },
                    {
                        "id": "product-publisher",
                        "credential_env": "BALDR_MANAGER_PRODUCT_PUBLISHER",
                        "roles": ["publisher"],
                        "tenants": ["product"],
                        "owners": ["product-team"],
                    },
                    {
                        "id": "product-operator",
                        "credential_env": "BALDR_MANAGER_PRODUCT_OPERATOR",
                        "roles": ["operator"],
                        "tenants": ["product"],
                        "owners": ["product-team"],
                    },
                    {
                        "id": "product-auditor",
                        "credential_env": "BALDR_MANAGER_PRODUCT_AUDITOR",
                        "roles": ["auditor"],
                        "tenants": ["product"],
                        "owners": ["product-team"],
                    },
                    {
                        "id": "cyber-reader",
                        "credential_env": "BALDR_MANAGER_CYBER_READER",
                        "roles": ["reader"],
                        "tenants": ["cyber"],
                        "owners": ["cyber-team"],
                    },
                ],
            }
        ),
        encoding="utf-8",
    )
    database = tmp_path / "manager.sqlite3"
    server = build_agent_manager_server(
        host="127.0.0.1",
        port=0,
        database=database,
        registry="company",
        authorization_env="",
        policy_path=policy_path,
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    client = JsonHttpClient(allow_insecure_loopback=True)
    admin = HttpAgentManagerAdmin(
        _manager_config(port=server.server_port, credential_env="BALDR_MANAGER_ADMIN"),
        client=client,
    )
    publisher = HttpAgentManagerAdmin(
        _manager_config(
            port=server.server_port,
            credential_env="BALDR_MANAGER_PRODUCT_PUBLISHER",
        ),
        client=client,
    )
    operator = HttpAgentManagerAdmin(
        _manager_config(
            port=server.server_port,
            credential_env="BALDR_MANAGER_PRODUCT_OPERATOR",
        ),
        client=client,
    )
    auditor = HttpAgentManagerAdmin(
        _manager_config(
            port=server.server_port,
            credential_env="BALDR_MANAGER_PRODUCT_AUDITOR",
        ),
        client=client,
    )
    cyber = HttpAgentManagerResolver(
        _manager_config(
            port=server.server_port,
            credential_env="BALDR_MANAGER_CYBER_READER",
        ),
        client=client,
    )
    product_manifest = _managed_manifest(
        tenant="product", owner="product-team", name="reviewer"
    )
    cyber_manifest = _managed_manifest(
        tenant="cyber", owner="cyber-team", name="reviewer"
    )
    wrong_owner = _managed_manifest(
        tenant="product", owner="another-team", name="wrong-owner"
    )
    try:
        published = publisher.publish(product_manifest)
        assert published["actor"] == "product-publisher"
        assert published["tenant"] == "product"
        assert published["audit_sequence"] >= 1
        assert admin.publish(cyber_manifest)["created"] is True

        with pytest.raises(AgentTransportError) as tenant_denied:
            publisher.publish(cyber_manifest)
        assert tenant_denied.value.status_code == 403
        with pytest.raises(AgentTransportError) as owner_denied:
            publisher.publish(wrong_owner)
        assert owner_denied.value.status_code == 403
        with pytest.raises(AgentTransportError) as role_denied:
            publisher.set_enabled(product_manifest.reference, enabled=False)
        assert role_denied.value.status_code == 403

        assert [item.reference.namespace for item in cyber.catalog()] == ["cyber"]
        with pytest.raises(AgentTransportError) as invisible:
            cyber.resolve(
                product_manifest.reference,
                context=AgentResolutionContext(),
            )
        assert invisible.value.status_code == 403

        assert operator.set_enabled(product_manifest.reference, enabled=False)[
            "enabled"
        ] is False
        metrics = operator.metrics()
        assert metrics["total"] == 1
        assert metrics["requests"] >= 1

        audit = auditor.audit(limit=200)
        assert audit["events"]
        assert {event["tenant"] for event in audit["events"]} == {"product"}
        assert any(
            event["detail_code"] == "permission_denied"
            and event["outcome"] == "denied"
            for event in audit["events"]
        )

        base_url = f"http://127.0.0.1:{server.server_port}"
        assert client.request_json(method="GET", url=f"{base_url}/livez")["status"] == "ok"
        assert client.request_json(method="GET", url=f"{base_url}/readyz")["status"] == "ok"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    persisted = database.read_bytes()
    for credential in credentials.values():
        assert credential.encode() not in persisted
    with AgentManagerStore(database, registry="company").connect() as connection:
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            connection.execute(
                "UPDATE agent_manager_audit_events SET outcome = 'allowed' WHERE sequence = 1"
            )


def test_manager_migrates_legacy_catalog_and_creates_consistent_backup(
    tmp_path: Path,
) -> None:
    database = tmp_path / "legacy.sqlite3"
    manifest = _managed_manifest(
        tenant="product", owner="product-team", name="legacy-reviewer"
    )
    document = {**manifest.canonical_payload(), "digest": manifest.digest}
    with sqlite3.connect(database) as connection:
        connection.execute(
            """
            CREATE TABLE agent_manifests (
                reference TEXT PRIMARY KEY,
                digest TEXT NOT NULL,
                manifest_json TEXT NOT NULL,
                enabled INTEGER NOT NULL DEFAULT 1,
                revoked INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        connection.execute(
            "INSERT INTO agent_manifests(reference, digest, manifest_json) VALUES (?, ?, ?)",
            (str(manifest.reference), manifest.digest, json.dumps(document)),
        )

    store = AgentManagerStore(database, registry="company")
    assert store.schema_version == AGENT_MANAGER_SCHEMA_VERSION
    assert store.catalog(limit=10, tenants=("product",)) == [document]
    assert store.catalog(limit=10, tenants=("cyber",)) == []
    result = store.backup(tmp_path / "backup" / "manager.sqlite3")
    backup = AgentManagerStore(Path(result["backup"]), registry="company")
    assert backup.schema_version == AGENT_MANAGER_SCHEMA_VERSION
    assert backup.resolve(manifest.reference) == document


def test_publication_sdk_policy_and_public_contract_are_secret_free(
    tmp_path: Path,
    monkeypatch,
) -> None:
    manifest = _managed_manifest(
        tenant="product", owner="product-team", name="published-reviewer"
    )
    publication = tmp_path / "agent-publication.json"
    assert write_agent_publication(publication, manifest) == publication.resolve()
    assert load_agent_publication(publication).digest == manifest.digest
    assert AgentManagerPublisher.validate_file(publication) == {
        "ok": True,
        "reference": str(manifest.reference),
        "digest": manifest.digest,
        "owner": "product-team",
        "effect_mode": "read-only",
    }
    if os.name != "nt":
        # Windows chmod controls the read-only attribute rather than POSIX
        # group/other permission bits. Access there is inherited from ACLs.
        assert publication.stat().st_mode & 0o077 == 0
    with pytest.raises(AgentContractError, match="already exists"):
        write_agent_publication(publication, manifest)

    monkeypatch.setenv("BALDR_MANAGER_PRODUCT", "publication-policy-credential")
    document = {
        "contract": "baldr-agent-manager-policy",
        "version": 1,
        "registry": "company",
        "principals": [
            {
                "id": "product-publisher",
                "credential_env": "BALDR_MANAGER_PRODUCT",
                "roles": ["publisher"],
                "tenants": ["product"],
                "owners": ["product-team"],
            }
        ],
    }
    policy = AgentManagerPolicy.from_document(document)
    assert policy.safe_document() == document
    assert "publication-policy-credential" not in json.dumps(policy.safe_document())
    with pytest.raises(AgentContractError, match="Unexpected Agent Manager principal"):
        AgentManagerPolicy.from_document(
            {
                **document,
                "principals": [{**document["principals"][0], "token": "not-allowed"}],
            }
        )

    root = Path(__file__).resolve().parents[2]
    schema = json.loads(
        (root / "contracts" / "agent-manager-v1.schema.json").read_text(
            encoding="utf-8"
        )
    )
    registry_schema = json.loads(
        (root / "contracts" / "agent-registry-v1.schema.json").read_text(
            encoding="utf-8"
        )
    )
    Draft202012Validator.check_schema(schema)
    resources = Registry().with_resource(
        "https://baldr.dev/contracts/agent-registry-v1.schema.json",
        Resource.from_contents(registry_schema),
    )
    validator = Draft202012Validator(schema, registry=resources)
    validator.validate(document)
    validator.validate(json.loads(publication.read_text(encoding="utf-8")))


def test_manager_cli_bootstraps_publication_policy_backup_and_doctor(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    publication = tmp_path / "reviewer.agent.json"
    assert cli.main(
        [
            "agent-manager",
            "init-manifest",
            str(publication),
            "company://product/reviewer@3.0.0",
            "--owner",
            "product-team",
            "--transport",
            "http-json",
            "--target",
            "endpoint=https://agents.example.test/reviewer",
            "--target",
            "authorization_env=PRODUCT_AGENT_TOKEN",
            "--capability",
            "workspace.read",
        ]
    ) == 0
    created = json.loads(capsys.readouterr().out)
    assert created["reference"] == "company://product/reviewer@3.0.0"
    assert cli.main(
        ["agent-manager", "validate-manifest", str(publication)]
    ) == 0
    assert json.loads(capsys.readouterr().out)["ok"] is True

    policy = tmp_path / "manager-policy.json"
    assert cli.main(
        [
            "agent-manager",
            "init-policy",
            str(policy),
            "--registry",
            "company",
            "--principal-id",
            "product-publisher",
            "--credential-env",
            "PRODUCT_MANAGER_TOKEN",
            "--role",
            "publisher",
            "--tenant",
            "product",
            "--owner-scope",
            "product-team",
        ]
    ) == 0
    policy_result = json.loads(capsys.readouterr().out)
    assert policy_result["policy"]["principals"][0]["credential_env"] == (
        "PRODUCT_MANAGER_TOKEN"
    )
    policy_text = policy.read_text(encoding="utf-8")
    assert "PRODUCT_MANAGER_TOKEN" in policy_text
    assert '"token"' not in policy_text

    database = tmp_path / "manager.sqlite3"
    assert cli.main(
        [
            "agent-manager",
            "doctor",
            "--database",
            str(database),
            "--registry",
            "company",
        ]
    ) == 0
    doctor = json.loads(capsys.readouterr().out)
    assert doctor["schema_version"] == AGENT_MANAGER_SCHEMA_VERSION
    backup = tmp_path / "manager-backup.sqlite3"
    assert cli.main(
        [
            "agent-manager",
            "backup",
            "--database",
            str(database),
            "--registry",
            "company",
            "--output",
            str(backup),
        ]
    ) == 0
    assert json.loads(capsys.readouterr().out)["backup"] == str(backup.resolve())

from __future__ import annotations

import json
import os
import re
import sqlite3
import time
import urllib.parse
import uuid
from collections.abc import Mapping
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from . import __version__
from .agent_api import AgentContractError, AgentManifest, AgentRef
from .agent_manager import (
    AGENT_MANAGER_CONTRACT_VERSION,
    ADMIN_CONTRACT,
    AUDIT_CONTRACT,
    CATALOG_CONTRACT,
    HEALTH_CONTRACT,
    METRICS_CONTRACT,
    PROBE_CONTRACT,
    RESOLUTION_CONTRACT,
)
from .agent_manager_policy import (
    AgentManagerPolicy,
    AgentManagerPrincipal,
)

MAX_REQUEST_BYTES = 2 * 1024 * 1024
AGENT_MANAGER_SCHEMA_VERSION = 2
_REQUEST_ID = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")


class AgentManagerStore:
    """Persistent exact-version catalog owned by the Agent Manager service."""

    def __init__(self, path: Path, *, registry: str) -> None:
        self.path = path.expanduser().resolve()
        if path.expanduser().is_symlink():
            raise AgentContractError(
                "Agent Manager database cannot be a symbolic link."
            )
        self.registry = AgentRef.parse(f"{registry}://validation/agent@1").registry
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with self.connect() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA foreign_keys=ON")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS agent_manifests (
                    reference TEXT PRIMARY KEY,
                    digest TEXT NOT NULL,
                    manifest_json TEXT NOT NULL,
                    tenant TEXT NOT NULL DEFAULT '',
                    owner TEXT NOT NULL DEFAULT '',
                    enabled INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
                    revoked INTEGER NOT NULL DEFAULT 0 CHECK (revoked IN (0, 1)),
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS agent_manager_metadata (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS agent_manager_audit_events (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    occurred_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    request_id TEXT NOT NULL,
                    principal_id TEXT NOT NULL,
                    tenant TEXT NOT NULL,
                    action TEXT NOT NULL,
                    reference TEXT NOT NULL,
                    outcome TEXT NOT NULL CHECK (outcome IN ('allowed', 'denied', 'failed')),
                    status_code INTEGER NOT NULL,
                    detail_code TEXT NOT NULL
                );
                CREATE TRIGGER IF NOT EXISTS trg_agent_manager_audit_no_update
                    BEFORE UPDATE ON agent_manager_audit_events
                    BEGIN SELECT RAISE(ABORT, 'agent manager audit events are append-only'); END;
                CREATE TRIGGER IF NOT EXISTS trg_agent_manager_audit_no_delete
                    BEFORE DELETE ON agent_manager_audit_events
                    BEGIN SELECT RAISE(ABORT, 'agent manager audit events are append-only'); END;
                """
            )
            columns = {
                str(row["name"])
                for row in connection.execute("PRAGMA table_info(agent_manifests)").fetchall()
            }
            if "tenant" not in columns:
                connection.execute(
                    "ALTER TABLE agent_manifests ADD COLUMN tenant TEXT NOT NULL DEFAULT ''"
                )
            if "owner" not in columns:
                connection.execute(
                    "ALTER TABLE agent_manifests ADD COLUMN owner TEXT NOT NULL DEFAULT ''"
                )
            connection.executescript(
                """
                CREATE INDEX IF NOT EXISTS idx_agent_manager_manifest_tenant
                    ON agent_manifests(tenant, reference);
                CREATE INDEX IF NOT EXISTS idx_agent_manager_audit_tenant_sequence
                    ON agent_manager_audit_events(tenant, sequence);
                """
            )
            stale = connection.execute(
                "SELECT reference, manifest_json FROM agent_manifests WHERE tenant = '' OR owner = ''"
            ).fetchall()
            for row in stale:
                try:
                    manifest = AgentManifest.from_dict(json.loads(str(row["manifest_json"])))
                except (AgentContractError, json.JSONDecodeError) as exc:
                    raise AgentContractError(
                        "Agent Manager database contains an invalid legacy manifest."
                    ) from exc
                connection.execute(
                    "UPDATE agent_manifests SET tenant = ?, owner = ? WHERE reference = ?",
                    (
                        manifest.reference.namespace,
                        manifest.owner.lower(),
                        str(row["reference"]),
                    ),
                )
            connection.execute(
                "INSERT OR REPLACE INTO agent_manager_metadata(key, value) VALUES ('schema_version', ?)",
                (str(AGENT_MANAGER_SCHEMA_VERSION),),
            )
        try:
            self.path.chmod(0o600)
        except OSError:
            pass

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=5)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=5000")
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    def _validate(self, manifest: AgentManifest) -> None:
        if manifest.reference.registry != self.registry:
            raise AgentContractError(
                f"Agent Manager {self.registry!r} cannot own {manifest.reference}."
            )

    @staticmethod
    def _document(manifest: AgentManifest) -> dict[str, Any]:
        return {**manifest.canonical_payload(), "digest": manifest.digest}

    def publish(self, manifest: AgentManifest) -> dict[str, Any]:
        self._validate(manifest)
        reference = str(manifest.reference)
        document = self._document(manifest)
        payload = json.dumps(document, ensure_ascii=False, sort_keys=True)
        with self.connect() as connection:
            existing = connection.execute(
                "SELECT digest, revoked FROM agent_manifests WHERE reference = ?",
                (reference,),
            ).fetchone()
            if existing is not None:
                if str(existing["digest"]) != manifest.digest:
                    raise AgentContractError(
                        f"Agent {reference} is immutable; publish a new exact version."
                    )
                return {
                    "created": False,
                    "reference": reference,
                    "digest": manifest.digest,
                    "revoked": bool(existing["revoked"]),
                }
            connection.execute(
                """
                INSERT INTO agent_manifests(
                    reference, digest, manifest_json, tenant, owner
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    reference,
                    manifest.digest,
                    payload,
                    manifest.reference.namespace,
                    manifest.owner.lower(),
                ),
            )
        return {
            "created": True,
            "reference": reference,
            "digest": manifest.digest,
            "revoked": False,
        }

    def set_enabled(self, reference: AgentRef, *, enabled: bool) -> dict[str, Any]:
        if reference.registry != self.registry:
            raise KeyError(str(reference))
        with self.connect() as connection:
            row = connection.execute(
                "SELECT revoked FROM agent_manifests WHERE reference = ?",
                (str(reference),),
            ).fetchone()
            if row is None:
                raise KeyError(str(reference))
            if enabled and bool(row["revoked"]):
                raise AgentContractError(
                    f"Revoked agent {reference} cannot be enabled again."
                )
            connection.execute(
                """
                UPDATE agent_manifests
                SET enabled = ?, updated_at = CURRENT_TIMESTAMP
                WHERE reference = ?
                """,
                (int(enabled), str(reference)),
            )
        return {"reference": str(reference), "enabled": enabled}

    def revoke(self, reference: AgentRef) -> dict[str, Any]:
        if reference.registry != self.registry:
            raise KeyError(str(reference))
        with self.connect() as connection:
            cursor = connection.execute(
                """
                UPDATE agent_manifests
                SET revoked = 1, enabled = 0, updated_at = CURRENT_TIMESTAMP
                WHERE reference = ?
                """,
                (str(reference),),
            )
            if cursor.rowcount != 1:
                raise KeyError(str(reference))
        return {"reference": str(reference), "enabled": False, "revoked": True}

    def resolve(self, reference: AgentRef) -> dict[str, Any] | None:
        if reference.registry != self.registry:
            return None
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT manifest_json FROM agent_manifests
                WHERE reference = ? AND enabled = 1 AND revoked = 0
                """,
                (str(reference),),
            ).fetchone()
        return json.loads(str(row["manifest_json"])) if row is not None else None

    @staticmethod
    def _tenant_clause(tenants: tuple[str, ...] | None) -> tuple[str, tuple[Any, ...]]:
        if tenants is None or "*" in tenants:
            return "", ()
        if not tenants:
            return " AND 1 = 0", ()
        placeholders = ",".join("?" for _ in tenants)
        return f" AND tenant IN ({placeholders})", tuple(tenants)

    def catalog(
        self,
        *,
        limit: int,
        tenants: tuple[str, ...] | None = None,
    ) -> list[dict[str, Any]]:
        bounded = max(1, min(int(limit), 1000))
        clause, params = self._tenant_clause(tenants)
        with self.connect() as connection:
            rows = connection.execute(
                f"""
                SELECT manifest_json FROM agent_manifests
                WHERE enabled = 1 AND revoked = 0
                {clause}
                ORDER BY reference LIMIT ?
                """,
                (*params, bounded),
            ).fetchall()
        return [json.loads(str(row["manifest_json"])) for row in rows]

    def health(self, *, tenants: tuple[str, ...] | None = None) -> dict[str, Any]:
        clause, params = self._tenant_clause(tenants)
        with self.connect() as connection:
            row = connection.execute(
                f"""
                SELECT
                    COUNT(*) AS total,
                    SUM(CASE WHEN enabled = 1 AND revoked = 0 THEN 1 ELSE 0 END) AS enabled,
                    SUM(CASE WHEN revoked = 1 THEN 1 ELSE 0 END) AS revoked
                FROM agent_manifests
                WHERE 1 = 1 {clause}
                """,
                params,
            ).fetchone()
        return {
            "total": int(row["total"] or 0),
            "enabled": int(row["enabled"] or 0),
            "revoked": int(row["revoked"] or 0),
        }

    @property
    def schema_version(self) -> int:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT value FROM agent_manager_metadata WHERE key = 'schema_version'"
            ).fetchone()
        return int(row["value"]) if row is not None else 0

    def audit(
        self,
        *,
        request_id: str,
        principal_id: str,
        tenant: str,
        action: str,
        reference: str = "",
        outcome: str,
        status_code: int,
        detail_code: str,
    ) -> int:
        with self.connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO agent_manager_audit_events(
                    request_id, principal_id, tenant, action, reference,
                    outcome, status_code, detail_code
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    request_id[:128],
                    principal_id[:96],
                    tenant[:96],
                    action[:64],
                    reference[:512],
                    outcome,
                    int(status_code),
                    detail_code[:96],
                ),
            )
            return int(cursor.lastrowid)

    def audit_events(
        self,
        *,
        after: int,
        limit: int,
        tenants: tuple[str, ...] | None,
    ) -> list[dict[str, Any]]:
        bounded = max(1, min(int(limit), 200))
        clause, params = self._tenant_clause(tenants)
        with self.connect() as connection:
            rows = connection.execute(
                f"""
                SELECT sequence, occurred_at, request_id, principal_id, tenant,
                       action, reference, outcome, status_code, detail_code
                FROM agent_manager_audit_events
                WHERE sequence > ? {clause}
                ORDER BY sequence LIMIT ?
                """,
                (max(0, int(after)), *params, bounded),
            ).fetchall()
        return [dict(row) for row in rows]

    def metrics(self, *, tenants: tuple[str, ...] | None) -> dict[str, Any]:
        clause, params = self._tenant_clause(tenants)
        with self.connect() as connection:
            audit_row = connection.execute(
                f"""
                SELECT COUNT(*) AS requests,
                       SUM(CASE WHEN outcome = 'denied' THEN 1 ELSE 0 END) AS denied,
                       SUM(CASE WHEN outcome = 'failed' THEN 1 ELSE 0 END) AS failed,
                       MAX(sequence) AS last_sequence
                FROM agent_manager_audit_events
                WHERE 1 = 1 {clause}
                """,
                params,
            ).fetchone()
        return {
            **self.health(tenants=tenants),
            "requests": int(audit_row["requests"] or 0),
            "denied": int(audit_row["denied"] or 0),
            "failed": int(audit_row["failed"] or 0),
            "last_audit_sequence": int(audit_row["last_sequence"] or 0),
        }

    def backup(self, destination: Path) -> dict[str, Any]:
        target = destination.expanduser()
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            descriptor = os.open(target, flags, 0o600)
        except FileExistsError as exc:
            raise AgentContractError(
                "Agent Manager backup destination already exists."
            ) from exc
        except OSError as exc:
            raise AgentContractError(
                "Agent Manager backup destination could not be reserved."
            ) from exc
        os.close(descriptor)
        try:
            with self.connect() as source, sqlite3.connect(target) as output:
                source.backup(output)
        except Exception:
            target.unlink(missing_ok=True)
            raise
        return {
            "ok": True,
            "database": str(self.path),
            "backup": str(target.resolve()),
            "schema_version": self.schema_version,
        }


def build_agent_manager_server(
    *,
    host: str,
    port: int,
    database: Path,
    registry: str,
    authorization_env: str,
    policy_path: Path | None = None,
) -> ThreadingHTTPServer:
    store = AgentManagerStore(database, registry=registry)
    policy = (
        AgentManagerPolicy.load(policy_path)
        if policy_path is not None
        else AgentManagerPolicy.legacy(
            registry=store.registry,
            credential_env=str(authorization_env or "").strip(),
        )
    )
    if policy.registry != store.registry:
        raise AgentContractError(
            "Agent Manager policy registry does not match the service registry."
        )
    started = time.monotonic()

    class Handler(BaseHTTPRequestHandler):
        server_version = "BaldrAgentManager/1"

        def _request_id(self) -> str:
            current = getattr(self, "_baldr_request_id", "")
            if current:
                return str(current)
            supplied = str(self.headers.get("X-Request-ID") or "").strip()
            value = supplied if _REQUEST_ID.fullmatch(supplied) else uuid.uuid4().hex
            self._baldr_request_id = value
            return value

        def _json(self, payload: Mapping[str, Any], status: int = 200) -> None:
            body = json.dumps(dict(payload), ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Request-ID", self._request_id())
            self.end_headers()
            self.wfile.write(body)

        def _error(self, status: int, code: str, message: str) -> None:
            self._json({"ok": False, "error": {"code": code, "message": message}}, status)

        def _record(
            self,
            principal: AgentManagerPrincipal | None,
            *,
            action: str,
            status: int,
            outcome: str,
            detail_code: str,
            reference: AgentRef | None = None,
            tenant: str = "",
        ) -> int:
            if tenant or reference is not None:
                audit_tenants = (tenant or reference.namespace,)
            elif principal is not None and not principal.unrestricted_tenants:
                # A catalog/health/metrics decision can span every tenant in a
                # principal's scope. Emit one tenant-addressable event per
                # scope instead of creating a cross-tenant record that no
                # scoped auditor can retrieve.
                audit_tenants = principal.tenants
            else:
                audit_tenants = ("",)
            sequence = 0
            for audit_tenant in audit_tenants:
                sequence = store.audit(
                    request_id=self._request_id(),
                    principal_id=principal.identifier
                    if principal is not None
                    else "anonymous",
                    tenant=audit_tenant,
                    action=action,
                    reference=str(reference) if reference is not None else "",
                    outcome=outcome,
                    status_code=status,
                    detail_code=detail_code,
                )
            return sequence

        def _authenticate(self, *, action: str) -> AgentManagerPrincipal | None:
            principal = policy.authenticate(str(self.headers.get("Authorization") or ""))
            if principal is None:
                self._record(
                    None,
                    action=action,
                    status=401,
                    outcome="denied",
                    detail_code="authentication_required",
                )
                self._error(401, "unauthorized", "Authentication is required.")
            return principal

        def _authorize(
            self,
            *,
            action: str,
            reference: AgentRef | None = None,
        ) -> AgentManagerPrincipal | None:
            principal = self._authenticate(action=action)
            if principal is None:
                return None
            if not principal.permits(action):
                self._record(
                    principal,
                    action=action,
                    status=403,
                    outcome="denied",
                    detail_code="permission_denied",
                    reference=reference,
                )
                self._error(403, "forbidden", "The principal cannot perform this action.")
                return None
            if reference is not None and not principal.allows_tenant(reference.namespace):
                self._record(
                    principal,
                    action=action,
                    status=403,
                    outcome="denied",
                    detail_code="tenant_denied",
                    reference=reference,
                )
                self._error(403, "forbidden", "The principal cannot access this tenant.")
                return None
            return principal

        @staticmethod
        def _visible_tenants(principal: AgentManagerPrincipal) -> tuple[str, ...] | None:
            return None if principal.unrestricted_tenants else principal.tenants

        def _admin(
            self,
            result: Mapping[str, Any],
            *,
            principal: AgentManagerPrincipal,
            action: str,
            reference: AgentRef,
            status: int = 200,
        ) -> None:
            sequence = self._record(
                principal,
                action=action,
                status=status,
                outcome="allowed",
                detail_code="ok",
                reference=reference,
            )
            self._json(
                {
                    "contract": ADMIN_CONTRACT,
                    "version": AGENT_MANAGER_CONTRACT_VERSION,
                    "ok": True,
                    **dict(result),
                    "actor": principal.identifier,
                    "tenant": reference.namespace,
                    "request_id": self._request_id(),
                    "audit_sequence": sequence,
                },
                status,
            )

        def _body(self) -> dict[str, Any] | None:
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                self._body_error_status = 400
                self._error(400, "invalid_content_length", "Content-Length is invalid.")
                return None
            if length <= 0 or length > MAX_REQUEST_BYTES:
                self._body_error_status = 413
                self._error(413, "invalid_request_size", "Request body size is invalid.")
                return None
            try:
                value = json.loads(self.rfile.read(length).decode("utf-8"))
            except (UnicodeError, json.JSONDecodeError):
                self._body_error_status = 400
                self._error(400, "invalid_json", "Request body must be UTF-8 JSON.")
                return None
            if not isinstance(value, dict):
                self._body_error_status = 400
                self._error(400, "invalid_json", "Request body must be an object.")
                return None
            return value

        @staticmethod
        def _reference(path: str, suffix: str = "") -> AgentRef | None:
            prefix = "/v1/agents/"
            if not path.startswith(prefix) or (suffix and not path.endswith(suffix)):
                return None
            middle = path[len(prefix) : -len(suffix) if suffix else None]
            parts = [urllib.parse.unquote(item) for item in middle.split("/")]
            if len(parts) != 4 or parts[2] != "versions":
                return None
            try:
                return AgentRef.parse(
                    f"{store.registry}://{parts[0]}/{parts[1]}@{parts[3]}"
                )
            except AgentContractError:
                return None

        def do_GET(self) -> None:
            parsed = urllib.parse.urlsplit(self.path)
            if parsed.path == "/livez":
                self._json(
                    {
                        "contract": PROBE_CONTRACT,
                        "version": AGENT_MANAGER_CONTRACT_VERSION,
                        "probe": "live",
                        "status": "ok",
                    }
                )
                return
            if parsed.path == "/readyz":
                try:
                    ready = store.schema_version == AGENT_MANAGER_SCHEMA_VERSION
                except sqlite3.Error:
                    ready = False
                self._json(
                    {
                        "contract": PROBE_CONTRACT,
                        "version": AGENT_MANAGER_CONTRACT_VERSION,
                        "probe": "ready",
                        "status": "ok" if ready else "degraded",
                    },
                    200 if ready else 503,
                )
                return
            if parsed.path == "/v1/health":
                principal = self._authorize(action="health")
                if principal is None:
                    return
                health = store.health(tenants=self._visible_tenants(principal))
                self._record(
                    principal,
                    action="health",
                    status=200,
                    outcome="allowed",
                    detail_code="ok",
                )
                self._json(
                    {
                        "contract": HEALTH_CONTRACT,
                        "version": AGENT_MANAGER_CONTRACT_VERSION,
                        "status": "ok",
                        "service_version": __version__,
                        "registry": store.registry,
                        "schema_version": store.schema_version,
                        "policy_mode": policy.mode,
                        "principal": principal.identifier,
                        "uptime_seconds": int(time.monotonic() - started),
                        **health,
                    }
                )
                return
            if parsed.path == "/v1/agents":
                principal = self._authorize(action="catalog")
                if principal is None:
                    return
                query = urllib.parse.parse_qs(parsed.query)
                try:
                    limit = int((query.get("limit") or ["100"])[0])
                except ValueError:
                    self._record(
                        principal,
                        action="catalog",
                        status=400,
                        outcome="failed",
                        detail_code="invalid_limit",
                    )
                    self._error(400, "invalid_limit", "Catalog limit must be numeric.")
                    return
                agents = store.catalog(
                    limit=limit,
                    tenants=self._visible_tenants(principal),
                )
                self._record(
                    principal,
                    action="catalog",
                    status=200,
                    outcome="allowed",
                    detail_code="ok",
                )
                self._json(
                    {
                        "contract": CATALOG_CONTRACT,
                        "version": AGENT_MANAGER_CONTRACT_VERSION,
                        "agents": agents,
                    }
                )
                return
            if parsed.path == "/v1/audit":
                principal = self._authorize(action="audit")
                if principal is None:
                    return
                query = urllib.parse.parse_qs(parsed.query)
                try:
                    after = int((query.get("after") or ["0"])[0])
                    limit = int((query.get("limit") or ["100"])[0])
                except ValueError:
                    self._record(
                        principal,
                        action="audit",
                        status=400,
                        outcome="failed",
                        detail_code="invalid_cursor",
                    )
                    self._error(400, "invalid_cursor", "Audit cursor and limit must be numeric.")
                    return
                events = store.audit_events(
                    after=after,
                    limit=limit,
                    tenants=self._visible_tenants(principal),
                )
                self._record(
                    principal,
                    action="audit",
                    status=200,
                    outcome="allowed",
                    detail_code="ok",
                )
                self._json(
                    {
                        "contract": AUDIT_CONTRACT,
                        "version": AGENT_MANAGER_CONTRACT_VERSION,
                        "events": events,
                        "next_after": int(events[-1]["sequence"]) if events else max(0, after),
                    }
                )
                return
            if parsed.path == "/v1/metrics":
                principal = self._authorize(action="metrics")
                if principal is None:
                    return
                metrics = store.metrics(tenants=self._visible_tenants(principal))
                self._record(
                    principal,
                    action="metrics",
                    status=200,
                    outcome="allowed",
                    detail_code="ok",
                )
                self._json(
                    {
                        "contract": METRICS_CONTRACT,
                        "version": AGENT_MANAGER_CONTRACT_VERSION,
                        "registry": store.registry,
                        "schema_version": store.schema_version,
                        "uptime_seconds": int(time.monotonic() - started),
                        **metrics,
                    }
                )
                return
            reference = self._reference(parsed.path)
            principal = self._authorize(action="resolve", reference=reference)
            if principal is None:
                return
            manifest = store.resolve(reference) if reference is not None else None
            if manifest is None:
                self._record(
                    principal,
                    action="resolve",
                    status=404,
                    outcome="failed",
                    detail_code="agent_not_found",
                    reference=reference,
                )
                self._error(404, "agent_not_found", "Agent version is unavailable.")
                return
            self._record(
                principal,
                action="resolve",
                status=200,
                outcome="allowed",
                detail_code="ok",
                reference=reference,
            )
            self._json(
                {
                    "contract": RESOLUTION_CONTRACT,
                    "version": AGENT_MANAGER_CONTRACT_VERSION,
                    "manifest": manifest,
                }
            )

        def do_POST(self) -> None:
            parsed = urllib.parse.urlsplit(self.path)
            publish_route = parsed.path == "/v1/agents"
            reference: AgentRef | None = None
            action = "publish" if publish_route else "lifecycle"
            if not publish_route:
                for suffix in ("/enable", "/disable", "/revoke"):
                    reference = self._reference(parsed.path, suffix)
                    if reference is not None:
                        break
            principal = self._authorize(action=action, reference=reference)
            if principal is None:
                return
            body = self._body()
            if body is None:
                self._record(
                    principal,
                    action=action,
                    status=int(getattr(self, "_body_error_status", 400)),
                    outcome="failed",
                    detail_code="invalid_body",
                    reference=reference,
                )
                return
            try:
                if publish_route:
                    raw = body.get("manifest")
                    if not isinstance(raw, Mapping):
                        raise AgentContractError("Publish request requires manifest.")
                    manifest = AgentManifest.from_dict(raw)
                    reference = manifest.reference
                    if not principal.allows_tenant(reference.namespace):
                        self._record(
                            principal,
                            action=action,
                            status=403,
                            outcome="denied",
                            detail_code="tenant_denied",
                            reference=reference,
                        )
                        self._error(403, "forbidden", "The principal cannot publish to this tenant.")
                        return
                    if not principal.allows_owner(manifest.owner):
                        self._record(
                            principal,
                            action=action,
                            status=403,
                            outcome="denied",
                            detail_code="owner_denied",
                            reference=reference,
                        )
                        self._error(403, "forbidden", "The principal cannot publish for this owner.")
                        return
                    result = store.publish(manifest)
                    self._admin(
                        result,
                        principal=principal,
                        action=action,
                        reference=reference,
                        status=201 if result["created"] else 200,
                    )
                    return
                for action, enabled in (("/enable", True), ("/disable", False)):
                    candidate = self._reference(parsed.path, action)
                    if candidate is not None:
                        self._admin(
                            store.set_enabled(candidate, enabled=enabled),
                            principal=principal,
                            action="lifecycle",
                            reference=candidate,
                        )
                        return
                candidate = self._reference(parsed.path, "/revoke")
                if candidate is not None:
                    self._admin(
                        store.revoke(candidate),
                        principal=principal,
                        action="lifecycle",
                        reference=candidate,
                    )
                    return
            except KeyError:
                self._record(
                    principal,
                    action=action,
                    status=404,
                    outcome="failed",
                    detail_code="agent_not_found",
                    reference=reference,
                )
                self._error(404, "agent_not_found", "Agent version was not found.")
                return
            except AgentContractError as exc:
                self._record(
                    principal,
                    action=action,
                    status=409,
                    outcome="failed",
                    detail_code="agent_contract_error",
                    reference=reference,
                )
                self._error(409, "agent_contract_error", str(exc))
                return
            self._record(
                principal,
                action=action,
                status=404,
                outcome="failed",
                detail_code="route_not_found",
                reference=reference,
            )
            self._error(404, "route_not_found", "Route was not found.")

        def log_message(self, format: str, *args: Any) -> None:
            del format, args

    server = ThreadingHTTPServer((host, port), Handler)
    server.daemon_threads = True
    return server


def serve_agent_manager(
    *,
    host: str,
    port: int,
    database: Path,
    registry: str,
    authorization_env: str,
    policy_path: Path | None = None,
) -> None:
    server = build_agent_manager_server(
        host=host,
        port=port,
        database=database,
        registry=registry,
        authorization_env=authorization_env,
        policy_path=policy_path,
    )
    try:
        server.serve_forever()
    finally:
        server.server_close()

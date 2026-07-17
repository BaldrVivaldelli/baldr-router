from __future__ import annotations

import hmac
import json
import os
import re
import sqlite3
import urllib.parse
from collections.abc import Mapping
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from . import __version__
from .agent_api import AgentContractError, AgentManifest, AgentRef
from .agent_manager import (
    AGENT_MANAGER_CONTRACT_VERSION,
    ADMIN_CONTRACT,
    CATALOG_CONTRACT,
    HEALTH_CONTRACT,
    RESOLUTION_CONTRACT,
)

MAX_REQUEST_BYTES = 2 * 1024 * 1024
_ENV_NAME = re.compile(r"^[A-Z_][A-Z0-9_]{0,127}$")


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
            connection.executescript(
                """
                PRAGMA journal_mode=WAL;
                PRAGMA foreign_keys=ON;
                CREATE TABLE IF NOT EXISTS agent_manifests (
                    reference TEXT PRIMARY KEY,
                    digest TEXT NOT NULL,
                    manifest_json TEXT NOT NULL,
                    enabled INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
                    revoked INTEGER NOT NULL DEFAULT 0 CHECK (revoked IN (0, 1)),
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                """
            )
        try:
            self.path.chmod(0o600)
        except OSError:
            pass

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=5)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=5000")
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
                INSERT INTO agent_manifests(reference, digest, manifest_json)
                VALUES (?, ?, ?)
                """,
                (reference, manifest.digest, payload),
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

    def catalog(self, *, limit: int) -> list[dict[str, Any]]:
        bounded = max(1, min(int(limit), 1000))
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT manifest_json FROM agent_manifests
                WHERE enabled = 1 AND revoked = 0
                ORDER BY reference LIMIT ?
                """,
                (bounded,),
            ).fetchall()
        return [json.loads(str(row["manifest_json"])) for row in rows]

    def health(self) -> dict[str, Any]:
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT
                    COUNT(*) AS total,
                    SUM(CASE WHEN enabled = 1 AND revoked = 0 THEN 1 ELSE 0 END) AS enabled,
                    SUM(CASE WHEN revoked = 1 THEN 1 ELSE 0 END) AS revoked
                FROM agent_manifests
                """
            ).fetchone()
        return {
            "total": int(row["total"] or 0),
            "enabled": int(row["enabled"] or 0),
            "revoked": int(row["revoked"] or 0),
        }


def build_agent_manager_server(
    *,
    host: str,
    port: int,
    database: Path,
    registry: str,
    authorization_env: str,
) -> ThreadingHTTPServer:
    normalized_authorization_env = str(authorization_env or "").strip()
    if not _ENV_NAME.fullmatch(normalized_authorization_env):
        raise AgentContractError(
            "Agent Manager authorization_env must name an uppercase environment variable."
        )
    expected = os.environ.get(normalized_authorization_env, "").strip()
    if not expected:
        raise AgentContractError(
            "Agent Manager credential environment variable "
            f"{normalized_authorization_env!r} is unavailable."
        )
    store = AgentManagerStore(database, registry=registry)
    expected_header = expected if " " in expected else f"Bearer {expected}"

    class Handler(BaseHTTPRequestHandler):
        server_version = "BaldrAgentManager/1"

        def _authorized(self) -> bool:
            supplied = str(self.headers.get("Authorization") or "")
            return hmac.compare_digest(supplied, expected_header)

        def _json(self, payload: Mapping[str, Any], status: int = 200) -> None:
            body = json.dumps(dict(payload), ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _error(self, status: int, code: str, message: str) -> None:
            self._json({"ok": False, "error": {"code": code, "message": message}}, status)

        def _admin(self, result: Mapping[str, Any], status: int = 200) -> None:
            self._json(
                {
                    "contract": ADMIN_CONTRACT,
                    "version": AGENT_MANAGER_CONTRACT_VERSION,
                    "ok": True,
                    **dict(result),
                },
                status,
            )

        def _body(self) -> dict[str, Any] | None:
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                self._error(400, "invalid_content_length", "Content-Length is invalid.")
                return None
            if length <= 0 or length > MAX_REQUEST_BYTES:
                self._error(413, "invalid_request_size", "Request body size is invalid.")
                return None
            try:
                value = json.loads(self.rfile.read(length).decode("utf-8"))
            except (UnicodeError, json.JSONDecodeError):
                self._error(400, "invalid_json", "Request body must be UTF-8 JSON.")
                return None
            if not isinstance(value, dict):
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
            if not self._authorized():
                self._error(401, "unauthorized", "Authentication is required.")
                return
            parsed = urllib.parse.urlsplit(self.path)
            if parsed.path == "/v1/health":
                self._json(
                    {
                        "contract": HEALTH_CONTRACT,
                        "version": AGENT_MANAGER_CONTRACT_VERSION,
                        "status": "ok",
                        "service_version": __version__,
                        "registry": store.registry,
                        **store.health(),
                    }
                )
                return
            if parsed.path == "/v1/agents":
                query = urllib.parse.parse_qs(parsed.query)
                try:
                    limit = int((query.get("limit") or ["100"])[0])
                except ValueError:
                    self._error(400, "invalid_limit", "Catalog limit must be numeric.")
                    return
                self._json(
                    {
                        "contract": CATALOG_CONTRACT,
                        "version": AGENT_MANAGER_CONTRACT_VERSION,
                        "agents": store.catalog(limit=limit),
                    }
                )
                return
            reference = self._reference(parsed.path)
            manifest = store.resolve(reference) if reference is not None else None
            if manifest is None:
                self._error(404, "agent_not_found", "Agent version is unavailable.")
                return
            self._json(
                {
                    "contract": RESOLUTION_CONTRACT,
                    "version": AGENT_MANAGER_CONTRACT_VERSION,
                    "manifest": manifest,
                }
            )

        def do_POST(self) -> None:
            if not self._authorized():
                self._error(401, "unauthorized", "Authentication is required.")
                return
            parsed = urllib.parse.urlsplit(self.path)
            body = self._body()
            if body is None:
                return
            try:
                if parsed.path == "/v1/agents":
                    raw = body.get("manifest")
                    if not isinstance(raw, Mapping):
                        raise AgentContractError("Publish request requires manifest.")
                    result = store.publish(AgentManifest.from_dict(raw))
                    self._admin(result, 201 if result["created"] else 200)
                    return
                for action, enabled in (("/enable", True), ("/disable", False)):
                    reference = self._reference(parsed.path, action)
                    if reference is not None:
                        self._admin(store.set_enabled(reference, enabled=enabled))
                        return
                reference = self._reference(parsed.path, "/revoke")
                if reference is not None:
                    self._admin(store.revoke(reference))
                    return
            except KeyError:
                self._error(404, "agent_not_found", "Agent version was not found.")
                return
            except AgentContractError as exc:
                self._error(409, "agent_contract_error", str(exc))
                return
            self._error(404, "route_not_found", "Route was not found.")

        def log_message(self, format: str, *args: Any) -> None:
            del format, args

    return ThreadingHTTPServer((host, port), Handler)


def serve_agent_manager(
    *,
    host: str,
    port: int,
    database: Path,
    registry: str,
    authorization_env: str,
) -> None:
    server = build_agent_manager_server(
        host=host,
        port=port,
        database=database,
        registry=registry,
        authorization_env=authorization_env,
    )
    try:
        server.serve_forever()
    finally:
        server.server_close()

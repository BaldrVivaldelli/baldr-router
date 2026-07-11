from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from baldr_router import __version__
from baldr_router.evidence import sanitize_evidence
from baldr_router.telemetry import app_state_dir


RECEIPT_SCHEMA_VERSION = 1


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def receipts_root() -> Path:
    root = app_state_dir() / "client-receipts"
    root.mkdir(parents=True, exist_ok=True)
    try:
        root.chmod(0o700)
    except OSError:
        pass
    return root


def _safe_client(client: str) -> str:
    return re.sub(r"[^a-z0-9._-]+", "-", client.lower()).strip("-") or "client"


def record_client_receipt(
    *,
    client: str,
    client_version: str = "",
    facts: dict[str, Any] | None = None,
) -> dict[str, Any]:
    safe_client = _safe_client(client)
    payload = sanitize_evidence(
        {
            "ok": True,
            "schema_version": RECEIPT_SCHEMA_VERSION,
            "client": safe_client,
            "client_version": client_version,
            "baldr_version": __version__,
            "recorded_at": _utc_now(),
            "facts": facts or {},
        }
    )
    target = receipts_root() / f"{safe_client}.json"
    temporary = target.with_suffix(".json.tmp")
    temporary.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(target)
    try:
        target.chmod(0o600)
    except OSError:
        pass
    return {**payload, "path": str(target)}


def load_client_receipt(client: str) -> dict[str, Any] | None:
    path = receipts_root() / f"{_safe_client(client)}.json"
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    if not isinstance(value, dict):
        return None
    return {**value, "path": str(path)}


def latest_client_receipt(*, family: str | None = None) -> dict[str, Any]:
    candidates: list[dict[str, Any]] = []
    for path in receipts_root().glob("*.json"):
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if not isinstance(value, dict):
            continue
        client = str(value.get("client") or "")
        if family and family.lower() not in client.lower():
            continue
        candidates.append({**value, "path": str(path)})
    candidates.sort(key=lambda item: str(item.get("recorded_at") or ""), reverse=True)
    return {
        "ok": True,
        "available": bool(candidates),
        "receipt": candidates[0] if candidates else None,
        "count": len(candidates),
    }

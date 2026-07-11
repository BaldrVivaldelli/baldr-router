from __future__ import annotations

import json
import os
import re
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from baldr_router import __version__
from baldr_router.redaction import redact_value
from baldr_router.telemetry import app_state_dir

from baldr_router.discovery.fingerprint import file_sha256, stable_json_hash

SCHEMA_VERSION = 1


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def evidence_root() -> Path:
    return app_state_dir() / "evidence"


def _safe_id(kind: str, seed: dict[str, Any]) -> str:
    now = _utc_now()
    digest = stable_json_hash({"kind": kind, "now": now.isoformat(), "seed": seed})[:10]
    safe_kind = re.sub(r"[^a-z0-9-]+", "-", kind.lower()).strip("-") or "verification"
    return f"br-{safe_kind}-{now.strftime('%Y%m%dT%H%M%SZ')}-{digest}"


def _replace_home(value: Any) -> Any:
    homes = {
        str(Path.home().resolve()),
        os.environ.get("USERPROFILE", ""),
        os.environ.get("HOME", ""),
    }
    homes = {item.rstrip("/\\") for item in homes if item}
    if isinstance(value, str):
        text = value
        for home in sorted(homes, key=len, reverse=True):
            text = text.replace(home, "~")
        return text
    if isinstance(value, dict):
        return {str(key): _replace_home(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_replace_home(item) for item in value]
    if isinstance(value, tuple):
        return tuple(_replace_home(item) for item in value)
    return value


def sanitize_evidence(value: Any) -> Any:
    return _replace_home(redact_value(value))


def _write_json(path: Path, value: Any) -> None:
    path.write_text(
        json.dumps(sanitize_evidence(value), indent=2, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _summary_markdown(bundle: dict[str, Any]) -> str:
    scenarios = bundle.get("lifecycle", {}).get("scenarios", [])
    passed = sum(1 for item in scenarios if item.get("status") == "passed")
    skipped = sum(1 for item in scenarios if item.get("status") == "skipped")
    failed = sum(1 for item in scenarios if item.get("status") == "failed")
    lines = [
        "# Baldr Router verification evidence",
        "",
        f"- **Evidence ID:** `{bundle.get('evidence_id')}`",
        f"- **Baldr version:** `{bundle.get('baldr_version')}`",
        f"- **Generated at:** `{bundle.get('generated_at')}`",
        f"- **Result:** {'PASS' if bundle.get('ok') else 'FAIL'}",
        f"- **Scenarios:** {passed} passed, {skipped} skipped, {failed} failed",
        "",
        "## Lifecycle scenarios",
        "",
        "| Scenario | Status | Duration |",
        "|---|---:|---:|",
    ]
    for item in scenarios:
        duration = item.get("duration_ms")
        duration_text = f"{duration} ms" if isinstance(duration, int) else "—"
        lines.append(f"| `{item.get('id')}` | {item.get('status')} | {duration_text} |")
    lines += [
        "",
        "## Privacy",
        "",
        "This bundle is redacted. API keys, token-shaped values, passwords, and the current home path are not included.",
    ]
    return "\n".join(lines) + "\n"


def create_evidence_bundle(
    *,
    kind: str,
    environment: dict[str, Any],
    lifecycle: dict[str, Any],
    workspace_profile: dict[str, Any] | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    seed = {
        "environment_fingerprint": environment.get("fingerprint"),
        "lifecycle": lifecycle.get("run_id") or lifecycle.get("series_id"),
    }
    evidence_id = _safe_id(kind, seed)
    root = evidence_root() / evidence_id
    root.mkdir(parents=True, exist_ok=False)

    generated_at = _utc_now().isoformat()
    manifest = {
        "ok": bool(lifecycle.get("ok")),
        "schema_version": SCHEMA_VERSION,
        "evidence_id": evidence_id,
        "kind": kind,
        "baldr_version": __version__,
        "generated_at": generated_at,
        "environment_fingerprint": environment.get("fingerprint"),
        "workspace_fingerprint": (workspace_profile or {}).get("fingerprint"),
        "metadata": metadata or {},
    }
    _write_json(root / "environment.json", environment)
    _write_json(root / "lifecycle-results.json", lifecycle)
    if workspace_profile is not None:
        _write_json(root / "workspace-profile.json", workspace_profile)
    _write_json(root / "manifest.json", manifest)

    redaction_report = {
        "ok": True,
        "secret_patterns_redacted": True,
        "home_paths_normalized": True,
        "workspace_source_included": False,
        "raw_prompts_included": False,
    }
    _write_json(root / "redaction-report.json", redaction_report)

    bundle = {**manifest, "lifecycle": lifecycle}
    (root / "summary.md").write_text(_summary_markdown(bundle), encoding="utf-8")

    artifacts: dict[str, dict[str, Any]] = {}
    for path in sorted(root.iterdir()):
        if path.is_file() and path.name != "artifact-hashes.json":
            artifacts[path.name] = {"sha256": file_sha256(path), "bytes": path.stat().st_size}
    _write_json(root / "artifact-hashes.json", artifacts)
    return {
        "ok": bool(manifest["ok"]),
        "evidence_id": evidence_id,
        "path": str(root),
        "summary_path": str(root / "summary.md"),
        "manifest": manifest,
    }


def _load_manifest(directory: Path) -> dict[str, Any] | None:
    path = directory / "manifest.json"
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    if not isinstance(value, dict):
        return None
    value["path"] = str(directory)
    return value


def list_evidence(limit: int = 20) -> dict[str, Any]:
    root = evidence_root()
    if not root.exists():
        return {"ok": True, "path": str(root), "count": 0, "items": []}
    items: list[dict[str, Any]] = []
    for directory in root.iterdir():
        if not directory.is_dir():
            continue
        manifest = _load_manifest(directory)
        if manifest:
            items.append(manifest)
    items.sort(key=lambda item: str(item.get("generated_at") or ""), reverse=True)
    return {
        "ok": True,
        "path": str(root),
        "count": len(items),
        "items": items[: max(1, min(limit, 100))],
    }


def latest_evidence(*, kind: str | None = None, successful_only: bool = False) -> dict[str, Any]:
    items = list_evidence(limit=100)["items"]
    for item in items:
        if kind and item.get("kind") != kind:
            continue
        if successful_only and item.get("ok") is not True:
            continue
        return {"ok": True, "available": True, "evidence": item}
    return {"ok": True, "available": False, "evidence": None, "path": str(evidence_root())}


def evidence_is_current(
    *,
    environment_fingerprint: str,
    kind: str = "lifecycle",
    max_age_hours: int = 24,
) -> bool:
    latest = latest_evidence(kind=kind, successful_only=True)
    item = latest.get("evidence") or {}
    if not item or item.get("environment_fingerprint") != environment_fingerprint:
        return False
    raw = item.get("generated_at")
    try:
        generated = datetime.fromisoformat(str(raw))
    except Exception:
        return False
    if generated.tzinfo is None:
        generated = generated.replace(tzinfo=timezone.utc)
    return _utc_now() - generated <= timedelta(hours=max_age_hours)


def cleanup_evidence(*, retention_days: int) -> dict[str, Any]:
    root = evidence_root()
    if not root.exists():
        return {"ok": True, "removed": [], "path": str(root)}
    cutoff = _utc_now() - timedelta(days=max(1, retention_days))
    removed: list[str] = []
    for directory in root.iterdir():
        if not directory.is_dir():
            continue
        manifest = _load_manifest(directory) or {}
        try:
            generated = datetime.fromisoformat(str(manifest.get("generated_at")))
        except Exception:
            generated = datetime.fromtimestamp(directory.stat().st_mtime, tz=timezone.utc)
        if generated.tzinfo is None:
            generated = generated.replace(tzinfo=timezone.utc)
        if generated < cutoff:
            shutil.rmtree(directory, ignore_errors=True)
            removed.append(directory.name)
    return {"ok": True, "removed": removed, "path": str(root)}

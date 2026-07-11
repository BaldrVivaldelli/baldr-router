from __future__ import annotations

import json
import os
import platform
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from baldr_router.config import load_config
from baldr_router.discovery.environment_probe import environment_probe
from baldr_router.evidence import create_evidence_bundle
from baldr_router.validation.lifecycle import run_lifecycle_verification


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def current_profile() -> str:
    client = os.environ.get("BALDR_CLIENT_ID", "").lower()
    is_wsl = bool(os.environ.get("WSL_DISTRO_NAME") or os.environ.get("WSL_INTEROP"))
    if "kiro" in client:
        return "kiro"
    if "vscode" in client and is_wsl:
        return "vscode-remote-wsl"
    if platform.system().lower() == "windows" and "vscode" in client:
        return "vscode-windows"
    if is_wsl:
        return "wsl"
    return f"{platform.system().lower()}-native"


def run_lab_matrix(
    *,
    repeat: int | None = None,
    mode: str = "quick",
    workspace_root: str | None = None,
    include_provider_smoke: bool = False,
    profile: str | None = None,
) -> dict[str, Any]:
    cfg = load_config()
    repeats = max(1, min(int(repeat or cfg.verification.required_consecutive_passes), 20))
    series_id = f"br-lab-{uuid.uuid4().hex[:12]}"
    started_at = _utc_now()
    environment = environment_probe()
    runs: list[dict[str, Any]] = []
    for index in range(repeats):
        result = run_lifecycle_verification(
            mode=mode,
            workspace_root=workspace_root,
            include_provider_smoke=include_provider_smoke,
            client_id=os.environ.get("BALDR_CLIENT_ID") or "baldr-lab",
            write_evidence=False,
        )
        runs.append(
            {
                "iteration": index + 1,
                "ok": result.get("ok"),
                "run_id": result.get("run_id"),
                "duration_ms": result.get("duration_ms"),
                "passed": result.get("passed"),
                "skipped": result.get("skipped"),
                "failed": result.get("failed"),
                "scenarios": result.get("scenarios"),
            }
        )
    consecutive_passes = 0
    for item in runs:
        if item.get("ok") is True:
            consecutive_passes += 1
        else:
            consecutive_passes = 0
    result: dict[str, Any] = {
        "ok": all(item.get("ok") is True for item in runs),
        "schema_version": 1,
        "series_id": series_id,
        "profile": profile or current_profile(),
        "mode": mode,
        "started_at": started_at,
        "completed_at": _utc_now(),
        "repeat": repeats,
        "required_consecutive_passes": cfg.verification.required_consecutive_passes,
        "consecutive_passes": consecutive_passes,
        "acceptance_met": consecutive_passes >= cfg.verification.required_consecutive_passes,
        "environment_fingerprint": environment.get("fingerprint"),
        "runs": runs,
    }
    result["evidence"] = create_evidence_bundle(
        kind="lab",
        environment=environment,
        lifecycle=result,
        metadata={
            "profile": result["profile"],
            "repeat": repeats,
            "acceptance_met": result["acceptance_met"],
        },
    )
    return result


def load_matrix_definition(path: str | Path) -> dict[str, Any]:
    value = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("Lab matrix root must be an object")
    return value

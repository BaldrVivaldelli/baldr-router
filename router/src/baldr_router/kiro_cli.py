from __future__ import annotations

import json
import os
import shutil
import time
from pathlib import Path
from typing import Any

from .config import load_config
from .run import run_command
from .telemetry import append_run, utc_now_iso


def kiro_cli_found(command: str | None = None) -> str | None:
    cfg = load_config()
    return shutil.which(command or cfg.kiro_cli.command)


def kiro_cli_status() -> dict[str, Any]:
    cfg = load_config()
    path = kiro_cli_found(cfg.kiro_cli.command)
    api_key_available = bool(os.environ.get(cfg.kiro_cli.api_key_env))
    result: dict[str, Any] = {
        "enabled": cfg.kiro_cli.enabled,
        "command": cfg.kiro_cli.command,
        "found": bool(path),
        "path": path,
        "api_key_env": cfg.kiro_cli.api_key_env,
        "api_key_available": api_key_available,
        "default_agent": cfg.kiro_cli.default_agent,
        "default_effort": cfg.kiro_cli.default_effort,
    }
    if not path:
        result["ok"] = False
        result["reason"] = (
            "kiro-cli command not found. Install/configure Kiro CLI or disable the kiro-cli provider."
        )
    elif cfg.kiro_cli.require_api_key and not api_key_available:
        result["ok"] = False
        result["reason"] = (
            f"{cfg.kiro_cli.api_key_env} is not available to this process. Headless kiro-cli usually needs it."
        )
    else:
        result["ok"] = True
    return result


def _try_parse_json(text: str) -> Any | None:
    cleaned = text.strip()
    if not cleaned:
        return None
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        # Some CLIs print prose around JSON. Keep the parser conservative.
        return None


def run_kiro_role_prompt(
    *,
    cwd: Path,
    prompt: str,
    role: str,
    workflow: str,
    agent: str | None = None,
    effort: str | None = None,
    can_write: bool = False,
    report_kind: str = "review",
    extra_env: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Run a role prompt through Kiro CLI in no-interactive mode.

    This provider is intentionally optional. It is useful when baldr-router is
    called from VS Code or another MCP client and Kiro is used as an external
    planner/reviewer. When Kiro itself is the client, use it sparingly to avoid
    double Kiro usage and recursive agent loops.
    """
    cfg = load_config()
    if not cfg.kiro_cli.enabled:
        return {
            "ok": False,
            "provider": "kiro-cli",
            "reason": "kiro-cli provider is disabled. Enable it with `baldr-router enable-kiro-cli`.",
        }
    path = kiro_cli_found(cfg.kiro_cli.command)
    if not path:
        return {
            "ok": False,
            "provider": "kiro-cli",
            "reason": f"{cfg.kiro_cli.command!r} was not found on PATH.",
        }

    env = os.environ.copy()
    if extra_env:
        env.update(extra_env)

    selected_agent = agent or cfg.kiro_cli.default_agent
    selected_effort = effort or cfg.kiro_cli.default_effort
    cmd = [cfg.kiro_cli.command, "chat", "--no-interactive"]
    if selected_agent:
        cmd.extend(["--agent", selected_agent])
    if selected_effort:
        cmd.extend(["--effort", selected_effort])
    cmd.append(prompt)

    started = time.time()
    started_at = utc_now_iso()
    result = run_command(
        cmd,
        cwd=cwd,
        env=env,
        timeout=cfg.kiro_cli.timeout_seconds,
        stdout_limit=30000,
        stderr_limit=12000,
    )
    duration_ms = int((time.time() - started) * 1000)
    final_json = _try_parse_json(result.get("stdout", ""))
    final_report = (
        final_json
        if isinstance(final_json, dict)
        else {
            "status": "reviewed" if report_kind == "review" else "partial",
            "summary": (result.get("stdout") or "").strip()[:4000],
            "files_modified": [],
            "commands_run": [],
            "tests_run": [],
            "verification_needed": [],
            "risks": [],
            "follow_up": [],
        }
    )

    out: dict[str, Any] = {
        **result,
        "provider": "kiro-cli",
        "runner": "kiro-cli",
        "role": role,
        "workflow": workflow,
        "agent": selected_agent,
        "effort": selected_effort,
        "can_write": can_write,
        "started_at": started_at,
        "duration_ms": duration_ms,
        "final_report": final_report,
    }
    if cfg.telemetry.enabled:
        record = {
            "run_id": f"kiro-cli-{int(started * 1000)}",
            "ok": out.get("ok") is True,
            "provider": "kiro-cli",
            "runner": "kiro-cli",
            "role": role,
            "workflow": workflow,
            "agent": selected_agent,
            "effort": selected_effort,
            "started_at": started_at,
            "duration_ms": duration_ms,
            "cwd": str(cwd),
            "report_kind": report_kind,
            "final_status": final_report.get("status")
            if isinstance(final_report, dict)
            else None,
        }
        out["telemetry_path"] = str(append_run(record))
    return out

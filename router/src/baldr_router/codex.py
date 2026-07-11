from __future__ import annotations

import os
import shutil
import time
from pathlib import Path
from typing import Any

from .codex_app_server import run_codex_app_server
from .codex_exec_json import run_codex_exec_json
from .codex_sdk import run_codex_sdk
from .config import load_config
from .provider_errors import provider_error
from .run import run_command
from .secrets import read_context7_api_key


def codex_found() -> str | None:
    return shutil.which("codex")


_LOGIN_CACHE: tuple[float, dict[str, Any]] | None = None
_LOGIN_CACHE_SECONDS = 20.0


def reset_codex_login_cache() -> None:
    global _LOGIN_CACHE
    _LOGIN_CACHE = None


def codex_preflight(*, force: bool = False) -> dict[str, Any]:
    global _LOGIN_CACHE
    if not codex_found():
        return provider_error(
            "codex_not_found",
            "Codex CLI was not found. Install Codex CLI and run `codex login`.",
            provider="codex",
        )
    if os.environ.get("BALDR_SKIP_CODEX_LOGIN_CHECK") == "1":
        return {"ok": True, "skipped": True}
    now = time.monotonic()
    if not force and _LOGIN_CACHE and now - _LOGIN_CACHE[0] < _LOGIN_CACHE_SECONDS:
        return dict(_LOGIN_CACHE[1])
    status = codex_login_status()
    if status.get("ok"):
        result = {"ok": True, "login_status": status}
    else:
        result = provider_error(
            "codex_not_authenticated",
            "Codex is not authenticated. Run `codex login` and choose ChatGPT sign-in.",
            provider="codex",
            details={
                "exit_code": status.get("exit_code"),
                "stderr": status.get("stderr"),
            },
        )
    _LOGIN_CACHE = (now, result)
    return dict(result)


def npx_found() -> str | None:
    return shutil.which("npx")


def codex_login_status() -> dict[str, Any]:
    if not codex_found():
        return {"ok": False, "reason": "codex command not found"}
    return run_command(["codex", "login", "status"], timeout=20)


def codex_version() -> dict[str, Any]:
    if not codex_found():
        return {"ok": False, "reason": "codex command not found"}
    return run_command(["codex", "--version"], timeout=20)


def _codex_env(extra_env: dict[str, str] | None = None) -> dict[str, str]:
    cfg = load_config()
    env = os.environ.copy()
    if cfg.context7.enabled and cfg.context7.mode in {"codex-mcp", "hybrid"}:
        key = read_context7_api_key(cfg.context7.api_key_source)
        if key:
            env["CONTEXT7_API_KEY"] = key
    if extra_env:
        env.update(extra_env)
    return env


def build_codex_exec_command(
    *,
    workspace_root: Path,
    sandbox: str,
    approval_policy: str,
    model: str = "",
    reasoning_effort: str = "",
    skip_git_repo_check: bool,
) -> list[str]:
    cmd = [
        "codex",
        "--ask-for-approval",
        approval_policy,
        "exec",
        "-C",
        str(workspace_root),
        "--color",
        "never",
        "--sandbox",
        sandbox,
    ]
    if model:
        cmd += ["--model", model]
    if reasoning_effort:
        cmd += ["-c", f'model_reasoning_effort="{reasoning_effort}"']
    if skip_git_repo_check:
        cmd.append("--skip-git-repo-check")
    cmd.append("-")
    return cmd


def _run_codex_prompt(
    *,
    cwd: Path,
    prompt: str,
    sandbox: str,
    approval_policy: str,
    report_kind: str,
    model: str = "",
    reasoning_effort: str = "",
    runner: str = "",
    session_scope: str = "",
    session_key: str = "",
    resume_session_id: str | None = None,
    extra_env: dict[str, str] | None = None,
) -> dict[str, Any]:
    cfg = load_config()
    selected_runner = runner or cfg.codex.runner
    selected_model = model or cfg.codex.model
    selected_reasoning = reasoning_effort or cfg.codex.reasoning_effort
    selected_scope = session_scope or cfg.codex.session_scope

    if selected_runner != "sdk":
        preflight = codex_preflight()
        if not preflight.get("ok"):
            return preflight

    env = _codex_env(extra_env)
    if selected_runner == "exec-json":
        cmd = build_codex_exec_command(
            workspace_root=cwd,
            sandbox=sandbox,
            approval_policy=approval_policy,
            model=selected_model,
            reasoning_effort=selected_reasoning,
            skip_git_repo_check=cfg.codex.skip_git_repo_check,
        )
        result = run_codex_exec_json(
            cmd,
            cwd=cwd,
            stdin=prompt,
            env=env,
            timeout=cfg.codex.timeout_seconds,
            report_kind=report_kind,
            telemetry_enabled=cfg.telemetry.enabled,
            keep_raw_events=cfg.telemetry.keep_raw_events,
            max_events_returned=cfg.telemetry.max_events_returned,
        )
    elif selected_runner == "app-server":
        result = run_codex_app_server(
            prompt=prompt,
            cwd=cwd,
            model=selected_model,
            sandbox=sandbox,
            timeout=cfg.codex.timeout_seconds,
            session_scope=selected_scope,
            session_key=session_key or None,
            resume_thread_id=resume_session_id,
            env=env,
            telemetry_enabled=cfg.telemetry.enabled,
            report_kind=report_kind,
        )
    elif selected_runner == "sdk":
        result = run_codex_sdk(
            prompt=prompt,
            cwd=cwd,
            model=selected_model,
            sandbox=sandbox,
            timeout=cfg.codex.timeout_seconds,
            session_scope=selected_scope,
            session_key=session_key or None,
            resume_thread_id=resume_session_id,
            env=env,
            telemetry_enabled=cfg.telemetry.enabled,
            report_kind=report_kind,
        )
    else:
        return {
            "ok": False,
            "reason": (
                f"Unknown Codex runner {selected_runner!r}. "
                "Use one of: exec-json, app-server, sdk."
            ),
        }
    result.setdefault("execution", {})
    result["execution"].update(
        {
            "model": selected_model or None,
            "reasoning_effort": selected_reasoning or None,
            "runner": selected_runner,
            "session_scope": selected_scope,
            "session_key": session_key or None,
        }
    )
    return result


def run_codex_role_prompt(
    *,
    cwd: Path,
    prompt: str,
    role: str,
    workflow: str,
    can_write: bool,
    sandbox: str | None = None,
    report_kind: str,
    model: str = "",
    reasoning_effort: str = "",
    runner: str = "",
    session_scope: str = "",
    session_key: str = "",
    resume_session_id: str | None = None,
    extra_env: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Run one generic role step through the Codex provider."""
    cfg = load_config()
    selected_sandbox = sandbox or (cfg.codex.sandbox if can_write else "read-only")
    if not can_write:
        selected_sandbox = "read-only"
    result = _run_codex_prompt(
        cwd=cwd,
        prompt=prompt,
        sandbox=selected_sandbox,
        approval_policy=cfg.codex.approval_policy if can_write else "never",
        report_kind=report_kind,
        model=model,
        reasoning_effort=reasoning_effort,
        runner=runner,
        session_scope=session_scope,
        session_key=session_key,
        resume_session_id=resume_session_id,
        extra_env=extra_env,
    )
    result["role"] = role
    result["workflow"] = workflow
    result["requested_sandbox"] = selected_sandbox
    return result

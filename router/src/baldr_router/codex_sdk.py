from __future__ import annotations

import atexit
import json
import os
import uuid
import threading
from pathlib import Path
from typing import Any, Mapping

from .provider_errors import provider_error
from .provider_activity import ProviderActivitySink, emit_provider_activity
from .redaction import redact_text, redact_value
from .schemas import normalize_final_report, validate_final_report
from .telemetry import append_run, utc_now_iso


_CODEX: Any | None = None
_THREADS: dict[str, Any] = {}
_SDK_LOCK = threading.RLock()


def _session_key(cwd: Path, scope: str, explicit: str | None = None) -> str:
    if explicit:
        return explicit
    if scope == "global":
        return "global"
    if scope == "task":
        return f"task:{uuid.uuid4().hex}"
    if scope == "workflow":
        return f"workflow:{uuid.uuid4().hex}"
    return f"workspace:{cwd}"


def _sandbox_value(sandbox: str) -> Any:
    from openai_codex import Sandbox  # type: ignore

    mapping = {
        "read-only": Sandbox.read_only,
        "workspace-write": Sandbox.workspace_write,
        "danger-full-access": Sandbox.full_access,
    }
    return mapping.get(sandbox, Sandbox.workspace_write)


def _ensure_codex() -> Any:
    global _CODEX
    if _CODEX is None:
        from openai_codex import Codex  # type: ignore

        _CODEX = Codex()
        # SDK examples use a context manager. Keep one open while the MCP server lives.
        if hasattr(_CODEX, "__enter__"):
            _CODEX.__enter__()
    return _CODEX


def _try_parse_json(text: str) -> Any:
    if not text:
        return None
    t = text.strip()
    if t.startswith("```"):
        t = t.strip("`").removeprefix("json").strip()
    try:
        return json.loads(t)
    except Exception:
        return None


def run_codex_sdk(
    *,
    prompt: str,
    cwd: Path,
    model: str,
    sandbox: str,
    timeout: int,
    session_scope: str,
    session_key: str | None = None,
    resume_thread_id: str | None = None,
    env: Mapping[str, str] | None = None,
    telemetry_enabled: bool,
    report_kind: str,
    activity_sink: ProviderActivitySink | None = None,
) -> dict[str, Any]:
    # The SDK controls local Codex/app-server. Import lazily so the base router does not require it.
    run_id = f"codex-sdk-{uuid.uuid4().hex[:12]}"
    started_at = utc_now_iso()
    import time

    started = time.time()
    emit_provider_activity(activity_sink, "working")
    try:
        with _SDK_LOCK:
            old_env: dict[str, str | None] = {}
            if env:
                for key, value in env.items():
                    if key.startswith("CONTEXT7_"):
                        old_env[key] = os.environ.get(key)
                        os.environ[key] = value
            try:
                codex = _ensure_codex()
                key = _session_key(cwd, session_scope, session_key)
                resumed_from_durable_state = False
                if key not in _THREADS:
                    kwargs = {"sandbox": _sandbox_value(sandbox)}
                    if model:
                        kwargs["model"] = model
                    thread = None
                    if resume_thread_id and hasattr(codex, "thread_resume"):
                        try:
                            thread = codex.thread_resume(resume_thread_id)
                            resumed_from_durable_state = True
                        except Exception:
                            thread = None
                    if thread is None:
                        # Newer SDK builds may accept cwd. Older ones may not.
                        try:
                            thread = codex.thread_start(cwd=str(cwd), **kwargs)
                        except TypeError:
                            thread = codex.thread_start(**kwargs)
                    _THREADS[key] = thread
                thread = _THREADS[key]
                try:
                    result = thread.run(
                        prompt, sandbox=_sandbox_value(sandbox), timeout=timeout
                    )
                except TypeError:
                    result = thread.run(prompt, sandbox=_sandbox_value(sandbox))
                final_text = str(getattr(result, "final_response", "") or "")
                final_report = _try_parse_json(final_text)
                final_report = normalize_final_report(final_report)
                valid_report, validation_errors = validate_final_report(
                    final_report, kind=report_kind
                )
                duration_ms = int((time.time() - started) * 1000)
                out: dict[str, Any] = {
                    "ok": valid_report,
                    "exit_code": 0 if valid_report else 1,
                    "run_id": run_id,
                    "provider": "codex",
                    "runner": "sdk",
                    "started_at": started_at,
                    "duration_ms": duration_ms,
                    "final_text": "" if valid_report else redact_text(final_text),
                    "final_report": redact_value(final_report) if valid_report else None,
                    "cwd": str(cwd),
                    "thread_id": getattr(thread, "id", None)
                    or getattr(thread, "thread_id", None),
                    "session_key": key,
                    "session_resumed": resumed_from_durable_state,
                }
                if not valid_report:
                    out.update(
                        provider_error(
                            "codex_invalid_structured_output",
                            "Codex SDK completed without a valid structured report.",
                            retryable=True,
                            provider="codex",
                            runner="sdk",
                            details={"validation_errors": validation_errors},
                        )
                    )
            finally:
                for key, old_value in old_env.items():
                    if old_value is None:
                        os.environ.pop(key, None)
                    else:
                        os.environ[key] = old_value
    except ModuleNotFoundError:
        out = {
            "ok": False,
            "exit_code": 1,
            "run_id": run_id,
            "provider": "codex",
            "runner": "sdk",
            "cwd": str(cwd),
            **provider_error(
                "codex_sdk_not_installed",
                "Python Codex SDK is not installed. Install `openai-codex` or set runner back to exec-json.",
                provider="codex",
                runner="sdk",
            ),
        }
    except Exception as exc:
        out = {
            "ok": False,
            "exit_code": 1,
            "run_id": run_id,
            "provider": "codex",
            "runner": "sdk",
            "cwd": str(cwd),
            **provider_error(
                "codex_sdk_failed",
                redact_text(str(exc)),
                retryable=True,
                provider="codex",
                runner="sdk",
            ),
        }

    if telemetry_enabled:
        out["telemetry_path"] = str(
            append_run(
                {
                    "run_id": out.get("run_id"),
                    "ok": out.get("ok"),
                    "exit_code": out.get("exit_code"),
                    "provider": "codex",
                    "runner": "sdk",
                    "started_at": out.get("started_at") or started_at,
                    "duration_ms": out.get("duration_ms", 0),
                    "thread_id": out.get("thread_id"),
                    "cwd": str(cwd),
                    "final_status": (out.get("final_report") or {}).get("status")
                    if isinstance(out.get("final_report"), dict)
                    else None,
                }
            )
        )
    return redact_value(out)


def close_sdk() -> None:
    global _CODEX
    if _CODEX is not None and hasattr(_CODEX, "__exit__"):
        try:
            _CODEX.__exit__(None, None, None)
        except Exception:
            pass
        _CODEX = None
    _THREADS.clear()


atexit.register(close_sdk)

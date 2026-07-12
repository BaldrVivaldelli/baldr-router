from __future__ import annotations

import atexit
import json
import queue
import subprocess
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Mapping

from .process_control import managed_popen, terminate_process_tree, unregister_process
from .provider_errors import provider_error
from .redaction import redact_text, redact_value
from .run import redact_command
from .schemas import normalize_final_report, validate_final_report
from .telemetry import append_run, utc_now_iso


class CodexAppServerError(RuntimeError):
    pass


class CodexAppServerSession:
    """Small JSON-RPC client for `codex app-server` over stdio.

    This runner is intentionally marked experimental. The app-server protocol is deeper than
    `codex exec --json`; this client covers the basic lifecycle the router needs: initialize,
    start/resume a thread in memory, start a turn, collect notifications until completion.
    """

    def __init__(
        self,
        *,
        env: Mapping[str, str] | None = None,
        timeout: int = 1800,
        codex_executable: str = "codex",
    ) -> None:
        self.timeout = timeout
        self._command = [codex_executable, "app-server"]
        self.proc = managed_popen(
            self._command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=dict(env) if env is not None else None,
            bufsize=1,
        )
        self._next_id = 1
        self._responses: dict[int, dict[str, Any]] = {}
        self._notifications: queue.Queue[dict[str, Any]] = queue.Queue()
        self._stderr: list[str] = []
        self._lock = threading.Lock()
        self._turn_lock = threading.Lock()
        self._reader_thread = threading.Thread(target=self._read_stdout, daemon=True)
        self._stderr_thread = threading.Thread(target=self._read_stderr, daemon=True)
        self._reader_thread.start()
        self._stderr_thread.start()
        self._initialize()

    def close(self) -> None:
        if self.proc.poll() is None:
            terminate_process_tree(self.proc, grace_seconds=1.5)
        unregister_process(self.proc)

    def _read_stdout(self) -> None:
        assert self.proc.stdout is not None
        for line in self.proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue
            if "id" in msg:
                try:
                    mid = int(msg["id"])
                except Exception:
                    continue
                self._responses[mid] = msg
            else:
                self._notifications.put(msg)

    def _read_stderr(self) -> None:
        assert self.proc.stderr is not None
        for line in self.proc.stderr:
            self._stderr.append(line)
            if len(self._stderr) > 200:
                self._stderr = self._stderr[-200:]

    def _send(self, message: dict[str, Any]) -> None:
        if self.proc.poll() is not None:
            raise CodexAppServerError("codex app-server exited unexpectedly")
        assert self.proc.stdin is not None
        self.proc.stdin.write(json.dumps(message, ensure_ascii=False) + "\n")
        self.proc.stdin.flush()

    def request(
        self, method: str, params: dict[str, Any] | None = None, timeout: int = 60
    ) -> dict[str, Any]:
        with self._lock:
            rid = self._next_id
            self._next_id += 1
        self._send({"method": method, "id": rid, "params": params or {}})
        deadline = time.time() + timeout
        while time.time() < deadline:
            if rid in self._responses:
                msg = self._responses.pop(rid)
                if "error" in msg:
                    raise CodexAppServerError(str(msg["error"]))
                return msg.get("result", {})
            time.sleep(0.05)
        raise CodexAppServerError(
            f"Timeout waiting for app-server response to {method}"
        )

    def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        self._send({"method": method, "params": params or {}})

    def _initialize(self) -> None:
        self.request(
            "initialize",
            {
                "clientInfo": {
                    "name": "baldr-router",
                    "title": "Baldr Router",
                    "version": "0.18.0",
                }
            },
            timeout=30,
        )
        self.notify("initialized", {})

    def start_thread(self, *, model: str) -> str:
        params = {"model": model} if model else {}
        res = self.request("thread/start", params, timeout=60)
        thread = res.get("thread") if isinstance(res, dict) else None
        if isinstance(thread, dict) and thread.get("id"):
            return str(thread["id"])
        raise CodexAppServerError(f"thread/start did not return a thread id: {res}")

    def resume_thread(self, thread_id: str, *, model: str) -> str:
        """Best-effort resume of a durable Codex thread after router restart."""
        params = {"threadId": thread_id}
        if model:
            params["model"] = model
        res = self.request("thread/resume", params, timeout=60)
        thread = res.get("thread") if isinstance(res, dict) else None
        if isinstance(thread, dict) and thread.get("id"):
            return str(thread["id"])
        # Some app-server builds acknowledge resume without echoing a thread.
        if isinstance(res, dict) and not res.get("error"):
            return thread_id
        raise CodexAppServerError(
            f"thread/resume did not acknowledge the thread: {res}"
        )

    def run_turn(
        self,
        *,
        thread_id: str,
        prompt: str,
        cwd: Path,
        sandbox: str,
        model: str,
        timeout: int,
        report_kind: str,
    ) -> dict[str, Any]:
        started = time.time()
        started_at = utc_now_iso()
        run_id = f"codex-app-{uuid.uuid4().hex[:12]}"
        params = {
            "threadId": thread_id,
            "input": [{"type": "text", "text": prompt}],
            "cwd": str(cwd),
            "sandbox": sandbox,
        }
        if model:
            params["model"] = model
        # A reused session can contain late notifications from the previous
        # turn. Drain them before starting a new correlated turn.
        while True:
            try:
                self._notifications.get_nowait()
            except queue.Empty:
                break

        try:
            self.request("turn/start", params, timeout=60)
        except CodexAppServerError as exc:
            return {
                "ok": False,
                "run_id": run_id,
                "provider": "codex",
                "runner": "app-server",
                "thread_id": thread_id,
                **provider_error(
                    "codex_app_server_turn_start_failed",
                    redact_text(str(exc)),
                    retryable=True,
                    provider="codex",
                    runner="app-server",
                ),
                "stderr": redact_text("".join(self._stderr[-100:])[-12000:]),
            }

        notifications: list[dict[str, Any]] = []
        agent_text_parts: list[str] = []
        completed: dict[str, Any] | None = None
        deadline = started + timeout
        while time.time() < deadline:
            try:
                msg = self._notifications.get(timeout=0.2)
            except queue.Empty:
                if self.proc.poll() is not None:
                    break
                continue
            msg = redact_value(msg)
            notifications.append(msg)
            method = str(msg.get("method") or "")
            params = msg.get("params") if isinstance(msg.get("params"), dict) else {}
            if method == "item/agentMessage/delta":
                delta = params.get("delta") or params.get("text") or ""
                if isinstance(delta, str):
                    agent_text_parts.append(delta)
            elif method in {"item/completed", "item/started"}:
                item = params.get("item") if isinstance(params, dict) else None
                if (
                    isinstance(item, dict)
                    and item.get("type") == "agent_message"
                    and item.get("text")
                ):
                    agent_text_parts.append(str(item["text"]))
            elif method == "turn/completed":
                completed = msg
                break

        duration_ms = int((time.time() - started) * 1000)
        completed_ok = completed is not None
        final_text = "".join(agent_text_parts).strip()
        final_report = _try_parse_json(final_text)
        final_report = normalize_final_report(final_report)
        valid_report, validation_errors = validate_final_report(
            final_report, kind=report_kind
        )
        ok = completed_ok and valid_report
        result = {
            "ok": ok,
            "exit_code": 0 if ok else (1 if completed_ok else 124),
            "run_id": run_id,
            "provider": "codex",
            "runner": "app-server",
            "started_at": started_at,
            "duration_ms": duration_ms,
            "thread_id": thread_id,
            "final_text": redact_text(final_text) if not valid_report else "",
            "final_report": redact_value(final_report) if valid_report else None,
            "notifications_count": len(notifications),
            "notifications": notifications[-80:],
            "stderr": redact_text("".join(self._stderr[-100:])[-12000:]),
            "command": redact_command(
                getattr(self, "_command", ["codex", "app-server"])
            ),
        }
        if not completed_ok:
            result.update(
                provider_error(
                    "codex_timeout",
                    f"Timeout waiting for turn/completed after {timeout} seconds.",
                    retryable=True,
                    provider="codex",
                    runner="app-server",
                    details={"timeout_seconds": timeout},
                )
            )
        elif not valid_report:
            result.update(
                provider_error(
                    "codex_invalid_structured_output",
                    "Codex app-server completed without a valid structured report.",
                    retryable=True,
                    provider="codex",
                    runner="app-server",
                    details={"validation_errors": validation_errors},
                )
            )
        return redact_value(result)


def _try_parse_json(text: str) -> Any:
    if not text:
        return None
    text = text.strip()
    # Allow fenced json in case the model returns it despite instructions.
    if text.startswith("```"):
        text = text.strip("`")
        text = text.removeprefix("json").strip()
    try:
        return json.loads(text)
    except Exception:
        return None


_SESSIONS: dict[str, tuple[CodexAppServerSession, str]] = {}
_SESSIONS_LOCK = threading.RLock()


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


def run_codex_app_server(
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
    codex_executable: str = "codex",
) -> dict[str, Any]:
    key = _session_key(cwd, session_scope, session_key)
    resumed_from_durable_state = False
    try:
        with _SESSIONS_LOCK:
            if key not in _SESSIONS:
                session = CodexAppServerSession(
                    env=env,
                    timeout=timeout,
                    codex_executable=codex_executable,
                )
                if resume_thread_id:
                    try:
                        thread_id = session.resume_thread(resume_thread_id, model=model)
                        resumed_from_durable_state = True
                    except Exception:
                        thread_id = session.start_thread(model=model)
                else:
                    thread_id = session.start_thread(model=model)
                _SESSIONS[key] = (session, thread_id)
            session, thread_id = _SESSIONS[key]
        with session._turn_lock:
            result = session.run_turn(
                thread_id=thread_id,
                prompt=prompt,
                cwd=cwd,
                sandbox=sandbox,
                model=model,
                timeout=timeout,
                report_kind=report_kind,
            )
        if not result.get("ok"):
            with _SESSIONS_LOCK:
                stale = _SESSIONS.pop(key, None)
            if stale:
                stale[0].close()
    except Exception as exc:
        result = {
            "ok": False,
            "exit_code": 1,
            "run_id": f"codex-app-{uuid.uuid4().hex[:12]}",
            "provider": "codex",
            "runner": "app-server",
            "cwd": str(cwd),
            **provider_error(
                "codex_app_server_failed",
                redact_text(str(exc)),
                retryable=True,
                provider="codex",
                runner="app-server",
            ),
        }
    result.setdefault("session_key", key)
    result.setdefault("session_resumed", resumed_from_durable_state)
    result.setdefault("thread_id", locals().get("thread_id") or resume_thread_id)
    if telemetry_enabled:
        result["telemetry_path"] = str(
            append_run(
                {
                    "run_id": result.get("run_id"),
                    "ok": result.get("ok"),
                    "exit_code": result.get("exit_code"),
                    "provider": "codex",
                    "runner": "app-server",
                    "started_at": result.get("started_at") or utc_now_iso(),
                    "duration_ms": result.get("duration_ms", 0),
                    "thread_id": result.get("thread_id"),
                    "cwd": str(cwd),
                    "final_status": (result.get("final_report") or {}).get("status")
                    if isinstance(result.get("final_report"), dict)
                    else None,
                }
            )
        )
    return redact_value(result)


def close_all_sessions() -> None:
    with _SESSIONS_LOCK:
        sessions = list(_SESSIONS.values())
        _SESSIONS.clear()
    for session, _thread_id in sessions:
        session.close()


atexit.register(close_all_sessions)

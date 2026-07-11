from __future__ import annotations

import json
import queue
import subprocess
import tempfile
import threading
import time
import uuid
from collections import Counter
from pathlib import Path
from typing import Any, Mapping

from .process_control import managed_popen, terminate_process_tree, unregister_process
from .provider_errors import provider_error
from .redaction import redact_text, redact_value
from .run import redact_command
from .schemas import validate_final_report, write_schema
from .telemetry import append_run, utc_now_iso


def _tail(text: str, limit: int) -> str:
    value = redact_text(text)
    return value[-limit:] if len(value) > limit else value


def _summarize_item(item: dict[str, Any]) -> dict[str, Any]:
    keys = ["id", "type", "status", "command", "name", "tool", "text", "path", "title"]
    out: dict[str, Any] = {}
    for key in keys:
        if key in item:
            value = redact_value(item[key])
            if isinstance(value, str) and len(value) > 600:
                value = value[:600] + "…"
            out[key] = value
    return out


def _parse_final_output(path: Path) -> tuple[Any, str, str | None]:
    if not path.exists():
        return None, "", "structured output file was not created"
    text = path.read_text(encoding="utf-8", errors="replace").strip()
    if not text:
        return None, "", "structured output file was empty"
    try:
        return json.loads(text), text, None
    except json.JSONDecodeError as exc:
        return None, text, f"invalid JSON at line {exc.lineno}, column {exc.colno}: {exc.msg}"


def _base_result(
    *,
    run_id: str,
    started_at: str,
    started: float,
    command: list[str],
) -> dict[str, Any]:
    return {
        "ok": False,
        "run_id": run_id,
        "provider": "codex",
        "runner": "exec-json",
        "started_at": started_at,
        "duration_ms": int((time.time() - started) * 1000),
        "command": redact_command(command),
    }


def run_codex_exec_json(
    cmd: list[str],
    *,
    cwd: Path,
    stdin: str,
    env: Mapping[str, str] | None,
    timeout: int,
    report_kind: str,
    telemetry_enabled: bool,
    keep_raw_events: bool,
    max_events_returned: int,
) -> dict[str, Any]:
    """Run ``codex exec --json`` and classify every failure deterministically."""

    run_id = f"codex-{uuid.uuid4().hex[:12]}"
    started = time.time()
    started_at = utc_now_iso()

    with tempfile.TemporaryDirectory(prefix=f"baldr-router-{run_id}-") as tmpdir:
        tmp = Path(tmpdir)
        schema_path = write_schema(tmp / "final-report.schema.json", kind=report_kind)
        final_path = tmp / "final-report.json"

        full_cmd = list(cmd)
        insertion = ["--json", "--output-schema", str(schema_path), "-o", str(final_path)]
        if full_cmd and full_cmd[-1] == "-":
            full_cmd = full_cmd[:-1] + insertion + ["-"]
        else:
            full_cmd.extend(insertion)

        q: queue.Queue[tuple[str, str]] = queue.Queue()
        stdout_lines: list[str] = []
        stderr_lines: list[str] = []
        raw_events: list[dict[str, Any]] = []
        returned_events: list[dict[str, Any]] = []
        item_summaries: list[dict[str, Any]] = []
        event_counts: Counter[str] = Counter()
        item_type_counts: Counter[str] = Counter()
        usage: dict[str, Any] | None = None
        thread_id: str | None = None
        last_error: dict[str, Any] | None = None
        malformed_json_lines = 0
        timed_out = False
        cleanup: dict[str, Any] | None = None

        try:
            proc = managed_popen(
                full_cmd,
                cwd=cwd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=env,
                bufsize=1,
            )
        except FileNotFoundError:
            return {
                **_base_result(
                    run_id=run_id, started_at=started_at, started=started, command=full_cmd
                ),
                **provider_error(
                    "codex_not_found",
                    "Codex CLI was not found. Install Codex CLI and run `codex login`.",
                    provider="codex",
                    runner="exec-json",
                ),
                "exit_code": 127,
            }
        except OSError as exc:
            return {
                **_base_result(
                    run_id=run_id, started_at=started_at, started=started, command=full_cmd
                ),
                **provider_error(
                    "codex_start_failed",
                    f"Could not start Codex CLI: {redact_text(str(exc))}",
                    provider="codex",
                    runner="exec-json",
                ),
                "exit_code": 126,
            }

        def _reader(stream: Any, stream_name: str) -> None:
            try:
                for line in stream:
                    q.put((stream_name, line))
            finally:
                q.put((stream_name, ""))

        stdout_thread = threading.Thread(target=_reader, args=(proc.stdout, "stdout"), daemon=True)
        stderr_thread = threading.Thread(target=_reader, args=(proc.stderr, "stderr"), daemon=True)
        stdout_thread.start()
        stderr_thread.start()

        try:
            assert proc.stdin is not None
            proc.stdin.write(stdin)
            proc.stdin.close()

            open_streams = {"stdout", "stderr"}
            deadline = started + timeout if timeout else None
            while open_streams:
                if deadline is not None and time.time() > deadline:
                    timed_out = True
                    cleanup = terminate_process_tree(proc, grace_seconds=1.0)
                    break
                try:
                    stream_name, line = q.get(timeout=0.2)
                except queue.Empty:
                    if proc.poll() is not None and not stdout_thread.is_alive() and not stderr_thread.is_alive():
                        break
                    continue
                if line == "":
                    open_streams.discard(stream_name)
                    continue
                if stream_name == "stderr":
                    stderr_lines.append(line)
                    continue

                stdout_lines.append(line)
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    malformed_json_lines += 1
                    continue
                if not isinstance(event, dict):
                    malformed_json_lines += 1
                    continue
                event = redact_value(event)
                etype = str(event.get("type") or "unknown")
                event_counts[etype] += 1
                if keep_raw_events:
                    raw_events.append(event)
                if len(returned_events) < max_events_returned:
                    returned_events.append(event)

                if etype == "thread.started" and event.get("thread_id"):
                    thread_id = str(event.get("thread_id"))
                if etype in {"turn.completed", "turn.failed"} and isinstance(event.get("usage"), dict):
                    usage = event.get("usage")
                if etype == "error":
                    last_error = event
                item = event.get("item")
                if isinstance(item, dict):
                    itype = str(item.get("type") or "unknown")
                    item_type_counts[itype] += 1
                    if len(item_summaries) < max_events_returned:
                        item_summaries.append(_summarize_item(item))

            if proc.poll() is None:
                try:
                    exit_code = proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    cleanup = terminate_process_tree(proc, grace_seconds=1.0)
                    exit_code = 124
                    timed_out = True
            else:
                exit_code = int(proc.returncode or 0)
        finally:
            stdout_thread.join(timeout=1)
            stderr_thread.join(timeout=1)
            unregister_process(proc)

        final_json, final_text, parse_error = _parse_final_output(final_path)
        valid_report, validation_errors = validate_final_report(final_json, kind=report_kind)
        duration_ms = int((time.time() - started) * 1000)
        stderr = _tail("".join(stderr_lines), 12000)
        stdout_tail = _tail("".join(stdout_lines), 12000)

        result: dict[str, Any] = {
            "ok": False,
            "exit_code": 124 if timed_out else exit_code,
            "run_id": run_id,
            "provider": "codex",
            "runner": "exec-json",
            "started_at": started_at,
            "duration_ms": duration_ms,
            "thread_id": thread_id,
            "usage": redact_value(usage or {}),
            "event_counts": dict(event_counts),
            "item_type_counts": dict(item_type_counts),
            "malformed_json_lines": malformed_json_lines,
            "items": item_summaries,
            "events": returned_events,
            "final_report": final_json if valid_report else None,
            "final_text": _tail(final_text, 12000) if not valid_report else "",
            "stderr": stderr,
            "stdout_tail": stdout_tail,
            "command": redact_command(full_cmd),
        }
        if last_error:
            result["last_error"] = last_error
        if keep_raw_events:
            result["raw_events"] = raw_events
        if cleanup:
            result["cleanup"] = cleanup

        if timed_out:
            result.update(
                provider_error(
                    "codex_timeout",
                    f"Codex timed out after {timeout} seconds and its process tree was terminated.",
                    retryable=True,
                    provider="codex",
                    runner="exec-json",
                    details={"timeout_seconds": timeout},
                )
            )
        elif exit_code < 0:
            result.update(
                provider_error(
                    "codex_process_aborted",
                    f"Codex was aborted by signal {-exit_code}.",
                    retryable=True,
                    provider="codex",
                    runner="exec-json",
                    details={"signal": -exit_code},
                )
            )
        elif exit_code != 0:
            combined = f"{stderr}\n{stdout_tail}".lower()
            if any(token in combined for token in ("not logged in", "login required", "authentication", "unauthorized")):
                code = "codex_not_authenticated"
                message = "Codex is not authenticated. Run `codex login` and choose ChatGPT sign-in."
            else:
                code = "codex_process_failed"
                message = stderr.strip() or f"Codex exited with code {exit_code}."
            result.update(
                provider_error(
                    code,
                    message,
                    retryable=code == "codex_process_failed",
                    provider="codex",
                    runner="exec-json",
                    details={"exit_code": exit_code},
                )
            )
        elif not valid_report:
            message = "Codex completed but did not return a valid structured report."
            result.update(
                provider_error(
                    "codex_invalid_structured_output",
                    message,
                    retryable=True,
                    provider="codex",
                    runner="exec-json",
                    details={
                        "parse_error": parse_error,
                        "validation_errors": validation_errors,
                        "malformed_json_lines": malformed_json_lines,
                    },
                )
            )
        else:
            result["ok"] = True

        if telemetry_enabled:
            telemetry_record = {
                "run_id": run_id,
                "ok": result["ok"],
                "exit_code": result["exit_code"],
                "provider": "codex",
                "runner": "exec-json",
                "started_at": started_at,
                "duration_ms": duration_ms,
                "thread_id": thread_id,
                "usage": usage or {},
                "event_counts": dict(event_counts),
                "item_type_counts": dict(item_type_counts),
                "malformed_json_lines": malformed_json_lines,
                "cwd": str(cwd),
                "report_kind": report_kind,
                "final_status": final_json.get("status") if valid_report else None,
                "error_code": (result.get("error") or {}).get("code"),
            }
            result["telemetry_path"] = str(append_run(telemetry_record))

        return redact_value(result)

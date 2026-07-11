from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any, Mapping

from .process_control import managed_popen, terminate_process_tree, unregister_process
from .redaction import redact_text


def run_command(
    cmd: list[str],
    *,
    cwd: Path | None = None,
    stdin: str | None = None,
    env: Mapping[str, str] | None = None,
    timeout: int = 60,
    stdout_limit: int = 30000,
    stderr_limit: int = 12000,
) -> dict[str, Any]:
    try:
        proc = managed_popen(
            cmd,
            cwd=cwd,
            stdin=subprocess.PIPE if stdin is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
        )
    except FileNotFoundError:
        return {
            "ok": False,
            "exit_code": 127,
            "stdout": "",
            "stderr": "",
            "reason": f"Command not found: {cmd[0] if cmd else '<empty>'}",
            "error": {
                "code": "command_not_found",
                "message": f"Command not found: {cmd[0] if cmd else '<empty>'}",
                "retryable": False,
            },
            "command": redact_command(cmd),
        }
    except OSError as exc:
        return {
            "ok": False,
            "exit_code": 126,
            "stdout": "",
            "stderr": redact_text(str(exc)),
            "reason": f"Could not start command: {exc}",
            "error": {
                "code": "command_start_failed",
                "message": redact_text(str(exc)),
                "retryable": False,
            },
            "command": redact_command(cmd),
        }

    try:
        stdout, stderr = proc.communicate(input=stdin, timeout=timeout)
        exit_code = int(proc.returncode or 0)
        aborted = exit_code < 0
        result = {
            "ok": exit_code == 0,
            "exit_code": exit_code,
            "stdout": redact_text((stdout or "")[-stdout_limit:]),
            "stderr": redact_text((stderr or "")[-stderr_limit:]),
            "command": redact_command(cmd),
        }
        if aborted:
            result.update(
                {
                    "ok": False,
                    "reason": f"Command was aborted by signal {-exit_code}.",
                    "error": {
                        "code": "process_aborted",
                        "message": f"Command was aborted by signal {-exit_code}.",
                        "retryable": True,
                        "signal": -exit_code,
                    },
                }
            )
        elif exit_code != 0:
            result.update(
                {
                    "reason": redact_text((stderr or stdout or f"Command exited with code {exit_code}.").strip()),
                    "error": {
                        "code": "process_failed",
                        "message": redact_text((stderr or stdout or f"Command exited with code {exit_code}.").strip()),
                        "retryable": False,
                    },
                }
            )
        return result
    except subprocess.TimeoutExpired as exc:
        cleanup = terminate_process_tree(proc, grace_seconds=1.0)
        stdout = exc.stdout if isinstance(exc.stdout, str) else ""
        stderr = exc.stderr if isinstance(exc.stderr, str) else ""
        message = f"Command timed out after {timeout} seconds."
        return {
            "ok": False,
            "exit_code": 124,
            "stdout": redact_text(stdout[-stdout_limit:]),
            "stderr": redact_text((stderr + f"\n{message}")[-stderr_limit:]),
            "reason": message,
            "error": {
                "code": "timeout",
                "message": message,
                "retryable": True,
                "timeout_seconds": timeout,
            },
            "cleanup": cleanup,
            "command": redact_command(cmd),
        }
    finally:
        unregister_process(proc)


def redact_command(cmd: list[str]) -> list[str]:
    redacted: list[str] = []
    skip_next = False
    for item in cmd:
        if skip_next:
            redacted.append("<redacted>")
            skip_next = False
            continue
        lowered = item.lower()
        if item in {"--api-key", "--token", "--password", "--secret"}:
            redacted.append(item)
            skip_next = True
            continue
        if any(fragment in lowered for fragment in ("api_key", "apikey", "token=", "password=", "secret=")):
            redacted.append("<redacted>")
        else:
            redacted.append(redact_text(item))
    return redacted

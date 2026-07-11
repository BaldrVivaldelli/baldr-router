from __future__ import annotations

import atexit
import os
import signal
import subprocess
import threading
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass
class _ManagedProcess:
    process: subprocess.Popen[Any]
    run_id: str | None
    command: tuple[str, ...]


_ACTIVE: dict[int, _ManagedProcess] = {}
_LOCK = threading.RLock()
_SIGNAL_HANDLERS_INSTALLED = False
_PREVIOUS_HANDLERS: dict[int, Any] = {}
_TERMINATING = False


def _creation_kwargs() -> dict[str, Any]:
    if os.name == "nt":
        return {"creationflags": getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)}
    return {"start_new_session": True}


def register_process(
    proc: subprocess.Popen[Any],
    *,
    run_id: str | None = None,
    command: Sequence[str] | None = None,
) -> subprocess.Popen[Any]:
    with _LOCK:
        _ACTIVE[proc.pid] = _ManagedProcess(
            process=proc,
            run_id=(run_id or "").strip() or None,
            command=tuple(str(item) for item in (command or ())),
        )
    _install_handlers()
    return proc


def unregister_process(proc: subprocess.Popen[Any]) -> None:
    with _LOCK:
        _ACTIVE.pop(proc.pid, None)


def managed_popen(
    cmd: Sequence[str],
    *,
    cwd: Path | str | None = None,
    env: Mapping[str, str] | None = None,
    **kwargs: Any,
) -> subprocess.Popen[Any]:
    options = _creation_kwargs()
    options.update(kwargs)
    child_env = dict(env) if env is not None else None
    proc = subprocess.Popen(
        list(cmd),
        cwd=str(cwd) if cwd is not None else None,
        env=child_env,
        **options,
    )
    run_id = (child_env or os.environ).get("BALDR_ROUTER_RUN_ID")
    return register_process(proc, run_id=run_id, command=cmd)


def _wait(proc: subprocess.Popen[Any], timeout: float) -> bool:
    try:
        proc.wait(timeout=max(0.05, timeout))
        return True
    except subprocess.TimeoutExpired:
        return False
    except Exception:
        try:
            return proc.poll() is not None
        except Exception:
            return False


def terminate_process_tree(
    proc: subprocess.Popen[Any],
    *,
    grace_seconds: float = 2.0,
    force: bool = True,
) -> dict[str, Any]:
    """Terminate a managed process and its descendants as reliably as possible."""
    try:
        exited = proc.poll() is not None
    except Exception:
        exited = False
    result: dict[str, Any] = {
        "pid": proc.pid,
        "already_exited": exited,
        "terminated": False,
        "forced": False,
    }
    if exited:
        unregister_process(proc)
        result["exit_code"] = proc.returncode
        result["terminated"] = True
        return result

    if os.name == "nt":
        try:
            proc.send_signal(getattr(signal, "CTRL_BREAK_EVENT", signal.SIGTERM))
        except Exception:
            try:
                proc.terminate()
            except Exception:
                pass
        if not _wait(proc, grace_seconds) and force:
            result["forced"] = True
            try:
                subprocess.run(
                    ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                    text=True,
                    capture_output=True,
                    timeout=10,
                    check=False,
                )
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
            _wait(proc, 2.0)
    else:
        try:
            pgid = os.getpgid(proc.pid)
            # Every managed process is started in a fresh session. The guard is
            # defensive: never signal our own process group if a third-party
            # Popen implementation ignored start_new_session.
            if pgid == os.getpgrp():
                proc.terminate()
            else:
                os.killpg(pgid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        except Exception:
            try:
                proc.terminate()
            except Exception:
                pass
        if not _wait(proc, grace_seconds) and force:
            result["forced"] = True
            try:
                pgid = os.getpgid(proc.pid)
                if pgid == os.getpgrp():
                    proc.kill()
                else:
                    os.killpg(pgid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
            _wait(proc, 2.0)

    try:
        result["terminated"] = proc.poll() is not None
        result["exit_code"] = proc.poll()
    except Exception:
        result["terminated"] = True
        result["exit_code"] = None
    unregister_process(proc)
    return result


def _records_for_run(run_id: str) -> list[_ManagedProcess]:
    with _LOCK:
        return [record for record in _ACTIVE.values() if record.run_id == run_id]


def terminate_processes_for_run(
    run_id: str,
    *,
    grace_seconds: float = 1.0,
) -> list[dict[str, Any]]:
    """Terminate only provider/subprocess trees associated with a durable run."""
    return [
        terminate_process_tree(record.process, grace_seconds=grace_seconds)
        for record in _records_for_run(run_id)
    ]


def terminate_all_processes(*, grace_seconds: float = 1.0) -> list[dict[str, Any]]:
    global _TERMINATING
    with _LOCK:
        if _TERMINATING:
            return []
        _TERMINATING = True
        processes = [record.process for record in _ACTIVE.values()]
    results: list[dict[str, Any]] = []
    try:
        for proc in processes:
            results.append(terminate_process_tree(proc, grace_seconds=grace_seconds))
    finally:
        with _LOCK:
            _TERMINATING = False
    return results


def active_processes(*, run_id: str | None = None) -> list[dict[str, Any]]:
    with _LOCK:
        values = list(_ACTIVE.values())
    result: list[dict[str, Any]] = []
    for record in values:
        if run_id is not None and record.run_id != run_id:
            continue
        proc = record.process
        try:
            exit_code = proc.poll()
        except Exception:
            exit_code = None
        result.append(
            {
                "pid": proc.pid,
                "exit_code": exit_code,
                "running": exit_code is None,
                "run_id": record.run_id,
                "command": list(record.command),
            }
        )
    return result


def _signal_handler(signum: int, frame: Any) -> None:
    terminate_all_processes(grace_seconds=0.75)
    previous = _PREVIOUS_HANDLERS.get(signum)
    if callable(previous) and previous is not _signal_handler:
        previous(signum, frame)
        return
    if previous == signal.SIG_IGN:
        return
    raise SystemExit(128 + signum)


def _install_handlers() -> None:
    """Install handlers only when explicitly requested by a top-level runtime.

    Libraries and test runners must not silently replace their process-wide
    SIGTERM handler merely because they launched one managed child. The actual
    CLI/MCP launcher may opt in with BALDR_INSTALL_SIGNAL_HANDLERS=1.
    """
    global _SIGNAL_HANDLERS_INSTALLED
    if os.getenv("BALDR_INSTALL_SIGNAL_HANDLERS", "").strip() != "1":
        return
    if _SIGNAL_HANDLERS_INSTALLED or threading.current_thread() is not threading.main_thread():
        return
    _SIGNAL_HANDLERS_INSTALLED = True
    for name in ("SIGINT", "SIGTERM"):
        sig = getattr(signal, name, None)
        if sig is None:
            continue
        try:
            _PREVIOUS_HANDLERS[int(sig)] = signal.getsignal(sig)
            signal.signal(sig, _signal_handler)
        except Exception:
            pass


atexit.register(terminate_all_processes)

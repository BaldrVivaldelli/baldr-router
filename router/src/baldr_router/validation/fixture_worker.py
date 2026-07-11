from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

_STOP = False
_CHILD: subprocess.Popen[Any] | None = None


def _emit(event_type: str, **payload: Any) -> None:
    print(json.dumps({"type": event_type, **payload}, ensure_ascii=False), flush=True)


def _signal_handler(signum: int, _frame: Any) -> None:
    global _STOP
    _STOP = True
    _emit("signal.received", signal=signum)
    child = _CHILD
    if child is not None and child.poll() is None:
        try:
            child.terminate()
        except Exception:
            pass


def _install_signals() -> None:
    for name in ("SIGINT", "SIGTERM"):
        value = getattr(signal, name, None)
        if value is not None:
            try:
                signal.signal(value, _signal_handler)
            except Exception:
                pass


def _write_pid_file(path: str | None, *, child_pid: int | None = None) -> None:
    if not path:
        return
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps({"pid": os.getpid(), "child_pid": child_pid}) + "\n",
        encoding="utf-8",
    )


def _success(workspace: Path, steps: int, delay: float) -> int:
    workspace.mkdir(parents=True, exist_ok=True)
    _emit("fixture.started", pid=os.getpid(), mode="success")
    for index in range(steps):
        _emit("fixture.progress", current=index + 1, total=steps)
        if delay:
            time.sleep(delay)
    marker = workspace / "baldr-fixture.txt"
    marker.write_text("baldr fixture completed\n", encoding="utf-8")
    _emit(
        "fixture.completed",
        ok=True,
        marker=str(marker),
        bytes=marker.stat().st_size,
    )
    return 0


def _stream(steps: int, delay: float) -> int:
    _emit("fixture.started", pid=os.getpid(), mode="stream-progress")
    for index in range(steps):
        _emit("fixture.progress", current=index + 1, total=steps)
        time.sleep(delay)
    _emit("fixture.completed", ok=True)
    return 0


def _hang(pid_file: str | None, spawn_child: bool) -> int:
    global _CHILD
    _install_signals()
    if spawn_child:
        _CHILD = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(3600)"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    _write_pid_file(pid_file, child_pid=_CHILD.pid if _CHILD else None)
    _emit(
        "fixture.started",
        pid=os.getpid(),
        child_pid=_CHILD.pid if _CHILD else None,
        mode="spawn-child-and-hang" if spawn_child else "hang-until-cancelled",
    )
    while not _STOP:
        time.sleep(0.1)
    if _CHILD is not None:
        try:
            _CHILD.wait(timeout=2)
        except Exception:
            try:
                _CHILD.kill()
            except Exception:
                pass
    _emit("fixture.cancelled", ok=True)
    return 130


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Deterministic Baldr lifecycle fixture worker")
    parser.add_argument(
        "mode",
        choices=[
            "success",
            "stream-progress",
            "hang-until-cancelled",
            "spawn-child-and-hang",
            "crash",
            "invalid-json",
            "secret-output",
        ],
    )
    parser.add_argument("--workspace", default=".")
    parser.add_argument("--steps", type=int, default=3)
    parser.add_argument("--delay", type=float, default=0.05)
    parser.add_argument("--pid-file")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.mode == "success":
        return _success(Path(args.workspace).resolve(), max(1, args.steps), max(0.0, args.delay))
    if args.mode == "stream-progress":
        return _stream(max(1, args.steps), max(0.0, args.delay))
    if args.mode == "hang-until-cancelled":
        return _hang(args.pid_file, False)
    if args.mode == "spawn-child-and-hang":
        return _hang(args.pid_file, True)
    if args.mode == "crash":
        _emit("fixture.started", mode="crash")
        _emit("fixture.error", code="synthetic_crash")
        return 42
    if args.mode == "invalid-json":
        print("{this is not valid json", flush=True)
        return 0
    if args.mode == "secret-output":
        print("CONTEXT7_API_KEY=ctx7sk-synthetic-secret-for-redaction", flush=True)
        return 0
    raise AssertionError(args.mode)


if __name__ == "__main__":
    raise SystemExit(main())

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from baldr_router.codex import reset_codex_login_cache, run_codex_role_prompt
from baldr_router.codex_exec_json import run_codex_exec_json


FAKE_CODEX = r'''#!/usr/bin/env python3
import json
import os
import signal
import sys
import time
from pathlib import Path

args = sys.argv[1:]
if args[:2] == ["login", "status"]:
    if os.environ.get("FAKE_CODEX_LOGIN") == "fail":
        print("Not logged in", file=sys.stderr)
        raise SystemExit(1)
    print("Logged in with ChatGPT")
    raise SystemExit(0)
if args == ["--version"]:
    print("codex-cli 0.test")
    raise SystemExit(0)

mode = os.environ.get("FAKE_CODEX_MODE", "valid")
if mode == "timeout":
    time.sleep(30)
if mode == "abort":
    os.kill(os.getpid(), signal.SIGTERM)
if mode == "failed":
    print("synthetic codex failure", file=sys.stderr)
    raise SystemExit(23)

out = None
for index, value in enumerate(args):
    if value == "-o" and index + 1 < len(args):
        out = Path(args[index + 1])
        break
if out is None:
    print("missing output path", file=sys.stderr)
    raise SystemExit(2)

print(json.dumps({"type": "thread.started", "thread_id": "fake-thread"}), flush=True)
print(json.dumps({"type": "item.completed", "item": {"type": "command_execution", "command": "pytest"}}), flush=True)
if mode == "malformed-event":
    print("not-json", flush=True)

if mode == "invalid-json":
    out.write_text("{not valid json", encoding="utf-8")
elif mode == "invalid-schema":
    out.write_text(json.dumps({"status": "implemented", "summary": "missing arrays"}), encoding="utf-8")
else:
    report = {
        "status": os.environ.get("FAKE_CODEX_STATUS", "implemented"),
        "summary": "synthetic success",
        "files_modified": ["example.txt"],
        "commands_run": ["pytest"],
        "tests_run": ["pytest: passed"],
        "verification_needed": [],
        "risks": [],
        "follow_up": [],
    }
    out.write_text(json.dumps(report), encoding="utf-8")
print(json.dumps({"type": "turn.completed", "usage": {"input_tokens": 10, "output_tokens": 5}}), flush=True)
'''


def _fake_codex(tmp_path: Path) -> Path:
    binary = tmp_path / "codex"
    binary.write_text(FAKE_CODEX, encoding="utf-8")
    binary.chmod(binary.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return binary


def _run_direct(tmp_path: Path, mode: str, *, timeout: int = 3):
    binary = _fake_codex(tmp_path)
    env = os.environ.copy()
    env["PATH"] = f"{tmp_path}{os.pathsep}{env.get('PATH', '')}"
    env["FAKE_CODEX_MODE"] = mode
    return run_codex_exec_json(
        [str(binary), "exec", "-"],
        cwd=tmp_path,
        stdin="test prompt",
        env=env,
        timeout=timeout,
        report_kind="implementation",
        telemetry_enabled=False,
        keep_raw_events=False,
        max_events_returned=20,
    )


def test_codex_exec_json_success_and_malformed_event_accounting(tmp_path: Path):
    result = _run_direct(tmp_path, "malformed-event")

    assert result["ok"] is True
    assert result["thread_id"] == "fake-thread"
    assert result["malformed_json_lines"] == 1
    assert result["final_report"]["status"] == "implemented"
    assert result["usage"]["input_tokens"] == 10


@pytest.mark.parametrize("mode", ["invalid-json", "invalid-schema"])
def test_codex_invalid_structured_output_is_classified(tmp_path: Path, mode: str):
    result = _run_direct(tmp_path, mode)

    assert result["ok"] is False
    assert result["error"]["code"] == "codex_invalid_structured_output"
    assert result["error"]["retryable"] is True


def test_codex_timeout_terminates_process_tree(tmp_path: Path):
    result = _run_direct(tmp_path, "timeout", timeout=1)

    assert result["ok"] is False
    assert result["exit_code"] == 124
    assert result["error"]["code"] == "codex_timeout"
    assert result["cleanup"]["terminated"] is True


@pytest.mark.skipif(os.name == "nt", reason="signal exit code is POSIX-specific")
def test_codex_aborted_process_is_classified(tmp_path: Path):
    result = _run_direct(tmp_path, "abort")

    assert result["ok"] is False
    assert result["error"]["code"] == "codex_process_aborted"
    assert result["error"]["details"]["signal"] == 15


def test_codex_nonzero_process_is_classified(tmp_path: Path):
    result = _run_direct(tmp_path, "failed")

    assert result["ok"] is False
    assert result["error"]["code"] == "codex_process_failed"
    assert result["exit_code"] == 23


def test_missing_codex_login_is_caught_before_work(tmp_path: Path, monkeypatch):
    _fake_codex(tmp_path)
    monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}{os.environ.get('PATH', '')}")
    monkeypatch.setenv("FAKE_CODEX_LOGIN", "fail")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    reset_codex_login_cache()

    result = run_codex_role_prompt(
        cwd=tmp_path,
        prompt="do work",
        role="implementer",
        workflow="test",
        can_write=True,
        sandbox="workspace-write",
        report_kind="implementation",
    )

    assert result["ok"] is False
    assert result["error"]["code"] == "codex_not_authenticated"
    assert "codex login" in result["reason"].lower()

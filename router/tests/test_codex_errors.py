from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

from baldr_router import codex as codex_module
from baldr_router.codex import reset_codex_login_cache, run_codex_role_prompt
from baldr_router.codex_exec_json import run_codex_exec_json


FAKE_CODEX = r"""#!/usr/bin/env python3
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
if os.environ.get("FAKE_CODEX_ENV_OUTPUT"):
    Path(os.environ["FAKE_CODEX_ENV_OUTPUT"]).write_text(json.dumps({
        name: os.environ.get(name)
        for name in (
            "UV_CACHE_DIR",
            "PIP_CACHE_DIR",
            "npm_config_cache",
            "NPM_CONFIG_CACHE",
            "TMPDIR",
            "TMP",
            "TEMP",
        )
    }), encoding="utf-8")
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
        "decisions": [{"key": "delivery", "value": "direct"}],
        "constraints": [],
        "assumptions": [],
        "alternatives_rejected": [],
        "acceptance_criteria": [],
        "blockers": [],
        "review_decision": "not_applicable",
    }
    out.write_text(json.dumps(report), encoding="utf-8")
print(json.dumps({"type": "turn.completed", "usage": {"input_tokens": 10, "output_tokens": 5}}), flush=True)
"""


def _fake_codex(tmp_path: Path) -> Path:
    script = tmp_path / "fake_codex.py"
    script.write_text(FAKE_CODEX, encoding="utf-8")
    return script


def _run_direct(
    tmp_path: Path,
    mode: str,
    *,
    timeout: int = 3,
    extra_env: dict[str, str] | None = None,
):
    binary = _fake_codex(tmp_path)
    env = os.environ.copy()
    env["PATH"] = f"{tmp_path}{os.pathsep}{env.get('PATH', '')}"
    env["FAKE_CODEX_MODE"] = mode
    env.update(extra_env or {})
    return run_codex_exec_json(
        [sys.executable, str(binary), "exec", "-"],
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
    assert result["final_report"]["decisions"] == {"delivery": "direct"}
    assert result["usage"]["input_tokens"] == 10


def test_codex_exec_json_supplies_sandbox_writable_tool_caches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    for name in (
        "UV_CACHE_DIR",
        "PIP_CACHE_DIR",
        "npm_config_cache",
        "NPM_CONFIG_CACHE",
        "TMPDIR",
        "TMP",
        "TEMP",
    ):
        monkeypatch.delenv(name, raising=False)
    observed_path = tmp_path / "tool-cache-env.json"

    result = _run_direct(
        tmp_path,
        "valid",
        extra_env={"FAKE_CODEX_ENV_OUTPUT": str(observed_path)},
    )
    observed = json.loads(observed_path.read_text(encoding="utf-8"))

    assert result["ok"] is True
    assert "--add-dir" in result["command"]
    assert all(observed.values())
    assert observed["npm_config_cache"] == observed["NPM_CONFIG_CACHE"]
    assert observed["TMPDIR"] == observed["TMP"] == observed["TEMP"]
    assert all("baldr-router-codex-" in value for value in observed.values())
    assert all(not value.startswith(str(Path.home())) for value in observed.values())


def test_codex_exec_json_preserves_explicit_tool_cache(tmp_path: Path):
    observed_path = tmp_path / "explicit-tool-cache-env.json"
    explicit = tmp_path / "operator-cache"

    result = _run_direct(
        tmp_path,
        "valid",
        extra_env={
            "FAKE_CODEX_ENV_OUTPUT": str(observed_path),
            "UV_CACHE_DIR": str(explicit),
        },
    )
    observed = json.loads(observed_path.read_text(encoding="utf-8"))

    assert result["ok"] is True
    assert observed["UV_CACHE_DIR"] == str(explicit)


def test_codex_read_only_uses_granular_scratch_permission(tmp_path: Path):
    binary = _fake_codex(tmp_path)
    result = run_codex_exec_json(
        [
            sys.executable,
            str(binary),
            "exec",
            "--sandbox",
            "read-only",
            "-",
        ],
        cwd=tmp_path,
        stdin="test prompt",
        env=os.environ.copy(),
        timeout=10,
        report_kind="implementation",
        telemetry_enabled=False,
        keep_raw_events=False,
        max_events_returned=20,
    )

    command = result["command"]
    assert result["ok"] is True
    assert "--sandbox" not in command
    assert "--add-dir" not in command
    assert "--ignore-user-config" in command
    assert 'default_permissions="baldr-read-only-scratch"' in command
    assert any(
        value.startswith("permissions.baldr-read-only-scratch.filesystem=")
        and '":root"="read"' in value
        and '="write"' in value
        for value in command
    )


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
    assert "process tree" in result["error"]["summary"].lower()
    assert "retry" in result["error"]["action"].lower()
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


def test_codex_commands_use_the_resolved_executable(tmp_path: Path, monkeypatch):
    executable = str(tmp_path / "codex.cmd")
    calls: list[list[str]] = []

    def fake_run_command(command: list[str], *, timeout: int):
        calls.append(command)
        assert timeout == 20
        return {"ok": True}

    monkeypatch.setattr(codex_module, "codex_found", lambda: executable)
    monkeypatch.setattr(codex_module, "run_command", fake_run_command)

    assert codex_module.codex_login_status()["ok"] is True
    assert codex_module.codex_version()["ok"] is True
    command = codex_module.build_codex_exec_command(
        workspace_root=tmp_path,
        sandbox="read-only",
        approval_policy="never",
        skip_git_repo_check=False,
    )

    assert calls == [
        [executable, "login", "status"],
        [executable, "--version"],
    ]
    assert command[0] == executable


@pytest.mark.skipif(os.name != "nt", reason="Windows command shim integration")
def test_windows_cmd_shim_is_discovered_and_runs_from_path_with_spaces(
    tmp_path: Path, monkeypatch
):
    shim_dir = tmp_path / "Codex shim with spaces"
    shim_dir.mkdir()
    script = _fake_codex(shim_dir)
    launcher = shim_dir / "codex.cmd"
    launcher.write_text(
        f'@echo off\n"{sys.executable}" "{script}" %*\nexit /b %errorlevel%\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("PATH", f"{shim_dir}{os.pathsep}{os.environ.get('PATH', '')}")
    monkeypatch.setenv("PATHEXT", ".COM;.EXE;.BAT;.CMD")
    monkeypatch.setenv("FAKE_CODEX_MODE", "malformed-event")

    executable = codex_module.codex_found()

    assert executable is not None
    assert Path(executable).resolve() == launcher.resolve()
    login = codex_module.codex_login_status()
    version = codex_module.codex_version()
    assert login["ok"] is True, login
    assert "Logged in with ChatGPT" in login["stdout"]
    assert version["ok"] is True, version
    assert "codex-cli 0.test" in version["stdout"]

    command = codex_module.build_codex_exec_command(
        workspace_root=shim_dir,
        sandbox="read-only",
        approval_policy="never",
        skip_git_repo_check=True,
    )
    result = run_codex_exec_json(
        command,
        cwd=shim_dir,
        stdin="test prompt",
        env=os.environ.copy(),
        timeout=10,
        report_kind="implementation",
        telemetry_enabled=False,
        keep_raw_events=False,
        max_events_returned=20,
    )

    assert Path(command[0]).resolve() == launcher.resolve()
    assert result["ok"] is True, result
    assert result["thread_id"] == "fake-thread"
    assert result["malformed_json_lines"] == 1
    assert result["final_report"]["status"] == "implemented"


def test_missing_codex_login_is_caught_before_work(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(codex_module, "codex_found", lambda: "fake-codex")
    monkeypatch.setattr(
        codex_module,
        "codex_login_status",
        lambda: {
            "ok": False,
            "exit_code": 1,
            "stdout": "",
            "stderr": "Not logged in",
        },
    )
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
    assert result["error"]["retryable"] is True
    assert "codex login" in result["error"]["action"].lower()
    assert "codex login" in result["reason"].lower()

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from baldr_router.facade import facade_run
from baldr_router.facade_contract import render_facade_prompt
from baldr_router.server import (
    baldr_run_prompt,
    baldr_setup_prompt,
    baldr_status_prompt,
    run_architect_implement_review,
)


def _git_repo(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", str(path)], check=True)


def _semantic_run(result: dict) -> dict:
    return {
        "ok": result.get("ok"),
        "dry_run": result.get("dry_run"),
        "workflow": result.get("workflow"),
        "workspace_root": result.get("workspace_root"),
        "roles": result.get("roles"),
        "max_rounds": result.get("max_rounds"),
        "planned_steps": result.get("planned_steps"),
    }


def test_mcp_prompts_are_the_shared_contract_prompts():
    assert baldr_setup_prompt("/tmp/repo") == render_facade_prompt(
        "setup", workspace_root="/tmp/repo"
    )
    assert baldr_status_prompt("/tmp/repo") == render_facade_prompt(
        "status", workspace_root="/tmp/repo"
    )
    assert baldr_run_prompt("Implement X", "/tmp/repo") == render_facade_prompt(
        "run", workspace_root="/tmp/repo", task="Implement X"
    )


def test_cli_facade_mcp_and_python_facade_are_semantically_equivalent(
    tmp_path: Path, monkeypatch
):
    workspace = tmp_path / "repo"
    _git_repo(workspace)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setenv(
        "BALDR_TRUSTED_WORKSPACE_ROOTS_JSON", json.dumps([str(workspace)])
    )

    direct = facade_run(
        str(workspace),
        "Implement conformance fixture",
        client="conformance",
        dry_run=True,
    )
    mcp_result = run_architect_implement_review(
        str(workspace), "Implement conformance fixture", dry_run=True
    )

    env = os.environ.copy()
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "baldr_router",
            "facade",
            "run",
            str(workspace),
            "Implement conformance fixture",
            "--client",
            "conformance",
            "--dry-run",
        ],
        text=True,
        capture_output=True,
        env=env,
        timeout=30,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    cli_result = json.loads(completed.stdout)

    assert _semantic_run(direct) == _semantic_run(mcp_result)
    assert _semantic_run(direct) == _semantic_run(cli_result)
    assert direct["facade"] == cli_result["facade"] == {
        "intent": "run",
        "client": "conformance",
        "contract_version": "1.0.0",
    }

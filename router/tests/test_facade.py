from __future__ import annotations

import json
import subprocess
from pathlib import Path

from baldr_router.facade import (
    FACADE_INTENTS,
    facade_contract,
    facade_run,
    facade_setup_plan,
    facade_status_report,
    render_facade_prompt,
)
from baldr_router.cli import _parse_agent_overrides, build_parser


def test_facade_contract_is_frozen_and_packaged():
    contract = facade_contract()
    assert contract["contract"] == "baldr-facade"
    assert contract["version"] == "1.0.0"
    assert tuple(contract["intents"]) == FACADE_INTENTS
    assert contract["commandPalette"]["maximumVisibleCommands"] == 1


def test_facade_prompts_use_shared_intents():
    setup = render_facade_prompt("setup", client="test")
    status = render_facade_prompt("status", client="test")
    run = render_facade_prompt(
        "run", workspace_root="/tmp/example", task="Implement X", client="test"
    )
    assert "router_doctor" in setup
    assert "Do not modify" in status
    assert "run_architect_implement_review" in run
    assert "Implement X" in run


def test_facade_setup_and_status_return_shared_shape(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))

    setup = facade_setup_plan(str(tmp_path), client="test")
    status = facade_status_report(str(tmp_path), client="test")
    assert setup["intent"] == "setup"
    assert setup["client"] == "test"
    assert "context7_decision" in setup
    assert status["intent"] == "status"
    assert status["client"] == "test"
    assert "telemetry" in status


def test_facade_run_dry_run_uses_existing_workflow(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    workspace = tmp_path / "repo"
    workspace.mkdir()
    subprocess.run(["git", "init", "-q", str(workspace)], check=True)
    monkeypatch.setenv(
        "BALDR_TRUSTED_WORKSPACE_ROOTS_JSON", json.dumps([str(workspace)])
    )
    result = facade_run(
        str(workspace),
        "Implement a small feature",
        client="test",
        dry_run=True,
    )
    assert result["ok"] is True
    assert result["dry_run"] is True
    assert result["workflow"] == "architect-implement-review"
    assert result["facade"] == {
        "intent": "run",
        "client": "test",
        "contract_version": "1.0.0",
    }


def test_facade_team_preferences_and_cli_flags_round_trip(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    workspace = tmp_path / "repo"
    workspace.mkdir()
    subprocess.run(["git", "init", "-q", str(workspace)], check=True)
    reference = "local://kiro/planner@1.0.0"

    result = facade_setup_plan(
        str(workspace),
        client="test",
        team_mode="automatic",
        agent_overrides={"architect": reference},
    )
    arguments = build_parser().parse_args(
        [
            "facade",
            "setup",
            str(workspace),
            "--team-mode",
            "automatic",
            "--agent-override",
            f"architect={reference}",
        ]
    )

    preferences = result["workbench"]["preferences"]
    assert preferences["team_mode"] == "automatic"
    assert preferences["agent_overrides"] == {
        "architect": reference
    }
    assert arguments.team_mode == "automatic"
    assert _parse_agent_overrides(arguments.agent_override) == {"architect": reference}
    assert _parse_agent_overrides(None, clear=True) == {}


def test_source_contract_matches_packaged_copy():
    repo_root = Path(__file__).resolve().parents[2]
    source = json.loads((repo_root / "contracts" / "facade-v1.json").read_text())
    packaged = facade_contract()
    assert source == packaged

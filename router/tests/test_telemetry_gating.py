from __future__ import annotations

from pathlib import Path

from baldr_router.config import load_config, save_config
from baldr_router.durability.engine import DurableWorkflowEngine
from baldr_router.durability.store import DurableStore
from baldr_router.telemetry import runs_jsonl_path


def _result() -> dict:
    return {
        "run_id": "run-telemetry-gating",
        "ok": True,
        "workflow": "architect-implement-review",
        "status": "approved",
        "steps": [],
    }


def _engine(tmp_path: Path) -> DurableWorkflowEngine:
    return DurableWorkflowEngine(store=DurableStore(path=tmp_path / "baldr.sqlite3"))


def test_workflow_telemetry_is_skipped_when_disabled(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    cfg = load_config()
    cfg.telemetry.enabled = False
    save_config(cfg)

    _engine(tmp_path)._append_telemetry(_result())

    assert not runs_jsonl_path().exists()


def test_workflow_telemetry_is_recorded_when_enabled(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    cfg = load_config()
    cfg.telemetry.enabled = True
    save_config(cfg)

    _engine(tmp_path)._append_telemetry(_result())

    assert "run-telemetry-gating" in runs_jsonl_path().read_text(encoding="utf-8")

"""Characterization tests for the entry contract of ``DurableWorkflowEngine.run``.

``run`` validates cancellation, resume targets, idempotency and lease ownership
before any provider work happens. Those guards used to be inline in a 708-line
method; these tests pin their exact durable results so the decomposition cannot
change what a client sees.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from baldr_router.config import AppConfig, ExecutionProfileConfig
from baldr_router.durability.engine import (
    DurableWorkflowEngine,
    _idempotency_conflict_result,
    _resolved_snapshot,
    _workflow_lease_seconds,
)
from baldr_router.durability.store import DurableStore, IdempotencyConflict


def _config() -> AppConfig:
    cfg = AppConfig.defaults()
    cfg.context7.enabled = False
    cfg.durability.lease_seconds = 2
    cfg.execution_profiles = {
        "architecture": ExecutionProfileConfig(provider="codex", model="a"),
        "implementation": ExecutionProfileConfig(provider="codex", model="i"),
        "review": ExecutionProfileConfig(provider="codex", model="r"),
    }
    cfg.roles["architect"].profiles = ["architecture"]
    cfg.roles["implementer"].profiles = ["implementation"]
    cfg.roles["reviewer"].profiles = ["review"]
    return cfg


def _snapshot() -> dict:
    return _resolved_snapshot(
        _config(),
        architect_provider=None,
        implementer_provider=None,
        reviewer_provider=None,
        max_rounds=0,
    )


def _engine(tmp_path: Path) -> DurableWorkflowEngine:
    return DurableWorkflowEngine(store=DurableStore(path=tmp_path / "state.sqlite3"))


def _run(engine: DurableWorkflowEngine, workspace: Path, **overrides: object) -> dict:
    arguments: dict = {
        "workspace_root": workspace,
        "task": "fixture",
        "extra_context": "",
        "config_snapshot": _snapshot(),
        "context7_libraries": None,
        "client_name": "test",
    }
    arguments.update(overrides)
    return engine.run(**arguments)  # type: ignore[arg-type]


def test_cancellation_without_a_run_id_is_an_invalid_request(tmp_path: Path) -> None:
    result = _run(_engine(tmp_path), tmp_path, cancel=True)

    assert result["ok"] is False
    assert result["status"] == "invalid_request"
    assert result["error"]["code"] == "cancel_requires_run_id"
    assert result["reason"] == "Cancellation requires resume_run_id."


def test_resuming_an_unknown_run_reports_it_as_not_found(tmp_path: Path) -> None:
    result = _run(_engine(tmp_path), tmp_path, resume_run_id="run-does-not-exist")

    assert result["ok"] is False
    assert result["status"] == "not_found"
    assert result["error"]["code"] == "durable_run_not_found"
    assert "run-does-not-exist" in result["reason"]


def test_cancelling_an_unknown_run_takes_the_cancellation_path(
    tmp_path: Path,
) -> None:
    """Cancellation is routed before resume validation.

    Documents current behavior: unlike a resume, cancelling a run that does not
    exist raises instead of returning a durable ``not_found`` result. The intent
    here is to pin which branch handles the request, not to endorse the raise.
    """
    with pytest.raises(KeyError):
        _run(
            _engine(tmp_path), tmp_path, cancel=True, resume_run_id="run-does-not-exist"
        )


@pytest.mark.parametrize(
    ("configured", "expected"),
    [(None, 45), (0, 45), (2, 15), (45, 45), (600, 600)],
)
def test_workflow_lease_never_drops_below_its_operational_floor(
    configured: int | None,
    expected: int,
) -> None:
    snapshot = {"durability": {"lease_seconds": configured}}

    assert _workflow_lease_seconds(snapshot) == expected


def test_idempotency_conflicts_report_both_fingerprints() -> None:
    result = _idempotency_conflict_result(
        IdempotencyConflict("key-1", "expected-1", "received-1")
    )

    assert result["ok"] is False
    assert result["status"] == "idempotency_conflict"
    assert result["error"] == {
        "code": "idempotency_conflict",
        "key": "key-1",
        "expected_fingerprint": "expected-1",
        "received_fingerprint": "received-1",
    }
    assert result["reason"]

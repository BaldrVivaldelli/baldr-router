from __future__ import annotations

import logging
from pathlib import Path

from baldr_router import diagnostics


def _fresh_logger(tmp_path: Path, monkeypatch, **env: str) -> logging.Logger:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(diagnostics, "_configured", False)
    return diagnostics.configure_logging(force=True)


def test_log_records_never_reach_stdout_or_stderr(tmp_path: Path, monkeypatch, capsys):
    logger = _fresh_logger(tmp_path, monkeypatch)

    logger.warning("durable transition failed")

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""
    assert "durable transition failed" in diagnostics.log_path().read_text(
        encoding="utf-8"
    )


def test_root_handlers_cannot_capture_records(tmp_path: Path, monkeypatch):
    logger = _fresh_logger(tmp_path, monkeypatch)

    assert logger.propagate is False


def test_secrets_are_redacted_in_the_log(tmp_path: Path, monkeypatch):
    secret = "ctx7sk-synthetic-diagnostics-secret-4242"
    monkeypatch.setenv("CONTEXT7_API_KEY", secret)
    logger = _fresh_logger(tmp_path, monkeypatch)

    logger.warning("provider rejected the call with %s", secret)
    raw = diagnostics.log_path().read_text(encoding="utf-8")

    assert secret not in raw
    assert "<redacted>" in raw


def test_suppressed_exception_keeps_its_traceback(tmp_path: Path, monkeypatch):
    logger = _fresh_logger(tmp_path, monkeypatch)

    try:
        raise RuntimeError("sqlite is locked")
    except RuntimeError:
        diagnostics.log_suppressed(logger, "Could not record", run_id="run-7")

    raw = diagnostics.log_path().read_text(encoding="utf-8")

    assert "Could not record" in raw
    assert "run_id='run-7'" in raw
    assert "RuntimeError: sqlite is locked" in raw
    assert "Traceback" in raw


def test_logging_can_be_switched_off(tmp_path: Path, monkeypatch):
    logger = _fresh_logger(tmp_path, monkeypatch, BALDR_ROUTER_LOG_LEVEL="off")

    logger.error("should not be written")

    assert not diagnostics.log_path().exists()


def test_unwritable_state_directory_degrades_instead_of_raising(
    tmp_path: Path, monkeypatch
):
    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory\n", encoding="utf-8")
    logger = _fresh_logger(
        tmp_path,
        monkeypatch,
        BALDR_ROUTER_LOG_FILE=str(blocker / "nested" / "router.log"),
    )

    logger.warning("degraded but alive")

    assert any(isinstance(h, logging.NullHandler) for h in logger.handlers)

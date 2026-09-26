"""Local, redacted diagnostics log.

Baldr swallows failures that must never abort a durable run, which used to mean
a failed state transition or a lost cleanup left no trace at all. These records
give an operator something to read after the fact.

The MCP server owns stdout for its JSON-RPC stream, so a record must never
reach it. Everything goes to a rotating file under the Baldr state directory
and passes through the same redaction as telemetry and evidence. Logging is
best-effort by construction: if the file cannot be opened the logger degrades
to a null handler instead of failing the caller.
"""

from __future__ import annotations

import logging
import os
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

from .redaction import redact_text
from .telemetry import app_state_dir

LOGGER_NAME = "baldr_router"
DEFAULT_LEVEL = "WARNING"
_MAX_BYTES = 2_000_000
_BACKUP_COUNT = 3
_OFF_VALUES = frozenset({"off", "none", "disabled", "0", "false"})
_configured = False


class _RedactingFormatter(logging.Formatter):
    """Redact the fully rendered record, arguments and traceback included."""

    def format(self, record: logging.LogRecord) -> str:
        return redact_text(super().format(record))


def log_path() -> Path:
    raw = os.environ.get("BALDR_ROUTER_LOG_FILE", "").strip()
    return Path(raw).expanduser() if raw else app_state_dir() / "router.log"


def _requested_level() -> str:
    return os.environ.get("BALDR_ROUTER_LOG_LEVEL", DEFAULT_LEVEL).strip() or DEFAULT_LEVEL


def configure_logging(*, force: bool = False) -> logging.Logger:
    """Attach the rotating file handler once per process."""

    global _configured
    logger = logging.getLogger(LOGGER_NAME)
    if _configured and not force:
        return logger
    for existing in list(logger.handlers):
        logger.removeHandler(existing)
        existing.close()
    # A record must never escape to stdout through a root handler installed by
    # whichever host imported Baldr.
    logger.propagate = False
    level = _requested_level()
    if level.lower() in _OFF_VALUES:
        logger.addHandler(logging.NullHandler())
        logger.setLevel(logging.CRITICAL + 1)
        _configured = True
        return logger
    logger.setLevel(getattr(logging, level.upper(), logging.WARNING))
    try:
        path = log_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        handler: logging.Handler = RotatingFileHandler(
            path,
            maxBytes=_MAX_BYTES,
            backupCount=_BACKUP_COUNT,
            encoding="utf-8",
            delay=True,
        )
        handler.setFormatter(
            _RedactingFormatter(
                "%(asctime)s %(levelname)s %(name)s %(message)s",
                datefmt="%Y-%m-%dT%H:%M:%S%z",
            )
        )
    except OSError:
        # A read-only or missing state directory must not break a run.
        handler = logging.NullHandler()
    logger.addHandler(handler)
    _configured = True
    return logger


def get_logger(name: str | None = None) -> logging.Logger:
    configure_logging()
    if not name:
        return logging.getLogger(LOGGER_NAME)
    return logging.getLogger(LOGGER_NAME).getChild(name.removeprefix("baldr_router."))


def log_suppressed(
    logger: logging.Logger,
    message: str,
    /,
    **context: Any,
) -> None:
    """Record an exception the caller is about to swallow on purpose.

    Call from inside an ``except`` block: the active traceback is included so a
    silent degradation stays diagnosable without changing control flow.
    """

    try:
        details = " ".join(f"{key}={value!r}" for key, value in sorted(context.items()))
        logger.warning("%s %s", message, details, exc_info=True)
    except Exception:  # pragma: no cover - logging must never raise
        pass

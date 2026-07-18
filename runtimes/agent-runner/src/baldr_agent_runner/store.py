from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def default_state_path() -> Path:
    base = Path(
        os.environ.get("XDG_STATE_HOME")
        or (Path.home() / ".local" / "state")
    )
    return base / "baldr-agent-runner" / "jobs.sqlite3"


class RunnerStore:
    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path or default_state_path()).expanduser().resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._migrate()
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=FULL")
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=10000")
        return connection

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        connection = self.connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _migrate(self) -> None:
        with self.transaction() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS jobs (
                    job_id TEXT PRIMARY KEY,
                    idempotency_key TEXT NOT NULL UNIQUE,
                    request_digest TEXT NOT NULL,
                    request_id TEXT NOT NULL,
                    agent_ref TEXT NOT NULL,
                    agent_digest TEXT NOT NULL,
                    effect_mode TEXT NOT NULL,
                    state TEXT NOT NULL,
                    child_pid INTEGER,
                    result_json TEXT,
                    error_code TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS events (
                    job_id TEXT NOT NULL REFERENCES jobs(job_id) ON DELETE CASCADE,
                    sequence INTEGER NOT NULL,
                    category TEXT NOT NULL,
                    message TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (job_id, sequence)
                );
                """
            )

    @staticmethod
    def _row(row: sqlite3.Row | None) -> dict[str, Any] | None:
        return dict(row) if row is not None else None

    def begin(
        self,
        *,
        job_id: str,
        idempotency_key: str,
        request_digest: str,
        request_id: str,
        agent_ref: str,
        agent_digest: str,
        effect_mode: str,
    ) -> tuple[dict[str, Any], bool]:
        now = utc_now()
        with self.transaction() as connection:
            existing = connection.execute(
                "SELECT * FROM jobs WHERE idempotency_key=?",
                (idempotency_key,),
            ).fetchone()
            if existing is not None:
                value = dict(existing)
                if value["request_digest"] != request_digest:
                    raise ValueError(
                        "An idempotency key cannot be reused for a different request."
                    )
                if value["job_id"] != job_id:
                    raise ValueError(
                        "An idempotent retry must preserve the original job_id."
                    )
                return value, True
            connection.execute(
                """
                INSERT INTO jobs(
                    job_id, idempotency_key, request_digest, request_id,
                    agent_ref, agent_digest, effect_mode, state,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'accepted', ?, ?)
                """,
                (
                    job_id,
                    idempotency_key,
                    request_digest,
                    request_id,
                    agent_ref,
                    agent_digest,
                    effect_mode,
                    now,
                    now,
                ),
            )
            row = connection.execute(
                "SELECT * FROM jobs WHERE job_id=?", (job_id,)
            ).fetchone()
            return dict(row), False

    def get(self, job_id: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            return self._row(
                connection.execute(
                    "SELECT * FROM jobs WHERE job_id=?", (job_id,)
                ).fetchone()
            )

    def set_running(self, job_id: str, child_pid: int) -> None:
        with self.transaction() as connection:
            connection.execute(
                "UPDATE jobs SET state='running', child_pid=?, updated_at=? WHERE job_id=?",
                (child_pid, utc_now(), job_id),
            )

    def finish(
        self,
        job_id: str,
        *,
        state: str,
        result: dict[str, Any],
        error_code: str | None,
    ) -> None:
        with self.transaction() as connection:
            connection.execute(
                """
                UPDATE jobs
                SET state=?, child_pid=NULL, result_json=?, error_code=?, updated_at=?
                WHERE job_id=?
                """,
                (
                    state,
                    json.dumps(result, ensure_ascii=False, separators=(",", ":")),
                    error_code,
                    utc_now(),
                    job_id,
                ),
            )

    def add_event(self, job_id: str, category: str, message: str) -> dict[str, Any]:
        with self.transaction() as connection:
            sequence = int(
                connection.execute(
                    "SELECT COALESCE(MAX(sequence), 0) + 1 FROM events WHERE job_id=?",
                    (job_id,),
                ).fetchone()[0]
            )
            connection.execute(
                "INSERT INTO events(job_id, sequence, category, message, created_at) VALUES (?, ?, ?, ?, ?)",
                (job_id, sequence, category[:160], message[:4096], utc_now()),
            )
        return {
            "sequence": sequence,
            "category": category[:160],
            "message": message[:4096],
        }

    def events(self, job_id: str, *, after: int, limit: int) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT sequence, category, message, created_at
                FROM events WHERE job_id=? AND sequence>? ORDER BY sequence LIMIT ?
                """,
                (job_id, max(0, after), max(1, min(limit, 1000))),
            ).fetchall()
        return [dict(row) for row in rows]

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

from baldr_router.durability.store import DurableStore


def test_close_releases_connections_opened_by_other_threads(tmp_path: Path) -> None:
    store = DurableStore(path=tmp_path / "baldr.sqlite3")
    worker_connections: list[sqlite3.Connection] = []

    def open_in_thread() -> None:
        worker_connections.append(store.connect())

    threads = [threading.Thread(target=open_in_thread) for _ in range(3)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(worker_connections) == 3

    store.close()

    # A closed sqlite3 connection raises ProgrammingError on any use, which is
    # how this test proves the handle was actually released.
    for connection in worker_connections:
        try:
            connection.execute("SELECT 1")
        except sqlite3.ProgrammingError:
            continue
        raise AssertionError("close() left a worker thread's connection open")


def test_a_thread_whose_connection_was_closed_gets_a_fresh_one(
    tmp_path: Path,
) -> None:
    store = DurableStore(path=tmp_path / "baldr.sqlite3")
    results: list[int] = []
    opened = threading.Event()
    closed = threading.Event()

    def worker() -> None:
        store.connect()
        opened.set()
        assert closed.wait(timeout=10)
        # The cached handle is gone; connect() must not hand back a dead one.
        row = store.connect().execute("SELECT 1 AS value").fetchone()
        results.append(int(row["value"]))

    thread = threading.Thread(target=worker)
    thread.start()
    assert opened.wait(timeout=10)
    store.close()
    closed.set()
    thread.join(timeout=10)

    assert results == [1]


def test_close_is_idempotent(tmp_path: Path) -> None:
    store = DurableStore(path=tmp_path / "baldr.sqlite3")
    store.close()
    store.close()

    assert store.connect().execute("SELECT 1").fetchone() is not None

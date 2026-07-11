from __future__ import annotations

from pathlib import Path

import pytest

from baldr_router.config import AppConfig, save_config
from baldr_router.durability.store import DurableStore


def test_private_artifacts_are_not_inlined_into_sqlite(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    cfg = AppConfig.defaults()
    cfg.artifact_privacy.private_artifacts_external = True
    save_config(cfg)
    store = DurableStore(path=tmp_path / "state" / "baldr.sqlite3")
    artifact_id = store.store_artifact(
        run_id=None,
        kind="private-test",
        value={"task": "private but required for durable recovery"},
        redaction_level="private",
        redact=False,
    )
    row = store.connect().execute(
        "SELECT inline_text, storage_path FROM artifacts WHERE id = ?", (artifact_id,)
    ).fetchone()
    assert row is not None
    assert row["inline_text"] is None
    assert row["storage_path"]
    assert Path(str(row["storage_path"])).exists()
    assert store.load_artifact(artifact_id)["task"].startswith("private")

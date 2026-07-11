from __future__ import annotations

from pathlib import Path

from baldr_router.lab import matrix


def test_lab_requires_consecutive_passes(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))

    counter = {"value": 0}

    def fake_verification(**_kwargs):
        counter["value"] += 1
        return {
            "ok": True,
            "run_id": f"run-{counter['value']}",
            "duration_ms": 1,
            "passed": 7,
            "skipped": 1,
            "failed": 0,
            "scenarios": [{"id": "fixture", "status": "passed", "duration_ms": 1}],
        }

    monkeypatch.setattr(matrix, "run_lifecycle_verification", fake_verification)
    result = matrix.run_lab_matrix(repeat=3, profile="test")
    assert result["ok"] is True
    assert result["acceptance_met"] is True
    assert result["consecutive_passes"] == 3
    assert Path(result["evidence"]["path"]).exists()

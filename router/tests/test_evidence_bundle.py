from __future__ import annotations

import json
from pathlib import Path

from baldr_router.evidence import create_evidence_bundle, latest_evidence, list_evidence


def test_evidence_bundle_redacts_secrets_and_home(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("CONTEXT7_API_KEY", "ctx7sk-synthetic-super-secret-evidence")
    result = create_evidence_bundle(
        kind="lifecycle",
        environment={
            "fingerprint": "abc",
            "home": str(Path.home()),
            "token": "ctx7sk-synthetic-super-secret-evidence",
        },
        lifecycle={
            "ok": True,
            "run_id": "run-1",
            "scenarios": [{"id": "x", "status": "passed", "duration_ms": 1}],
        },
    )
    root = Path(result["path"])
    combined = "\n".join(path.read_text(encoding="utf-8") for path in root.iterdir() if path.is_file())
    assert "ctx7sk-synthetic-super-secret-evidence" not in combined
    assert str(Path.home()) not in combined
    assert (root / "artifact-hashes.json").exists()
    assert latest_evidence(kind="lifecycle")["available"] is True
    assert list_evidence()["count"] == 1


def test_evidence_manifest_is_valid_json(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    result = create_evidence_bundle(
        kind="lab",
        environment={"fingerprint": "env"},
        lifecycle={"ok": False, "series_id": "series", "scenarios": []},
    )
    manifest = json.loads((Path(result["path"]) / "manifest.json").read_text())
    assert manifest["kind"] == "lab"
    assert manifest["ok"] is False

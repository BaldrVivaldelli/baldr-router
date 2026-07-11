from __future__ import annotations

import importlib.util
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "run_qualification_ci.py"


def _module():
    spec = importlib.util.spec_from_file_location("baldr_qualification_ci", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_export_result_copies_redacted_receipt_and_uses_portable_pointer(tmp_path: Path) -> None:
    source = tmp_path / "generated-receipt"
    source.mkdir()
    (source / "receipt.json").write_text('{"status":"provisional"}\n', encoding="utf-8")
    output = tmp_path / "output"

    exported = _module().export_result(
        {
            "ok": False,
            "status": "provisional",
            "qualification_id": "br-q-test",
            "profile": "vscode-linux-native",
            "receipt_sha256": "abc123",
            "checks": {"lab": {"ok": True}},
            "next_steps": ["Complete real evidence"],
            "bundle": {"path": str(source)},
        },
        output,
    )

    assert exported["status"] == "provisional"
    assert (output / "receipt" / "receipt.json").exists()
    summary = json.loads((output / "qualification-result.json").read_text(encoding="utf-8"))
    assert summary["receipt_directory"] == "receipt"
    assert str(tmp_path) not in json.dumps(summary)


def test_portable_error_redacts_home_and_checkout(monkeypatch, tmp_path: Path) -> None:
    module = _module()
    monkeypatch.chdir(tmp_path)
    text = module._portable_text(f"failed in {Path.home()} and {tmp_path}")
    assert str(Path.home()) not in text
    assert str(tmp_path) not in text
    assert "~" in text
    assert "<checkout>" in text

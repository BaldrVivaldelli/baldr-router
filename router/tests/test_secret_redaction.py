from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from baldr_router.context7 import _write_cache
from baldr_router.run import run_command
from baldr_router.telemetry import append_run, runs_jsonl_path


def test_telemetry_never_persists_known_secret(tmp_path: Path, monkeypatch):
    secret = "ctx7sk-synthetic-super-secret-value-123456"
    monkeypatch.setenv("CONTEXT7_API_KEY", secret)
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))

    append_run(
        {
            "ok": False,
            "message": f"provider failed with {secret}",
            "authorization": f"Bearer {secret}",
            "nested": {"api_key": secret},
        }
    )
    raw = runs_jsonl_path().read_text(encoding="utf-8")

    assert secret not in raw
    assert "<redacted>" in raw


def test_command_output_is_redacted(tmp_path: Path, monkeypatch):
    secret = "ctx7sk-synthetic-command-output-secret-999999"
    monkeypatch.setenv("CONTEXT7_API_KEY", secret)
    result = run_command(
        [sys.executable, "-c", "import os; print(os.environ['CONTEXT7_API_KEY'])"],
        env=os.environ.copy(),
    )

    assert result["ok"] is True
    assert secret not in json.dumps(result)
    assert "<redacted>" in result["stdout"]


def test_context7_cache_hides_query_and_response_secrets(tmp_path: Path, monkeypatch):
    secret = "ctx7sk-synthetic-cache-secret-abcdefgh"
    monkeypatch.setenv("CONTEXT7_API_KEY", secret)
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))

    path = _write_cache(
        "/api/v2/context",
        {"libraryId": "/example/lib", "query": f"private task {secret}"},
        200,
        {"echo": secret, "authorization": f"Bearer {secret}"},
    )
    raw = path.read_text(encoding="utf-8")
    payload = json.loads(raw)

    assert secret not in raw
    assert payload["params"]["query"] == "<query-redacted>"
    assert payload["body"]["echo"] == "<redacted>"

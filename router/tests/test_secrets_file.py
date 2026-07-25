from __future__ import annotations

import stat
import sys
import tomllib
from pathlib import Path

import pytest

from baldr_router.secrets import (
    _load_secret_file,
    _write_secret_file,
    read_context7_api_key,
    store_context7_api_key_local_file,
)


def test_writer_preserves_unrelated_sections(tmp_path: Path) -> None:
    path = tmp_path / "secrets.toml"
    path.write_text(
        '[context7]\napi_key = "old-key"\n\n'
        '[provider]\ntoken = "keep-me"\nenabled = true\nretries = 3\n',
        encoding="utf-8",
    )

    data = _load_secret_file(path)
    data.setdefault("context7", {})["api_key"] = "new-key"
    _write_secret_file(data, path)

    reloaded = tomllib.loads(path.read_text(encoding="utf-8"))
    assert reloaded["context7"]["api_key"] == "new-key"
    assert reloaded["provider"] == {
        "token": "keep-me",
        "enabled": True,
        "retries": 3,
    }


def test_writer_round_trips_quotes_and_backslashes(tmp_path: Path) -> None:
    path = tmp_path / "secrets.toml"
    awkward = 'ctx7sk-with-"quote"-and-\\backslash\\-value'

    _write_secret_file({"context7": {"api_key": awkward}}, path)

    assert _load_secret_file(path)["context7"]["api_key"] == awkward


def test_writer_drops_empty_sections_and_values(tmp_path: Path) -> None:
    path = tmp_path / "secrets.toml"

    _write_secret_file(
        {"context7": {"api_key": ""}, "empty": {}, "other": {"token": "kept"}},
        path,
    )

    reloaded = _load_secret_file(path)
    assert "context7" not in reloaded
    assert "empty" not in reloaded
    assert reloaded["other"]["token"] == "kept"


def test_writer_rejects_unsupported_values(tmp_path: Path) -> None:
    path = tmp_path / "secrets.toml"

    with pytest.raises(TypeError):
        _write_secret_file({"context7": {"api_key": ["not", "a", "scalar"]}}, path)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX permission bits only")
def test_stored_secret_file_stays_owner_only(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))

    path = store_context7_api_key_local_file("  ctx7sk-synthetic-value  ")

    assert read_context7_api_key("local-file") == "ctx7sk-synthetic-value"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600

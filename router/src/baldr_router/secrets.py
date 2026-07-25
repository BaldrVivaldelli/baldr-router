from __future__ import annotations

import getpass
import os
import stat
import tomllib
from collections.abc import Mapping
from pathlib import Path
from typing import Optional

from .config import secrets_path


def _load_secret_file(path: Path | None = None) -> dict:
    p = path or secrets_path()
    if not p.exists():
        return {}
    return tomllib.loads(p.read_text(encoding="utf-8"))


def _toml_scalar(value: object) -> str:
    """Render a supported scalar as TOML.

    The secrets file is written by Baldr and read back with ``tomllib``, so the
    accepted value space stays deliberately small. Anything outside it is a
    programming error and must fail loudly instead of silently degrading a
    credential to a lossy string.
    """
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return repr(value)
    if isinstance(value, str):
        escaped = (
            value.replace("\\", "\\\\")
            .replace('"', '\\"')
            .replace("\n", "\\n")
            .replace("\r", "\\r")
            .replace("\t", "\\t")
        )
        return f'"{escaped}"'
    raise TypeError(f"Unsupported secrets value type: {type(value).__name__}")


def _dump_secret_toml(data: Mapping[str, object]) -> str:
    """Serialize the whole secrets document, not just the sections we know.

    Emitting only recognized keys silently destroys every other credential in
    the file on the next write, so the writer must round-trip whatever
    ``_load_secret_file`` parsed.
    """
    lines: list[str] = []
    for key, value in data.items():
        if isinstance(value, Mapping):
            continue
        lines.append(f"{key} = {_toml_scalar(value)}")
    for key, value in data.items():
        if not isinstance(value, Mapping):
            continue
        if lines:
            lines.append("")
        lines.append(f"[{key}]")
        for inner_key, inner_value in value.items():
            if isinstance(inner_value, Mapping):
                raise TypeError(
                    f"Nested secrets tables are unsupported: {key}.{inner_key}"
                )
            lines.append(f"{inner_key} = {_toml_scalar(inner_value)}")
    return "".join(f"{line}\n" for line in lines)


def _write_secret_file(data: dict, path: Path | None = None) -> Path:
    p = path or secrets_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    pruned = {
        key: (
            {
                inner_key: inner_value
                for inner_key, inner_value in value.items()
                if inner_value not in (None, "")
            }
            if isinstance(value, Mapping)
            else value
        )
        for key, value in data.items()
    }
    pruned = {
        key: value
        for key, value in pruned.items()
        if not (isinstance(value, Mapping) and not value) and value is not None
    }
    p.write_text(_dump_secret_toml(pruned), encoding="utf-8")
    os.chmod(p, stat.S_IRUSR | stat.S_IWUSR)
    return p


def read_context7_api_key(source: str = "env:CONTEXT7_API_KEY") -> Optional[str]:
    if source.startswith("env:"):
        return os.environ.get(source.split(":", 1)[1])
    if source == "local-file":
        data = _load_secret_file()
        key = data.get("context7", {}).get("api_key")
        return key or None
    return None


def store_context7_api_key_local_file(api_key: str) -> Path:
    data = _load_secret_file()
    data.setdefault("context7", {})["api_key"] = api_key.strip()
    return _write_secret_file(data)


def prompt_context7_key_and_store() -> Path:
    key = getpass.getpass("Context7 API key (input hidden): ").strip()
    if not key:
        raise SystemExit("No API key provided.")
    return store_context7_api_key_local_file(key)

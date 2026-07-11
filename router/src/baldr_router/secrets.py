from __future__ import annotations

import getpass
import os
import stat
import tomllib
from pathlib import Path
from typing import Optional

from .config import secrets_path


def _load_secret_file(path: Path | None = None) -> dict:
    p = path or secrets_path()
    if not p.exists():
        return {}
    return tomllib.loads(p.read_text(encoding="utf-8"))


def _write_secret_file(data: dict, path: Path | None = None) -> Path:
    p = path or secrets_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    content = ""
    context7 = data.get("context7", {})
    if context7:
        content += "[context7]\n"
        if context7.get("api_key"):
            escaped = str(context7["api_key"]).replace('"', '\\"')
            content += f'api_key = "{escaped}"\n'
    p.write_text(content, encoding="utf-8")
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

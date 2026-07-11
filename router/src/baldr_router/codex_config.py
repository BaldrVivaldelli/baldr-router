from __future__ import annotations

import re
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any

CODEX_CONTEXT7_BLOCK_START = "# >>> baldr-router: context7 managed block >>>"
CODEX_CONTEXT7_BLOCK_END = "# <<< baldr-router: context7 managed block <<<"


def codex_config_path() -> Path:
    return Path.home() / ".codex" / "config.toml"


def context7_codex_toml_block() -> str:
    return f"""{CODEX_CONTEXT7_BLOCK_START}
[mcp_servers.context7]
command = "npx"
args = ["-y", "@upstash/context7-mcp"]
env_vars = ["CONTEXT7_API_KEY"]
startup_timeout_sec = 40
tool_timeout_sec = 60
enabled = true
default_tools_approval_mode = "prompt"
{CODEX_CONTEXT7_BLOCK_END}
"""


def has_managed_context7_block(text: str) -> bool:
    return CODEX_CONTEXT7_BLOCK_START in text and CODEX_CONTEXT7_BLOCK_END in text


def has_context7_table(text: str) -> bool:
    return bool(re.search(r"(?m)^\s*\[mcp_servers\.context7\]\s*$", text))


def remove_managed_context7_block(text: str) -> str:
    pattern = re.compile(
        re.escape(CODEX_CONTEXT7_BLOCK_START)
        + r".*?"
        + re.escape(CODEX_CONTEXT7_BLOCK_END)
        + r"\n?",
        re.DOTALL,
    )
    return pattern.sub("", text)


def install_context7_mcp_config(
    *, force: bool = False, path: Path | None = None
) -> dict[str, Any]:
    p = path or codex_config_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    text = p.read_text(encoding="utf-8") if p.exists() else ""

    if has_context7_table(text) and not has_managed_context7_block(text) and not force:
        return {
            "ok": False,
            "path": str(p),
            "reason": "A non-managed [mcp_servers.context7] table already exists. Re-run with force=True or edit ~/.codex/config.toml manually.",
        }

    backup_path = None
    if p.exists():
        backup_path = p.with_suffix(
            p.suffix + f".bak-{datetime.now().strftime('%Y%m%d%H%M%S')}"
        )
        shutil.copy2(p, backup_path)

    text = remove_managed_context7_block(text)
    if has_context7_table(text) and force:
        # Best-effort removal of an existing context7 table until the next TOML table.
        text = re.sub(
            r"(?ms)^\s*\[mcp_servers\.context7\]\s*\n.*?(?=^\s*\[|\Z)",
            "",
            text,
        )
    text = text.rstrip() + "\n\n" + context7_codex_toml_block() + "\n"
    p.write_text(text, encoding="utf-8")
    return {
        "ok": True,
        "path": str(p),
        "backup_path": str(backup_path) if backup_path else None,
    }


def remove_context7_mcp_config(path: Path | None = None) -> dict[str, Any]:
    p = path or codex_config_path()
    if not p.exists():
        return {"ok": True, "path": str(p), "changed": False}
    text = p.read_text(encoding="utf-8")
    new_text = remove_managed_context7_block(text)
    changed = new_text != text
    if changed:
        backup_path = p.with_suffix(
            p.suffix + f".bak-{datetime.now().strftime('%Y%m%d%H%M%S')}"
        )
        shutil.copy2(p, backup_path)
        p.write_text(new_text.rstrip() + "\n", encoding="utf-8")
        return {
            "ok": True,
            "path": str(p),
            "changed": True,
            "backup_path": str(backup_path),
        }
    return {"ok": True, "path": str(p), "changed": False}


def context7_mcp_config_status(path: Path | None = None) -> dict[str, Any]:
    p = path or codex_config_path()
    if not p.exists():
        return {
            "exists": False,
            "path": str(p),
            "managed": False,
            "context7_table": False,
        }
    text = p.read_text(encoding="utf-8")
    return {
        "exists": True,
        "path": str(p),
        "managed": has_managed_context7_block(text),
        "context7_table": has_context7_table(text),
    }

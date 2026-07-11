from __future__ import annotations

import os
import platform
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

_WINDOWS_DRIVE_RE = re.compile(r"^([a-zA-Z]):[\\/](.*)$")
_WSL_UNC_RE = re.compile(
    r"^\\\\+(?:wsl\$|wsl\.localhost)\\+([^\\]+)\\+(.*)$", re.IGNORECASE
)


def is_wsl() -> bool:
    """Return True when this Python process is running inside WSL."""
    if platform.system().lower() != "linux":
        return False
    if os.environ.get("WSL_DISTRO_NAME") or os.environ.get("WSL_INTEROP"):
        return True
    for probe in (Path("/proc/sys/kernel/osrelease"), Path("/proc/version")):
        try:
            text = probe.read_text(encoding="utf-8", errors="ignore").lower()
        except OSError:
            continue
        if "microsoft" in text or "wsl" in text:
            return True
    return False


def looks_like_windows_path(value: str) -> bool:
    text = str(value).strip()
    return bool(_WINDOWS_DRIVE_RE.match(text)) or bool(_WSL_UNC_RE.match(text))


def windows_path_to_wsl_path(value: str) -> str:
    """Convert a Windows or WSL UNC path to a Linux path.

    Prefer `wslpath` when available. Fallbacks cover the common C:\\... and
    \\wsl.localhost\\Distro\\... shapes.
    """
    raw = str(value).strip()

    unc = _WSL_UNC_RE.match(raw)
    if unc:
        rest = re.sub(r"[\\/]+", "/", unc.group(2))
        if not rest.startswith("/"):
            rest = "/" + rest
        return rest

    if shutil.which("wslpath"):
        try:
            converted = subprocess.check_output(
                ["wslpath", "-u", raw], text=True, stderr=subprocess.DEVNULL
            ).strip()
            if converted:
                return converted
        except Exception:
            pass

    drive = _WINDOWS_DRIVE_RE.match(raw)
    if drive:
        letter = drive.group(1).lower()
        rest = re.sub(r"[\\/]+", "/", drive.group(2)).lstrip("/")
        return f"/mnt/{letter}/{rest}"

    return raw


def normalize_path_for_runtime(value: str | Path) -> Path:
    text = str(value).strip()
    if is_wsl() and looks_like_windows_path(text):
        text = windows_path_to_wsl_path(text)
    return Path(text).expanduser().resolve()


def environment_report() -> dict[str, Any]:
    return {
        "platform": {
            "system": platform.system(),
            "release": platform.release(),
            "machine": platform.machine(),
            "python": platform.python_version(),
        },
        "wsl": {
            "is_wsl": is_wsl(),
            "distro_name": os.environ.get("WSL_DISTRO_NAME"),
            "wsl_interop": bool(os.environ.get("WSL_INTEROP")),
            "wslpath_found": bool(shutil.which("wslpath")),
            "wslpath_path": shutil.which("wslpath"),
        },
        "commands": {
            "baldr_router": shutil.which("baldr-router"),
            "codex": shutil.which("codex"),
            "uv": shutil.which("uv"),
            "npx": shutil.which("npx"),
            "git": shutil.which("git"),
        },
        "path_hints": {
            "note": "If an MCP client runs on Windows and this router runs in WSL, workspace roots may arrive as Windows paths and are normalized with wslpath/fallbacks.",
        },
    }

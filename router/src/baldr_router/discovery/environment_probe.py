from __future__ import annotations

import json
import os
import platform
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from baldr_router import __version__
from baldr_router.platforming import environment_report
from baldr_router.redaction import redact_value

from .fingerprint import file_sha256, stable_json_hash


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _command_version(command: str, args: list[str]) -> dict[str, Any]:
    path = shutil.which(command)
    if not path:
        return {"found": False, "path": None, "version": None}
    try:
        completed = subprocess.run(
            [path, *args],
            text=True,
            capture_output=True,
            timeout=8,
            check=False,
        )
    except Exception as exc:
        return {
            "found": True,
            "path": path,
            "version": None,
            "error": type(exc).__name__,
        }
    output = (completed.stdout or completed.stderr or "").strip().splitlines()
    return {
        "found": True,
        "path": path,
        "version": output[0][:240] if output else None,
        "exit_code": completed.returncode,
    }


def _runtime_receipt() -> dict[str, Any]:
    raw = os.environ.get("BALDR_RUNTIME_RECEIPT_PATH", "").strip()
    candidates: list[Path] = []
    if raw:
        candidates.append(Path(raw).expanduser())
    executable = Path(sys.argv[0]).resolve() if sys.argv else None
    if executable:
        for parent in executable.parents:
            candidate = parent / "runtime.json"
            if candidate not in candidates:
                candidates.append(candidate)
            if len(candidates) >= 8:
                break

    for candidate in candidates:
        if not candidate.exists() or not candidate.is_file():
            continue
        try:
            data = json.loads(candidate.read_text(encoding="utf-8"))
        except Exception as exc:
            return {
                "available": True,
                "valid": False,
                "path": str(candidate),
                "error": type(exc).__name__,
            }
        executable_value = str(data.get("executable") or "")
        wheel_path = str(data.get("wheelPath") or "")
        wheel_hash = str(data.get("wheelSha256") or "")
        actual_hash = ""
        if wheel_path and Path(wheel_path).exists():
            try:
                actual_hash = file_sha256(Path(wheel_path))
            except OSError:
                actual_hash = ""
        executable_ok = bool(executable_value and Path(executable_value).exists())
        hash_ok = not wheel_hash or not actual_hash or wheel_hash == actual_hash
        return {
            "available": True,
            "valid": bool(executable_ok and hash_ok),
            "path": str(candidate),
            "version": data.get("version"),
            "installed_at": data.get("installedAt"),
            "platform": data.get("platform"),
            "source": data.get("source"),
            "executable_exists": executable_ok,
            "wheel_hash_matches": hash_ok,
            "rollback_performed": bool(data.get("rollbackPerformed", False)),
            "previous_version": data.get("previousVersion"),
        }
    return {"available": False, "valid": None, "path": None}


def environment_probe(*, client_id: str | None = None) -> dict[str, Any]:
    base = environment_report()
    commands = {
        "git": _command_version("git", ["--version"]),
        "codex": _command_version("codex", ["--version"]),
        "kiro_cli": _command_version("kiro-cli", ["--version"]),
        "node": _command_version("node", ["--version"]),
        "npm": _command_version("npm", ["--version"]),
        "uv": _command_version("uv", ["--version"]),
    }
    client = {
        "id": client_id or os.environ.get("BALDR_CLIENT_ID") or "unknown",
        "version": os.environ.get("BALDR_CLIENT_VERSION") or None,
        "workspace_trust_declared": bool(
            os.environ.get("BALDR_TRUSTED_WORKSPACE_ROOTS_JSON")
        ),
    }
    stable = {
        "baldr_version": __version__,
        "system": platform.system(),
        "release": platform.release(),
        "machine": platform.machine(),
        "python": platform.python_version(),
        "is_wsl": bool(base.get("wsl", {}).get("is_wsl")),
        "wsl_distro": base.get("wsl", {}).get("distro_name"),
        "client_id": client["id"],
        "command_availability": {
            name: bool(status.get("found")) for name, status in commands.items()
        },
    }
    report = {
        "ok": True,
        "schema_version": 1,
        "generated_at": _utc_now(),
        "fingerprint": stable_json_hash(stable),
        "baldr": {
            "version": __version__,
            "python_executable": sys.executable,
            "argv0": sys.argv[0] if sys.argv else None,
        },
        "platform": base.get("platform", {}),
        "wsl": base.get("wsl", {}),
        "client": client,
        "commands": commands,
        "runtime_receipt": _runtime_receipt(),
        "privacy": {
            "raw_environment_exported": False,
            "secret_values_included": False,
            "fingerprint_is_sha256": True,
        },
    }
    return redact_value(report)

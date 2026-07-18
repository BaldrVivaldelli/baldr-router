from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import baldr_agent_sdk
from baldr_agent_sdk.contract import ContractError, canonical_json

from .models import ProjectSpec
from .release import JsonRunner, default_install_root, run_json_command


def run_project_tests(
    project: ProjectSpec,
    *,
    capture_output: bool = False,
) -> Mapping[str, Any]:
    command = [
        sys.executable if item == "{python}" else item for item in project.test_command
    ]
    env = os.environ.copy()
    sdk_parent = str(Path(baldr_agent_sdk.__file__).resolve().parent.parent)
    previous = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = sdk_parent + (os.pathsep + previous if previous else "")
    process = subprocess.run(
        command,
        cwd=project.root,
        env=env,
        check=False,
        capture_output=capture_output,
        text=capture_output,
    )
    if process.returncode:
        detail = ""
        if capture_output:
            output = ((process.stdout or "") + (process.stderr or "")).strip()
            if output:
                detail = " " + output[-2000:]
        raise ContractError(
            f"Agent tests failed with exit code {process.returncode}.{detail}"
        )
    return {"ok": True, "command": command, "exit_code": process.returncode}


def project_doctor(
    project: ProjectSpec,
    *,
    install_root: str | Path | None = None,
    runner: JsonRunner = run_json_command,
) -> Mapping[str, Any]:
    checks: list[dict[str, Any]] = []

    def check(name: str, ok: bool, detail: str) -> None:
        checks.append({"name": name, "ok": bool(ok), "detail": detail})

    for source in project.sources:
        path = project.root.joinpath(*source.parts)
        check(f"source:{source}", path.exists() and not path.is_symlink(), str(path))
    for command in dict.fromkeys(
        (project.runtime_command, "baldr-agent-runner", "baldr-router")
    ):
        resolved = shutil.which(command)
        check(f"command:{command}", bool(resolved), resolved or "not found")
    base = (
        Path(install_root).expanduser().resolve()
        if install_root is not None
        else default_install_root()
    )
    release_root = (
        base / project.registry / project.namespace / project.name / project.version
    )
    release_path = release_root / "release.json"
    if release_path.is_file() and not release_path.is_symlink():
        release = json.loads(release_path.read_text(encoding="utf-8"))
        artifact = Path(str(release.get("artifact") or ""))
        expected = str(release.get("artifact_digest") or "")
        actual = (
            "sha256:" + hashlib.sha256(artifact.read_bytes()).hexdigest()
            if artifact.is_file() and not artifact.is_symlink()
            else ""
        )
        check("installed-release", actual == expected, str(release_path))
    else:
        check("installed-release", False, "not published")
    try:
        health = runner(["baldr-agent-runner", "health"], project.root)
        check("runner-health", health.get("status") == "ok", canonical_json(health))
    except ContractError as exc:
        check("runner-health", False, str(exc))
    try:
        catalog = runner(
            ["baldr-router", "agent", "list", "--workspace", str(project.root)],
            project.root,
        )
        agents = catalog.get("agents") if isinstance(catalog, dict) else None
        ready = {
            str(item.get("ref"))
            for item in agents or []
            if isinstance(item, dict) and item.get("ready")
        }
        expected_refs = {project.reference(role) for role in project.roles}
        check(
            "catalog-ready",
            expected_refs.issubset(ready),
            f"{len(expected_refs & ready)}/{len(expected_refs)} ready",
        )
    except ContractError as exc:
        check("catalog-ready", False, str(exc))
    return {"ok": all(item["ok"] for item in checks), "checks": checks}

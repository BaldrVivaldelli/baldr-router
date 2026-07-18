from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import tarfile
import time
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
VERSION = "0.20.0"
DIST = ROOT / "dist"
ARTIFACTS = DIST / "artifacts"
PYTHON_DIST = ARTIFACTS / "python"
NODE_DIST = ARTIFACTS / "node"
VALIDATION_DIR = DIST / "validation"
METADATA_DIR = DIST / "metadata"
EXTENSION = ROOT / "facades" / "vscode-extension"
EXTENSION_RUNTIME = EXTENSION / "resources" / "runtime"
ADAPTER = ROOT / "facades" / "kiro" / "adapter"
VALIDATION: list[dict[str, Any]] = []


def executable(name: str) -> str:
    found = shutil.which(name)
    if not found:
        raise SystemExit(f"Required executable not found: {name}")
    return found


def require_zip_members(archive: Path, expected: set[str], *, label: str) -> None:
    with zipfile.ZipFile(archive) as bundle:
        members = set(bundle.namelist())
    missing = sorted(expected - members)
    if missing:
        raise SystemExit(f"{label} is missing required files: {', '.join(missing)}")


def require_tar_members(archive: Path, expected: set[str], *, label: str) -> None:
    with tarfile.open(archive, "r:gz") as bundle:
        members = set(bundle.getnames())
    missing = sorted(expected - members)
    if missing:
        raise SystemExit(f"{label} is missing required files: {', '.join(missing)}")


def _portable(value: Any, *, replacements: dict[str, str] | None = None) -> Any:
    mapping = {
        str(ROOT): "<source-root>",
        str(Path.home()): "~",
        str(Path(tempfile.gettempdir()).resolve()): "<temp>",
        **(replacements or {}),
    }
    if isinstance(value, str):
        result = value
        for source, target in sorted(mapping.items(), key=lambda item: len(item[0]), reverse=True):
            if source:
                result = result.replace(source, target)
        return result
    if isinstance(value, dict):
        return {str(key): _portable(item, replacements=mapping) for key, item in value.items()}
    if isinstance(value, list):
        return [_portable(item, replacements=mapping) for item in value]
    return value


def run(
    *args: str,
    cwd: Path = ROOT,
    label: str | None = None,
    env: dict[str, str] | None = None,
    echo_output: bool = True,
    allowed_returncodes: tuple[int, ...] = (0,),
) -> str:
    print("+", " ".join(args))
    if echo_output:
        completed = subprocess.run(
            args,
            cwd=cwd,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            check=False,
            env=env,
        )
        output = completed.stdout or ""
        if output:
            print(output, end="" if output.endswith("\n") else "\n")
    else:
        with tempfile.TemporaryFile(mode="w+t", encoding="utf-8") as capture:
            process = subprocess.Popen(
                args,
                cwd=cwd,
                text=True,
                stdout=capture,
                stderr=subprocess.STDOUT,
                env=env,
            )
            while process.poll() is None:
                print("  …", flush=True)
                time.sleep(2)
            capture.seek(0)
            output = capture.read()
            completed = subprocess.CompletedProcess(args, process.returncode, output)
    VALIDATION.append(
        {
            "label": label or " ".join(args),
            "ok": completed.returncode in allowed_returncodes,
            "returncode": completed.returncode,
            "command": list(args),
            "cwd": str(cwd.relative_to(ROOT) if cwd.is_relative_to(ROOT) else "<external>"),
        }
    )
    if completed.returncode not in allowed_returncodes:
        raise subprocess.CalledProcessError(completed.returncode, args, output=output)
    return output


def parse_json_output(output: str, *, label: str) -> dict[str, Any]:
    start = output.find("{")
    if start < 0:
        raise SystemExit(f"{label} did not return JSON: {output[-1000:]}")
    try:
        value = json.loads(output[start:])
    except json.JSONDecodeError as exc:
        raise SystemExit(f"{label} returned invalid JSON: {exc}: {output[-2000:]}") from exc
    if not isinstance(value, dict):
        raise SystemExit(f"{label} JSON root must be an object")
    return value


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def zip_directory(
    source: Path,
    target: Path,
    *,
    root_name: str | None = None,
    exclude_parts: set[str] | None = None,
) -> None:
    excluded = {
        "node_modules", "__pycache__", ".pytest_cache", ".ruff_cache",
        ".mypy_cache", ".venv", "dist", ".git",
    }
    excluded.update(exclude_parts or set())
    target.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for item in sorted(source.rglob("*")):
            if not item.is_file() or any(part in excluded for part in item.parts):
                continue
            if item.suffix.lower() in {".pyc", ".sqlite3", ".vsix"}:
                continue
            relative = item.relative_to(source)
            arcname = Path(root_name) / relative if root_name else relative
            archive.write(item, arcname.as_posix())


def zip_selected(entries: list[tuple[Path, str]], target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for path, arcname in entries:
            if path.is_dir():
                for item in sorted(path.rglob("*")):
                    if item.is_file():
                        archive.write(item, (Path(arcname) / item.relative_to(path)).as_posix())
            elif path.is_file():
                archive.write(path, arcname)


def _write_bootstrap_smoke_wheel(target: Path) -> Path:
    distribution = "baldr_router_bootstrap_smoke"
    dist_info = f"{distribution}-{VERSION}.dist-info"
    files = {
        "baldr_bootstrap_smoke.py": (
            f"VERSION = {VERSION!r}\n"
            "def main():\n"
            "    print(VERSION)\n"
            "    return 0\n"
            "if __name__ == '__main__':\n"
            "    raise SystemExit(main())\n"
        ),
        f"{dist_info}/METADATA": (
            "Metadata-Version: 2.1\n"
            "Name: baldr-router-bootstrap-smoke\n"
            f"Version: {VERSION}\n"
            "Summary: Dependency-free Baldr bootstrap smoke package\n"
        ),
        f"{dist_info}/WHEEL": (
            "Wheel-Version: 1.0\nGenerator: baldr-router-release\n"
            "Root-Is-Purelib: true\nTag: py3-none-any\n"
        ),
        f"{dist_info}/entry_points.txt": "[console_scripts]\nbaldr-router = baldr_bootstrap_smoke:main\n",
    }
    target.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, content in files.items():
            archive.writestr(name, content)
        record = "".join(f"{name},,\n" for name in files) + f"{dist_info}/RECORD,,\n"
        archive.writestr(f"{dist_info}/RECORD", record)
    return target


def bootstrap_runtime_validation(node: str) -> None:
    with tempfile.TemporaryDirectory(prefix="baldr-bootstrap-") as temp:
        root = Path(temp)
        wheel = _write_bootstrap_smoke_wheel(root / f"baldr_router_bootstrap_smoke-{VERSION}-py3-none-any.whl")
        runtime_dir = root / "runtime"
        bootstrap = EXTENSION / "runtime" / "baldr-bootstrap.mjs"
        env = {
            **os.environ,
            "BALDR_BUNDLED_WHEEL": str(wheel),
            "BALDR_VSCODE_RUNTIME_DIR": str(runtime_dir),
            "BALDR_ROUTER_AUTO_INSTALL": "1",
            "BALDR_ROUTER_PREFER_MANAGED": "1",
            "BALDR_ROUTER_LAUNCHER_MODE": "host",
            "BALDR_ROUTER_KEEP_VERSIONS": "2",
            "BALDR_TRUSTED_WORKSPACE_ROOTS_JSON": "[]",
        }
        first = json.loads(run(node, str(bootstrap), "ensure", env=env, label="private runtime bootstrap"))
        second = json.loads(run(node, str(bootstrap), "ensure", env=env, label="private runtime reuse"))
        if not first.get("ok") or first.get("executable") != second.get("executable"):
            raise SystemExit(f"Private runtime bootstrap/reuse failed: {first=} {second=}")
        reported = run(node, str(bootstrap), "exec", "--", "--version", env=env, label="private runtime smoke").strip()
        if reported != VERSION:
            raise SystemExit(f"Private runtime version mismatch: {reported!r}")


def isolated_python_validation(
    uv: str,
    core_wheel: Path,
    adapter_wheel: Path,
    sdk_wheel: Path,
    builder_wheel: Path,
    runner_wheel: Path,
) -> None:
    with tempfile.TemporaryDirectory(prefix="baldr-isolated-") as temp:
        root = Path(temp)
        venv = root / "venv"
        run(uv, "venv", str(venv), label="isolated Python environment")
        python = venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        run(
            uv,
            "pip",
            "install",
            "--python",
            str(python),
            str(core_wheel),
            str(adapter_wheel),
            str(sdk_wheel),
            str(builder_wheel),
            str(runner_wheel),
            label="isolated wheel install",
        )
        code = f"""
import asyncio, json
from baldr_agent_runner import __version__ as runner_version
from baldr_agent_sdk import __version__ as sdk_version
from baldr_agent_builder import __version__ as builder_version
from baldr_router import __version__
from baldr_router.extensions import load_installed_extensions, extension_status
from baldr_router.server import mcp
load_installed_extensions(mcp)
tools = sorted(tool.name for tool in asyncio.run(mcp.list_tools()))
status = extension_status()
assert __version__ == {VERSION!r}
assert sdk_version == {VERSION!r}
assert builder_version == {VERSION!r}
assert runner_version == {VERSION!r}
assert any(item.get('adapter') == 'kiro' for item in status.get('results', [])), status
assert 'kiro_install_workspace' in tools
print(json.dumps({{'version': __version__, 'sdk': sdk_version, 'builder': builder_version, 'runner': runner_version, 'kiro_tool_loaded': True}}))
"""
        run(str(python), "-c", code, label="isolated adapter discovery")
        runner = venv / (
            "Scripts/baldr-agent-runner.exe"
            if os.name == "nt"
            else "bin/baldr-agent-runner"
        )
        health = parse_json_output(
            run(str(runner), "health", label="isolated agent runner health"),
            label="isolated agent runner health",
        )
        if health.get("status") != "ok" or 1 not in health.get("protocols", []):
            raise SystemExit(f"Agent runner health failed: {health}")
        agent_cli = venv / (
            "Scripts/baldr-agent.exe" if os.name == "nt" else "bin/baldr-agent"
        )
        help_text = run(
            str(agent_cli), "--help", label="isolated external agent CLI"
        )
        for command in (
            "init",
            "test",
            "build",
            "publish",
            "run",
            "doctor",
            "rollback",
        ):
            if command not in help_text:
                raise SystemExit(
                    f"External agent CLI does not expose {command!r}: {help_text}"
                )
        driver_help = run(
            str(agent_cli), "driver", "--help", label="isolated driver CLI"
        )
        if "conformance" not in driver_help:
            raise SystemExit(
                "External agent CLI does not expose driver conformance: "
                f"{driver_help}"
            )


def isolated_typescript_distribution_validation(
    uv: str,
    npm: str,
    node: str,
    core_wheel: Path,
    sdk_wheel: Path,
    builder_wheel: Path,
    runner_wheel: Path,
    typescript_sdk_package: Path,
    typescript_driver_package: Path,
) -> None:
    """Prove the published Node packages work without a source checkout."""

    with tempfile.TemporaryDirectory(prefix="baldr-typescript-distribution-") as temp:
        root = Path(temp)
        venv = root / "venv"
        npm_prefix = root / "npm-prefix"
        project = root / "external-agent"
        install_root = root / "installed-agents"
        run(uv, "venv", str(venv), label="TypeScript distribution Python environment")
        python = venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        scripts = python.parent
        run(
            uv,
            "pip",
            "install",
            "--python",
            str(python),
            str(core_wheel),
            str(sdk_wheel),
            str(builder_wheel),
            str(runner_wheel),
            label="TypeScript distribution wheel install",
        )
        npm_env = {
            **os.environ,
            "npm_config_cache": str(root / "npm-cache"),
        }
        run(
            npm,
            "install",
            "--global",
            "--prefix",
            str(npm_prefix),
            "--ignore-scripts",
            "--no-audit",
            "--no-fund",
            str(typescript_sdk_package),
            str(typescript_driver_package),
            cwd=root,
            env=npm_env,
            label="isolated TypeScript package install",
        )
        npm_bin = npm_prefix if os.name == "nt" else npm_prefix / "bin"
        isolated_tools = root / "tool-bin"
        isolated_tools.mkdir()
        if os.name == "nt":
            shutil.copy2(node, isolated_tools / "node.exe")
        else:
            (isolated_tools / "node").symlink_to(Path(node).resolve())
        isolated_path = (
            str(npm_bin),
            str(scripts),
            str(isolated_tools),
            str(Path(executable("git")).resolve().parent),
        )
        env = {
            **os.environ,
            # Do not inherit globally installed Baldr drivers. The release
            # smoke must prove only the freshly installed packages plus the
            # explicit platform prerequisites (Node and Git).
            "PATH": os.pathsep.join(dict.fromkeys(isolated_path)),
            "XDG_CONFIG_HOME": str(root / "config"),
            "XDG_CACHE_HOME": str(root / "cache"),
            "XDG_STATE_HOME": str(root / "state"),
            "XDG_DATA_HOME": str(root / "data"),
            "BALDR_AGENT_REGISTRY_PATH": str(root / "catalog" / "agents.json"),
            "BALDR_AGENT_INSTALL_ROOT": str(install_root),
        }
        agent_cli = scripts / (
            "baldr-agent.exe" if os.name == "nt" else "baldr-agent"
        )
        router_cli = scripts / (
            "baldr-router.exe" if os.name == "nt" else "baldr-router"
        )
        source_driver = (
            ROOT / "tooling" / "agent-builder-typescript" / "dist" / "driver.js"
        )
        descriptor_code = (
            f"const m = await import({json.dumps(source_driver.as_uri())}); "
            "console.log(JSON.stringify(m.descriptor()));"
        )
        source_descriptor = parse_json_output(
            run(
                node,
                "--input-type=module",
                "--eval",
                descriptor_code,
                label="source TypeScript driver identity",
            ),
            label="source TypeScript driver identity",
        )
        installed_status = parse_json_output(
            run(
                str(agent_cli),
                "driver",
                "doctor",
                "baldr.typescript",
                cwd=root,
                env=env,
                label="PATH-discovered TypeScript driver",
            ),
            label="PATH-discovered TypeScript driver",
        )
        installed_drivers = installed_status.get("drivers") or []
        if len(installed_drivers) != 1:
            raise SystemExit(
                f"Expected one installed TypeScript driver: {installed_status}"
            )
        installed_descriptor = installed_drivers[0]
        if installed_descriptor.get("origin") != "PATH":
            raise SystemExit(
                f"Packaged TypeScript driver was not discovered on PATH: {installed_descriptor}"
            )
        if installed_descriptor.get("digest") != source_descriptor.get("digest"):
            raise SystemExit(
                "TypeScript driver identity changed after package installation: "
                f"{source_descriptor=} {installed_descriptor=}"
            )

        run(
            str(agent_cli),
            "init",
            str(project),
            "--name",
            "distribution-agent",
            "--owner",
            "release-test",
            "--namespace",
            "distribution",
            "--language",
            "typescript",
            cwd=root,
            env=env,
            label="isolated TypeScript agent scaffold",
        )
        run(
            str(agent_cli),
            "test",
            "--project",
            str(project),
            cwd=root,
            env=env,
            label="isolated TypeScript agent tests",
        )
        conformance = parse_json_output(
            run(
                str(agent_cli),
                "driver",
                "conformance",
                "baldr.typescript",
                "--project",
                str(project),
                "--output-root",
                str(root / "conformance"),
                cwd=root,
                env=env,
                label="installed TypeScript driver conformance",
            ),
            label="installed TypeScript driver conformance",
        )
        if conformance.get("ok") is not True or not all(
            item.get("ok") is True for item in conformance.get("checks", [])
        ):
            raise SystemExit(
                f"Installed TypeScript driver failed conformance: {conformance}"
            )
        run_workspace = root / "run-workspace"
        run_workspace.mkdir()
        direct_run = parse_json_output(
            run(
                str(agent_cli),
                "run",
                "--project",
                str(project),
                "--role",
                "implementer",
                "--workspace",
                str(run_workspace),
                "--request",
                "Create the generated TypeScript result",
                "--output-dir",
                str(root / "run-build"),
                cwd=root,
                env=env,
                label="direct TypeScript agent execution",
            ),
            label="direct TypeScript agent execution",
        )
        if (
            direct_run.get("ok") is not True
            or direct_run.get("state") != "succeeded"
            or not (run_workspace / "distribution-agent_result.md").is_file()
        ):
            raise SystemExit(
                f"Installed TypeScript agent direct run failed: {direct_run}"
            )
        first = parse_json_output(
            run(
                str(agent_cli),
                "build",
                "--project",
                str(project),
                "--output-dir",
                str(root / "build-one"),
                cwd=root,
                env=env,
                label="first isolated TypeScript build",
            ),
            label="first isolated TypeScript build",
        )
        second = parse_json_output(
            run(
                str(agent_cli),
                "build",
                "--project",
                str(project),
                "--output-dir",
                str(root / "build-two"),
                cwd=root,
                env=env,
                label="second isolated TypeScript build",
            ),
            label="second isolated TypeScript build",
        )
        first_artifact = Path(str(first.get("artifact") or ""))
        second_artifact = Path(str(second.get("artifact") or ""))
        if (
            first.get("artifact_digest") != second.get("artifact_digest")
            or not first_artifact.is_file()
            or first_artifact.read_bytes() != second_artifact.read_bytes()
        ):
            raise SystemExit("Installed TypeScript packages produced a non-reproducible build")

        first_publication = parse_json_output(
            run(
                str(agent_cli),
                "publish",
                "--project",
                str(project),
                "--install-root",
                str(install_root),
                cwd=root,
                env=env,
                label="publish isolated TypeScript agent 1.0.0",
            ),
            label="publish isolated TypeScript agent 1.0.0",
        )
        doctor = parse_json_output(
            run(
                str(agent_cli),
                "doctor",
                "--project",
                str(project),
                "--install-root",
                str(install_root),
                cwd=root,
                env=env,
                label="diagnose isolated TypeScript agent",
            ),
            label="diagnose isolated TypeScript agent",
        )
        if first_publication.get("ok") is not True or doctor.get("ok") is not True:
            raise SystemExit(
                f"Initial TypeScript release is unhealthy: {first_publication=} {doctor=}"
            )

        facade_runs: dict[str, dict[str, Any]] = {}
        role_references = {
            "architect": "local://distribution/distribution-agent-planner@1.0.0",
            "implementer": "local://distribution/distribution-agent-writer@1.0.0",
            "reviewer": "local://distribution/distribution-agent-reviewer@1.0.0",
        }
        facade_workspaces = {
            client: root / f"{client}-workspace"
            for client in ("vscode-extension", "kiro")
        }
        facade_env = {
            **env,
            "BALDR_TRUSTED_WORKSPACE_ROOTS_JSON": json.dumps(
                [str(path) for path in facade_workspaces.values()]
            ),
        }
        for client, workspace in facade_workspaces.items():
            workspace.mkdir()
            run(
                executable("git"),
                "init",
                "-q",
                str(workspace),
                cwd=root,
                env=facade_env,
                label=f"initialize {client} facade workspace",
            )
            arguments = [
                str(router_cli),
                "facade",
                "run",
                str(workspace),
                "Create the generated TypeScript result through the shared facade",
                "--workspace-mode",
                "current",
                "--team-mode",
                "automatic",
            ]
            for role, reference in role_references.items():
                arguments.extend(("--agent-override", f"{role}={reference}"))
            arguments.extend(("--client", client))
            facade_result = parse_json_output(
                run(
                    *arguments,
                    cwd=root,
                    env=facade_env,
                    label=f"installed {client} external-agent facade execution",
                ),
                label=f"installed {client} external-agent facade execution",
            )
            actual_references = {
                str(step.get("phase")): str(profile.get("agent_ref"))
                for step in facade_result.get("steps", [])
                if isinstance(step, dict)
                for profile in step.get("profiles", [])
                if isinstance(profile, dict)
            }
            if (
                facade_result.get("ok") is not True
                or facade_result.get("status") != "approved"
                or (facade_result.get("facade") or {}).get("client") != client
                or actual_references != role_references
                or not (workspace / "distribution-agent_result.md").is_file()
            ):
                raise SystemExit(
                    f"Installed {client} facade execution failed: {facade_result}"
                )
            facade_runs[client] = {
                "status": facade_result.get("status"),
                "workflow": facade_result.get("workflow"),
                "roles": actual_references,
                "write_authorization_requested": (
                    (facade_result.get("error") or {}).get("code")
                    == "write_authorization_required"
                ),
            }

        config = project / "baldr-agent.toml"
        configured = config.read_text(encoding="utf-8")
        if 'version = "1.0.0"' not in configured:
            raise SystemExit("Generated project did not declare version 1.0.0")
        config.write_text(
            configured.replace('version = "1.0.0"', 'version = "1.1.0"', 1),
            encoding="utf-8",
        )
        entrypoint = project / "src" / "agent.ts"
        entrypoint.write_text(
            entrypoint.read_text(encoding="utf-8")
            + "\n// Distribution update 1.1.0.\n",
            encoding="utf-8",
        )
        second_publication = parse_json_output(
            run(
                str(agent_cli),
                "publish",
                "--project",
                str(project),
                "--install-root",
                str(install_root),
                cwd=root,
                env=env,
                label="publish isolated TypeScript agent 1.1.0",
            ),
            label="publish isolated TypeScript agent 1.1.0",
        )
        entrypoint.write_text(
            entrypoint.read_text(encoding="utf-8")
            + "// Illegal replacement without a version bump.\n",
            encoding="utf-8",
        )
        immutable_failure = parse_json_output(
            run(
                str(agent_cli),
                "publish",
                "--project",
                str(project),
                "--install-root",
                str(install_root),
                cwd=root,
                env=env,
                label="reject changed immutable TypeScript release",
                allowed_returncodes=(2,),
            ),
            label="reject changed immutable TypeScript release",
        )
        if "bump version" not in str(
            (immutable_failure.get("error") or {}).get("message") or ""
        ):
            raise SystemExit(
                f"Changed immutable release was not rejected: {immutable_failure}"
            )
        rollback = parse_json_output(
            run(
                str(agent_cli),
                "rollback",
                "1.0.0",
                "--project",
                str(project),
                cwd=root,
                env=env,
                label="rollback isolated TypeScript agent",
            ),
            label="rollback isolated TypeScript agent",
        )
        catalog = parse_json_output(
            run(
                str(router_cli),
                "agent",
                "list",
                "--workspace",
                str(project),
                cwd=root,
                env=env,
                label="inspect rolled back TypeScript catalog",
            ),
            label="inspect rolled back TypeScript catalog",
        )
        agents = [item for item in catalog.get("agents", []) if isinstance(item, dict)]
        enabled_versions = {
            str(item.get("version")) for item in agents if item.get("enabled")
        }
        if (
            second_publication.get("ok") is not True
            or rollback.get("version") != "1.0.0"
            or enabled_versions != {"1.0.0"}
        ):
            raise SystemExit(
                f"TypeScript update/rollback validation failed: {rollback=} {agents=}"
            )

        installed_text = "\n".join(
            path.read_text(encoding="utf-8", errors="replace")
            for path in sorted(install_root.rglob("*"))
            if path.is_file()
        )
        if str(ROOT) in installed_text:
            raise SystemExit("Installed TypeScript release leaked the source checkout path")
        write_json(
            VALIDATION_DIR / "typescript-distribution.json",
            {
                "ok": True,
                "packages": {
                    "sdk": typescript_sdk_package.name,
                    "driver": typescript_driver_package.name,
                },
                "driver": installed_descriptor,
                "artifact_digest": first.get("artifact_digest"),
                "conformance": conformance,
                "direct_run": {
                    "agent": direct_run.get("agent"),
                    "role": direct_run.get("role"),
                    "state": direct_run.get("state"),
                },
                "shared_facade_runs": facade_runs,
                "published_versions": ["1.0.0", "1.1.0"],
                "immutable_replacement_rejected": True,
                "rollback": rollback,
                "checkout_path_leaked": False,
                "platform": "windows" if os.name == "nt" else "linux-posix",
            },
            replacements={str(root): "<isolated-distribution>"},
        )


def write_json(path: Path, value: Any, *, replacements: dict[str, str] | None = None) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(_portable(value, replacements=replacements), indent=2, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return path


def build_validation_report() -> Path:
    report = {
        "release": VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "feature_freeze": True,
        "qualification_stage": "narrative-progress-ux",
        "live_environment_qualified": False,
        "live_environment_note": (
            "Build and synthetic validation cannot qualify a real client environment. "
            "Run `baldr-router qualification run` from the target VS Code/Kiro machine."
        ),
        "automated_steps": VALIDATION,
        "ok": all(bool(item.get("ok")) for item in VALIDATION),
    }
    return write_json(VALIDATION_DIR / "build-validation.json", report)


def write_release_manifest(bundles: dict[str, Path]) -> Path:
    files = [
        path
        for path in sorted(ARTIFACTS.rglob("*"))
        if path.is_file() and not path.name.startswith(".")
    ]
    files += [path for path in sorted(METADATA_DIR.rglob("*")) if path.is_file()]
    files += [path for path in sorted(VALIDATION_DIR.rglob("*")) if path.is_file()]
    files += list(bundles.values())
    manifest = {
        "schema_version": 1,
        "version": VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "feature_freeze": True,
        "primary_vscode_experience": "baldr-console",
        "qualification_required_for_promotion": True,
        "bundles": {name: path.relative_to(DIST).as_posix() for name, path in bundles.items()},
        "artifacts": [
            {
                "path": path.relative_to(DIST).as_posix(),
                "sha256": sha256(path),
                "bytes": path.stat().st_size,
            }
            for path in files
        ],
    }
    return write_json(DIST / "release-manifest.json", manifest)


def write_checksums() -> Path:
    checksum_path = DIST / "SHA256SUMS.txt"
    artifacts = [path for path in sorted(DIST.rglob("*")) if path.is_file() and path != checksum_path]
    checksum_path.write_text(
        "\n".join(f"{sha256(path)}  {path.relative_to(DIST).as_posix()}" for path in artifacts) + "\n",
        encoding="utf-8",
    )
    return checksum_path


def main() -> int:
    parser = argparse.ArgumentParser(description="Build Baldr 0.20 polyglot-agent artifacts")
    parser.add_argument("--skip-tests", action="store_true")
    parser.add_argument("--keep-dist", action="store_true")
    args = parser.parse_args()

    uv = executable("uv")
    npm = executable("npm")
    node = executable("node")

    if DIST.exists() and not args.keep_dist:
        shutil.rmtree(DIST)
    for directory in (
        PYTHON_DIST,
        NODE_DIST,
        VALIDATION_DIR,
        METADATA_DIR,
        EXTENSION_RUNTIME,
    ):
        directory.mkdir(parents=True, exist_ok=True)
    for old_wheel in EXTENSION_RUNTIME.glob("baldr_router-*.whl"):
        old_wheel.unlink()

    run(sys.executable, "scripts/check_release_consistency.py", label="release consistency")
    run(sys.executable, "scripts/generate_facades.py", label="generate shared facades")
    run(sys.executable, "scripts/generate_facades.py", "--check", label="facade conformance")
    shutil.copy2(ROOT / "launcher" / "lib" / "runtime-bootstrap.mjs", EXTENSION / "runtime" / "runtime-bootstrap.mjs")

    if not args.skip_tests:
        run(sys.executable, "scripts/dev.py", "test", label="all component tests")
        run(sys.executable, "scripts/dev.py", "lint", label="all lint and contract checks")

    with tempfile.TemporaryDirectory(prefix="baldr-release-state-") as temp:
        state = Path(temp)
        validation_env = {
            **os.environ,
            "XDG_CONFIG_HOME": str(state / "config"),
            "XDG_CACHE_HOME": str(state / "cache"),
            "XDG_STATE_HOME": str(state / "state"),
            "BALDR_CLIENT_ID": "release-build",
            "BALDR_CLIENT_VERSION": VERSION,
        }
        replacements = {str(state): "<validation-state>"}
        status_raw = run(
            uv, "run", "baldr-router", "workflow-status",
            cwd=ROOT / "router", env=validation_env,
            label="durable SQLite and execution-profile status", echo_output=False,
        )
        status = parse_json_output(status_raw, label="durable status")
        schema = ((status.get("durability") or {}).get("schema") or {})
        if schema.get("schema_version") != schema.get("latest_available"):
            raise SystemExit(f"Durable schema is not current: {schema}")
        write_json(VALIDATION_DIR / "durable-status.json", status, replacements=replacements)

        self_test = parse_json_output(
            run(
                uv, "run", "baldr-router", "verify", "--mode", "full", "--client", "release-build",
                cwd=ROOT / "router", env=validation_env,
                label="deterministic lifecycle self-test", echo_output=False,
            ),
            label="lifecycle self-test",
        )
        if self_test.get("ok") is not True:
            raise SystemExit("Lifecycle self-test failed")
        write_json(VALIDATION_DIR / "self-test.json", self_test, replacements=replacements)

        lab = parse_json_output(
            run(
                uv, "run", "baldr-router", "lab", "--mode", "quick", "--repeat", "3", "--profile", "release-build",
                cwd=ROOT / "router", env=validation_env,
                label="three-pass validation lab", echo_output=False,
            ),
            label="validation lab",
        )
        if lab.get("acceptance_met") is not True:
            raise SystemExit("Validation lab did not meet the three-pass threshold")
        write_json(VALIDATION_DIR / "lab-validation.json", lab, replacements=replacements)

        qualification_code = (
            "import json; "
            "from baldr_router.qualification import run_qualification; "
            "print(json.dumps(run_qualification(profile_id='vscode-linux-native', "
            "repeat=3, include_provider_smoke=False, client_id='vscode-extension')))"
        )
        qualification = parse_json_output(
            run(
                uv, "run", "python", "-c", qualification_code,
                cwd=ROOT / "router", env=validation_env,
                label="synthetic qualification remains provisional", echo_output=False,
            ),
            label="synthetic qualification",
        )
        if qualification.get("status") != "provisional":
            raise SystemExit(f"Synthetic build must not claim real qualification: {qualification}")
        write_json(VALIDATION_DIR / "synthetic-qualification.json", qualification, replacements=replacements)

    run(uv, "build", "router", "--out-dir", str(PYTHON_DIST), label="build core wheel and sdist")
    run(uv, "build", "facades/kiro/adapter", "--out-dir", str(PYTHON_DIST), label="build Kiro adapter wheel and sdist")
    run(uv, "build", "sdks/python", "--out-dir", str(PYTHON_DIST), label="build agent SDK wheel and sdist")
    run(uv, "build", "tooling/agent-builder", "--out-dir", str(PYTHON_DIST), label="build Agent Builder wheel and sdist")
    run(uv, "build", "runtimes/agent-runner", "--out-dir", str(PYTHON_DIST), label="build agent runner wheel and sdist")
    core_wheels = sorted(PYTHON_DIST.glob(f"baldr_router-{VERSION}-*.whl"))
    adapter_wheels = sorted(PYTHON_DIST.glob(f"baldr_kiro_adapter-{VERSION}-*.whl"))
    sdk_wheels = sorted(PYTHON_DIST.glob(f"baldr_agent_sdk-{VERSION}-*.whl"))
    builder_wheels = sorted(PYTHON_DIST.glob(f"baldr_agent_builder-{VERSION}-*.whl"))
    runner_wheels = sorted(PYTHON_DIST.glob(f"baldr_agent_runner-{VERSION}-*.whl"))
    if not all(
        len(items) == 1
        for items in (
            core_wheels,
            adapter_wheels,
            sdk_wheels,
            builder_wheels,
            runner_wheels,
        )
    ):
        raise SystemExit(
            "Expected one core, adapter, SDK, Builder, and runner wheel: "
            f"{core_wheels=} {adapter_wheels=} {sdk_wheels=} "
            f"{builder_wheels=} {runner_wheels=}"
        )
    core_wheel, adapter_wheel = core_wheels[0], adapter_wheels[0]
    sdk_wheel = sdk_wheels[0]
    builder_wheel = builder_wheels[0]
    runner_wheel = runner_wheels[0]
    require_zip_members(
        core_wheel,
        {
            "baldr_router/provider_activity.py",
            "baldr_router/phase_deliverables.py",
            "baldr_router/work_item_progress.py",
            "baldr_router/contracts/phase-deliverable-v1.schema.json",
            "baldr_router/contracts/phase-deliverable-page-v1.schema.json",
            "baldr_router/contracts/phase-deliverable-index-page-v1.schema.json",
            "baldr_router/contracts/work-item-progress-v1.schema.json",
            "baldr_router/contracts/agent-registry-v1.schema.json",
            "baldr_router/contracts/agent-transport-http-v1.schema.json",
            "baldr_router/contracts/agent-manager-v1.schema.json",
            "baldr_router/contracts/agent-source-v1.schema.json",
            "baldr_router/contracts/agent-catalog-sync-v1.schema.json",
            "baldr_router/contracts/agent-team-resolution-v1.schema.json",
            "baldr_router/contracts/orchestration-policy-v1.schema.json",
            "baldr_router/contracts/agent-execution-v1.schema.json",
        },
        label="Core wheel",
    )
    require_zip_members(
        sdk_wheel,
        {
            "baldr_agent_sdk/__init__.py",
            "baldr_agent_sdk/agent.py",
            "baldr_agent_sdk/contract.py",
        },
        label="Agent SDK wheel",
    )
    require_zip_members(
        builder_wheel,
        {
            "baldr_agent_builder/__init__.py",
            "baldr_agent_builder/backend.py",
            "baldr_agent_builder/build.py",
            "baldr_agent_builder/client.py",
            "baldr_agent_builder/cli.py",
            "baldr_agent_builder/conformance.py",
            "baldr_agent_builder/config.py",
            "baldr_agent_builder/diagnostics.py",
            "baldr_agent_builder/driver.py",
            "baldr_agent_builder/drivers.py",
            "baldr_agent_builder/execution.py",
            "baldr_agent_builder/inventory.py",
            "baldr_agent_builder/models.py",
            "baldr_agent_builder/protocol.py",
            "baldr_agent_builder/release.py",
            "baldr_agent_builder/scaffold.py",
            "baldr_agent_builder/templates/Makefile.tpl",
            "baldr_agent_builder/templates/README.md.tpl",
            "baldr_agent_builder/templates/README.typescript.md.tpl",
            "baldr_agent_builder/templates/agent.py.tpl",
            "baldr_agent_builder/templates/agent.ts.tpl",
            "baldr_agent_builder/templates/baldr-agent.toml.tpl",
            "baldr_agent_builder/templates/baldr-agent.typescript.toml.tpl",
            "baldr_agent_builder/templates/package.json.tpl",
            "baldr_agent_builder/templates/test_agent.py.tpl",
            "baldr_agent_builder/templates/test_agent.mjs.tpl",
            "baldr_agent_builder/templates/tsconfig.json.tpl",
        },
        label="Agent Builder wheel",
    )
    require_zip_members(
        runner_wheel,
        {
            "baldr_agent_runner/__init__.py",
            "baldr_agent_runner/cli.py",
            "baldr_agent_runner/runner.py",
            "baldr_agent_runner/store.py",
        },
        label="Agent runner wheel",
    )
    for hidden in PYTHON_DIST.glob(".*"):
        if hidden.is_file():
            hidden.unlink()
    shutil.copy2(core_wheel, EXTENSION_RUNTIME / core_wheel.name)

    bootstrap_runtime_validation(node)
    isolated_python_validation(
        uv,
        core_wheel,
        adapter_wheel,
        sdk_wheel,
        builder_wheel,
        runner_wheel,
    )

    npm_env = {
        **os.environ,
        "npm_config_cache": os.environ.get("npm_config_cache")
        or str(Path(tempfile.gettempdir()) / "baldr-router-npm-cache"),
    }
    run(npm, "run", "build:agents", env=npm_env, label="build TypeScript agent packages")
    run(
        npm,
        "pack",
        "--pack-destination",
        str(NODE_DIST),
        "--workspace",
        "@baldr/agent-sdk",
        env=npm_env,
        label="package TypeScript agent SDK",
    )
    run(
        npm,
        "pack",
        "--pack-destination",
        str(NODE_DIST),
        "--workspace",
        "@baldr/agent-builder-typescript",
        env=npm_env,
        label="package TypeScript Builder driver",
    )
    typescript_sdk_packages = sorted(
        NODE_DIST.glob(f"baldr-agent-sdk-{VERSION}.tgz")
    )
    typescript_driver_packages = sorted(
        NODE_DIST.glob(f"baldr-agent-builder-typescript-{VERSION}.tgz")
    )
    if len(typescript_sdk_packages) != 1 or len(typescript_driver_packages) != 1:
        raise SystemExit(
            "Expected one TypeScript SDK and Builder driver package: "
            f"{typescript_sdk_packages=} {typescript_driver_packages=}"
        )
    require_tar_members(
        typescript_sdk_packages[0],
        {
            "package/LICENSE",
            "package/package.json",
            "package/src/index.ts",
            "package/dist/index.js",
            "package/dist/index.d.ts",
        },
        label="TypeScript agent SDK package",
    )
    require_tar_members(
        typescript_driver_packages[0],
        {
            "package/LICENSE",
            "package/package.json",
            "package/baldr-builder-driver.json",
            "package/bin/baldr-builder-driver-typescript.mjs",
            "package/src/driver.ts",
            "package/dist/driver.js",
        },
        label="TypeScript Builder driver package",
    )
    isolated_typescript_distribution_validation(
        uv,
        npm,
        node,
        core_wheel,
        sdk_wheel,
        builder_wheel,
        runner_wheel,
        typescript_sdk_packages[0],
        typescript_driver_packages[0],
    )

    run(npm, "run", "compile", cwd=EXTENSION, label="compile VS Code extension")
    extension_manifest = json.loads((EXTENSION / "package.json").read_text(encoding="utf-8"))
    extension_name = str(extension_manifest.get("name") or "").strip()
    extension_version = str(extension_manifest.get("version") or "").strip()
    if not extension_name or not extension_version:
        raise SystemExit("VS Code extension package.json must declare name and version")
    vsix_target = ARTIFACTS / f"{extension_name}-{extension_version}.vsix"
    if vsix_target.exists():
        vsix_target.unlink()
    run(
        npm,
        "run",
        "package",
        "--",
        "--out",
        str(vsix_target),
        cwd=EXTENSION,
        label="package VSIX",
    )
    if not vsix_target.is_file():
        raise SystemExit(
            f"VSIX packaging did not create the expected artifact: {vsix_target}"
        )
    require_zip_members(
        vsix_target,
        {
            "extension/dist/workItemPresentation.js",
            "extension/resources/work-item-progress-v1.schema.json",
            "extension/resources/phase-deliverable-v1.schema.json",
            "extension/resources/phase-deliverable-page-v1.schema.json",
            "extension/resources/phase-deliverable-index-page-v1.schema.json",
            f"extension/resources/runtime/{core_wheel.name}",
        },
        label="VSIX",
    )
    run(
        sys.executable,
        "scripts/check_release_consistency.py",
        "--core-wheel",
        str(core_wheel),
        "--vsix",
        str(vsix_target),
        label="packaged release consistency",
    )

    zip_directory(
        ROOT / "facades" / "kiro" / "baldr-orchestrator",
        ARTIFACTS / f"baldr-orchestrator-kiro-{VERSION}.zip",
        root_name="baldr-orchestrator",
    )
    zip_directory(
        ROOT / "facades" / "vscode-agent-plugin",
        ARTIFACTS / f"baldr-router-agent-plugin-{VERSION}.zip",
        root_name="baldr-router-agent-plugin",
    )

    individual_artifacts = [
        path
        for path in sorted(ARTIFACTS.rglob("*"))
        if path.is_file() and not path.name.startswith(".")
    ]
    metadata_args = [
        sys.executable,
        "scripts/release_metadata.py",
        "--version", VERSION,
        "--output-dir", str(METADATA_DIR),
    ]
    for artifact in individual_artifacts:
        metadata_args.extend(["--artifact", str(artifact)])
    run(*metadata_args, label="SBOM, provenance, and secret scan")

    build_validation_report()

    source_bundle = DIST / f"baldr-router-{VERSION}-source.zip"
    artifacts_bundle = DIST / f"baldr-router-{VERSION}-artifacts.zip"
    validation_bundle = DIST / f"baldr-router-{VERSION}-validation-evidence.zip"
    zip_directory(
        ROOT,
        source_bundle,
        root_name="baldr-router",
        exclude_parts={"baldr_instrospeccion.md"},
    )
    zip_selected([(ARTIFACTS, "artifacts"), (METADATA_DIR, "metadata")], artifacts_bundle)
    zip_selected([(VALIDATION_DIR, "validation")], validation_bundle)
    bundles = {"source": source_bundle, "artifacts": artifacts_bundle, "validation_evidence": validation_bundle}
    write_release_manifest(bundles)
    write_checksums()

    run(sys.executable, "scripts/dev.py", "verify-release", label="release bundle hygiene")

    print("\nArtifacts:")
    for artifact in sorted(DIST.rglob("*")):
        if artifact.is_file():
            print(" -", artifact.relative_to(ROOT))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

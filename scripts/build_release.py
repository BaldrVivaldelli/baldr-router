from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
VERSION = "0.19.0"
DIST = ROOT / "dist"
ARTIFACTS = DIST / "artifacts"
PYTHON_DIST = ARTIFACTS / "python"
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


def isolated_python_validation(uv: str, core_wheel: Path, adapter_wheel: Path) -> None:
    with tempfile.TemporaryDirectory(prefix="baldr-isolated-") as temp:
        root = Path(temp)
        venv = root / "venv"
        run(uv, "venv", str(venv), label="isolated Python environment")
        python = venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        run(uv, "pip", "install", "--python", str(python), str(core_wheel), str(adapter_wheel), label="isolated wheel install")
        code = f"""
import asyncio, json
from baldr_router import __version__
from baldr_router.extensions import load_installed_extensions, extension_status
from baldr_router.server import mcp
load_installed_extensions(mcp)
tools = sorted(tool.name for tool in asyncio.run(mcp.list_tools()))
status = extension_status()
assert __version__ == {VERSION!r}
assert any(item.get('adapter') == 'kiro' for item in status.get('results', [])), status
assert 'kiro_install_workspace' in tools
print(json.dumps({{'version': __version__, 'kiro_tool_loaded': True}}))
"""
        run(str(python), "-c", code, label="isolated adapter discovery")


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
    parser = argparse.ArgumentParser(description="Build Baldr Router v0.19 narrative-progress artifacts")
    parser.add_argument("--skip-tests", action="store_true")
    parser.add_argument("--keep-dist", action="store_true")
    args = parser.parse_args()

    uv = executable("uv")
    npm = executable("npm")
    node = executable("node")

    if DIST.exists() and not args.keep_dist:
        shutil.rmtree(DIST)
    for directory in (PYTHON_DIST, VALIDATION_DIR, METADATA_DIR, EXTENSION_RUNTIME):
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
    core_wheels = sorted(PYTHON_DIST.glob(f"baldr_router-{VERSION}-*.whl"))
    adapter_wheels = sorted(PYTHON_DIST.glob(f"baldr_kiro_adapter-{VERSION}-*.whl"))
    if len(core_wheels) != 1 or len(adapter_wheels) != 1:
        raise SystemExit(f"Expected one core and adapter wheel: {core_wheels=} {adapter_wheels=}")
    core_wheel, adapter_wheel = core_wheels[0], adapter_wheels[0]
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
        },
        label="Core wheel",
    )
    for hidden in PYTHON_DIST.glob(".*"):
        if hidden.is_file():
            hidden.unlink()
    shutil.copy2(core_wheel, EXTENSION_RUNTIME / core_wheel.name)

    bootstrap_runtime_validation(node)
    isolated_python_validation(uv, core_wheel, adapter_wheel)

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
    zip_directory(ROOT, source_bundle, root_name="baldr-router")
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

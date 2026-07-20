from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import zipfile
from collections.abc import Mapping
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ROUTER = ROOT / "router"
ADAPTER = ROOT / "facades" / "kiro" / "adapter"
AGENT_SDK = ROOT / "sdks" / "python"
AGENT_BUILDER = ROOT / "tooling" / "agent-builder"
AGENT_RUNNER = ROOT / "runtimes" / "agent-runner"
LAUNCHER = ROOT / "launcher"
EXTENSION = ROOT / "facades" / "vscode-extension"
DIST = ROOT / "dist"


def _clean_test_environment(
    environ: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Keep host tooling available without inheriting an active Baldr run.

    Tests that exercise a BALDR_* switch set it explicitly in their own
    process.  Carrying orchestration identity, re-entry guards, agent bindings,
    or trusted roots from the parent makes the official runner depend on
    whether it was launched from a shell, Codex, Kiro, or Baldr itself.
    """

    source = os.environ if environ is None else environ
    return {
        key: value
        for key, value in source.items()
        if not key.upper().startswith("BALDR_")
    }


def _tool(name: str) -> str:
    value = shutil.which(name)
    if not value:
        raise SystemExit(f"Required executable not found: {name}")
    return value


def _run(*args: str, cwd: Path = ROOT, env: dict[str, str] | None = None) -> None:
    print("+", " ".join(args), f"[{cwd.relative_to(ROOT) if cwd != ROOT else '.'}]")
    subprocess.run(args, cwd=cwd, env=env, check=True)


def test_all() -> None:
    uv = _tool("uv")
    npm = _tool("npm")
    if not (ROOT / "node_modules").exists():
        _run(
            npm,
            "ci",
            "--ignore-scripts",
            "--no-audit",
            "--no-fund",
            cwd=ROOT,
        )
    _run(npm, "run", "test:agents", cwd=ROOT)
    # Durable tests must never reuse a developer's real Baldr state. Running
    # both Python suites under one disposable XDG root also makes local and CI
    # behavior reproducible.
    with tempfile.TemporaryDirectory(prefix="baldr-test-state-") as temp:
        root = Path(temp)
        git_config = root / "gitconfig"
        git_config.write_text(
            "[commit]\n\tgpgSign = false\n[tag]\n\tgpgSign = false\n",
            encoding="utf-8",
        )
        test_env = {
            **_clean_test_environment(),
            "XDG_CONFIG_HOME": str(root / "config"),
            "XDG_CACHE_HOME": str(root / "cache"),
            "XDG_STATE_HOME": str(root / "state"),
            # Tests create disposable repositories and must not inherit a
            # developer's signing keys, hooks, or global Git policy.
            "GIT_CONFIG_GLOBAL": str(git_config),
            "GIT_CONFIG_NOSYSTEM": "1",
        }
        _run(uv, "run", "--extra", "dev", "pytest", "-q", cwd=ROUTER, env=test_env)
        _run(uv, "run", "--extra", "dev", "pytest", "-q", cwd=ADAPTER, env=test_env)
        _run(uv, "run", "--extra", "dev", "pytest", "-q", cwd=AGENT_SDK, env=test_env)
        _run(
            uv,
            "run",
            "--extra",
            "dev",
            "pytest",
            "-q",
            cwd=AGENT_BUILDER,
            env=test_env,
        )
        _run(uv, "run", "--extra", "dev", "pytest", "-q", cwd=AGENT_RUNNER, env=test_env)
        _run(
            uv,
            "run",
            "python",
            "scripts/test_typescript_agent_vertical.py",
            cwd=ROOT,
            env=test_env,
        )
    _run(npm, "test", cwd=LAUNCHER)
    if not (EXTENSION / "node_modules").exists():
        _run(npm, "ci", "--ignore-scripts", "--no-audit", "--no-fund", cwd=EXTENSION)
    _run(npm, "test", cwd=EXTENSION)


def lint_all() -> None:
    uv = _tool("uv")
    npm = _tool("npm")
    lint_env = {
        **_clean_test_environment(),
        "UV_CACHE_DIR": os.environ.get("UV_CACHE_DIR")
        or str(Path(tempfile.gettempdir()) / "baldr-router-uv-cache"),
    }
    _run(npm, "run", "check:agents", cwd=ROOT)
    _run(sys.executable, "scripts/check_release_consistency.py")
    _run(
        uv,
        "run",
        "--extra",
        "dev",
        "ruff",
        "check",
        "src",
        "tests",
        cwd=ROUTER,
        env=lint_env,
    )
    _run(
        uv,
        "run",
        "--extra",
        "dev",
        "ruff",
        "check",
        "src",
        "tests",
        cwd=ADAPTER,
        env=lint_env,
    )
    _run(
        uv,
        "run",
        "--extra",
        "dev",
        "ruff",
        "check",
        "src",
        "tests",
        cwd=AGENT_SDK,
        env=lint_env,
    )
    _run(
        uv,
        "run",
        "--extra",
        "dev",
        "ruff",
        "check",
        "src",
        "tests",
        cwd=AGENT_BUILDER,
        env=lint_env,
    )
    _run(
        uv,
        "run",
        "--extra",
        "dev",
        "ruff",
        "check",
        "src",
        "tests",
        cwd=AGENT_RUNNER,
        env=lint_env,
    )
    _run(
        sys.executable,
        "-m",
        "compileall",
        "-q",
        "router/src",
        "facades/kiro/adapter/src",
        "sdks/python/src",
        "tooling/agent-builder/src",
        "runtimes/agent-runner/src",
    )
    _run(sys.executable, "scripts/generate_facades.py", "--check")
    if not (EXTENSION / "node_modules").exists():
        _run(npm, "ci", "--ignore-scripts", "--no-audit", "--no-fund", cwd=EXTENSION)
    _run(npm, "run", "check", cwd=EXTENSION)


def build_release(skip_tests: bool = False) -> None:
    args = [sys.executable, "scripts/build_release.py"]
    if skip_tests:
        args.append("--skip-tests")
    _run(*args)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify_release() -> None:
    manifest_path = DIST / "release-manifest.json"
    if not manifest_path.exists():
        raise SystemExit("dist/release-manifest.json is missing. Run `python scripts/dev.py build` first.")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    failures: list[str] = []
    promotion = manifest.get("promotion") or {}
    if manifest.get("qualification_required_for_promotion") is not True:
        failures.append("release manifest does not require real qualification")
    if promotion.get("provider") != "codex":
        failures.append("release promotion provider must be codex")
    if promotion.get("required_profiles") != ["vscode-remote-wsl"]:
        failures.append(
            "release promotion must require only the vscode-remote-wsl profile"
        )
    if "kiro-windows-wsl" not in (promotion.get("deferred_profiles") or []):
        failures.append("Kiro must remain explicitly deferred from the v0.20 gate")
    for item in manifest.get("artifacts", []):
        path = DIST / str(item["path"])
        if not path.exists():
            failures.append(f"missing artifact: {path}")
            continue
        if _sha256(path) != item.get("sha256"):
            failures.append(f"checksum mismatch: {path}")
    source_zip = DIST / str(manifest.get("bundles", {}).get("source", ""))
    if source_zip.exists():
        with zipfile.ZipFile(source_zip) as archive:
            names = archive.namelist()
        forbidden = [
            name
            for name in names
            if any(part in name for part in ("validation-state/", "node_modules/", "__pycache__/", ".pytest_cache/", ".ruff_cache/"))
            or name.endswith("baldr.sqlite3")
            or name.lower().endswith(".vsix")
        ]
        if forbidden:
            failures.append(f"source bundle contains forbidden generated/runtime content: {forbidden[:10]}")
    reports = [DIST / "validation" / "build-validation.json", DIST / "validation" / "synthetic-qualification.json"]
    raw_fragments = [
        str(ROOT),
        "/mnt/data/",
        str(Path(tempfile.gettempdir()).resolve()),
        "\\\\wsl.localhost\\",
    ]
    for report in reports:
        if not report.exists():
            continue
        text = report.read_text(encoding="utf-8", errors="replace")
        for fragment in raw_fragments:
            if fragment and fragment in text:
                failures.append(f"non-portable path leaked into {report.name}: {fragment}")
    if failures:
        raise SystemExit("Release verification failed:\n- " + "\n- ".join(failures))
    print(json.dumps({"ok": True, "release": manifest.get("version"), "artifacts": len(manifest.get("artifacts", []))}, indent=2))


def qualification_template(profile: str, output_dir: str) -> None:
    uv = _tool("uv")
    _run(
        uv,
        "run",
        "baldr-router",
        "qualification",
        "template",
        "--profile",
        profile,
        "--output-dir",
        output_dir,
        cwd=ROUTER,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Cross-platform Baldr developer and release entrypoint")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("test", help="Run all Python and Node suites")
    sub.add_parser("lint", help="Run Ruff, compileall, facade conformance, and TypeScript checks")
    build = sub.add_parser("build", help="Build the split source/artifact/evidence release")
    build.add_argument("--skip-tests", action="store_true")
    sub.add_parser("verify-release", help="Verify checksums, bundle hygiene, and path redaction")
    qualify = sub.add_parser("qualification-template", help="Create real-environment assertion and canary templates")
    qualify.add_argument("--profile", required=True)
    qualify.add_argument("--output-dir", required=True)
    args = parser.parse_args(argv)
    if args.command == "test":
        test_all()
    elif args.command == "lint":
        lint_all()
    elif args.command == "build":
        build_release(skip_tests=args.skip_tests)
    elif args.command == "verify-release":
        verify_release()
    elif args.command == "qualification-template":
        qualification_template(args.profile, args.output_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

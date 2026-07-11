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
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ROUTER = ROOT / "router"
ADAPTER = ROOT / "facades" / "kiro" / "adapter"
LAUNCHER = ROOT / "launcher"
EXTENSION = ROOT / "facades" / "vscode-extension"
DIST = ROOT / "dist"


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
    _run(uv, "run", "--extra", "dev", "pytest", "-q", cwd=ROUTER)
    _run(uv, "run", "--extra", "dev", "pytest", "-q", cwd=ADAPTER)
    _run(npm, "test", cwd=LAUNCHER)
    if not (EXTENSION / "node_modules").exists():
        _run(npm, "ci", "--ignore-scripts", "--no-audit", "--no-fund", cwd=EXTENSION)
    _run(npm, "test", cwd=EXTENSION)


def lint_all() -> None:
    uv = _tool("uv")
    npm = _tool("npm")
    _run(uv, "run", "--extra", "dev", "ruff", "check", "src", "tests", cwd=ROUTER)
    _run(uv, "run", "--extra", "dev", "ruff", "check", "src", "tests", cwd=ADAPTER)
    _run(sys.executable, "-m", "compileall", "-q", "router/src", "facades/kiro/adapter/src")
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
        ]
        if forbidden:
            failures.append(f"source bundle contains forbidden runtime state: {forbidden[:10]}")
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

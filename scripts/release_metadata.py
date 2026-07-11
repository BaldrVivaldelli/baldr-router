from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import sys
import tomllib
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]

SECRET_PATTERNS = {
    "context7": re.compile(r"ctx7sk-[A-Za-z0-9_-]{12,}"),
    "generic_api_key": re.compile(
        r"(?i)(?:(?:api[_-]?key|token|secret)\s*=\s*['\"]?[A-Za-z0-9_./+-]{24,}"
        r"|['\"]?(?:api[_-]?key|token|secret)['\"]?\s*:\s*['\"][A-Za-z0-9_./+-]{24,}['\"])"
    ),
    "private_key": re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
}
SKIP_PARTS = {".git", "node_modules", ".venv", "dist", "__pycache__", ".pytest_cache"}


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _load_toml(path: Path) -> dict[str, Any]:
    return tomllib.loads(path.read_text(encoding="utf-8"))


def _spdx_id(name: str) -> str:
    return "SPDXRef-" + re.sub(r"[^A-Za-z0-9.-]+", "-", name).strip("-")


def generate_spdx(version: str, output: Path) -> dict[str, Any]:
    packages: list[dict[str, Any]] = []
    relationships: list[dict[str, str]] = []
    root_id = _spdx_id("baldr-router-release")
    components = [
        ("baldr-router", ROOT / "router" / "pyproject.toml", "Python"),
        ("baldr-kiro-adapter", ROOT / "facades" / "kiro" / "adapter" / "pyproject.toml", "Python"),
        ("baldr-router-vscode", ROOT / "facades" / "vscode-extension" / "package.json", "JavaScript"),
        ("baldr-router-launcher", ROOT / "launcher" / "package.json", "JavaScript"),
    ]
    packages.append(
        {
            "SPDXID": root_id,
            "name": "baldr-router-release",
            "versionInfo": version,
            "downloadLocation": "NOASSERTION",
            "filesAnalyzed": False,
            "licenseConcluded": "MIT",
            "licenseDeclared": "MIT",
            "supplier": "Organization: Baldr",
        }
    )
    for name, path, language in components:
        if path.suffix == ".toml":
            data = _load_toml(path)
            project = data.get("project") or {}
            component_version = str(project.get("version") or version)
            dependencies = [str(item) for item in project.get("dependencies") or []]
        else:
            data = json.loads(path.read_text(encoding="utf-8"))
            component_version = str(data.get("version") or version)
            dependencies = [
                f"{dep}@{constraint}"
                for field in ("dependencies", "devDependencies")
                for dep, constraint in (data.get(field) or {}).items()
            ]
        component_id = _spdx_id(name)
        packages.append(
            {
                "SPDXID": component_id,
                "name": name,
                "versionInfo": component_version,
                "downloadLocation": "NOASSERTION",
                "filesAnalyzed": False,
                "licenseConcluded": "MIT",
                "licenseDeclared": "MIT",
                "primaryPackagePurpose": "APPLICATION" if name == "baldr-router" else "LIBRARY",
                "comment": f"Language: {language}. Declared dependencies: {', '.join(dependencies) or 'none'}",
            }
        )
        relationships.append(
            {"spdxElementId": root_id, "relationshipType": "CONTAINS", "relatedSpdxElement": component_id}
        )
    document = {
        "spdxVersion": "SPDX-2.3",
        "dataLicense": "CC0-1.0",
        "SPDXID": "SPDXRef-DOCUMENT",
        "name": f"baldr-router-{version}",
        "documentNamespace": f"https://baldr.invalid/spdx/baldr-router/{version}/{hashlib.sha256(version.encode()).hexdigest()[:16]}",
        "creationInfo": {
            "created": datetime.now(timezone.utc).isoformat(),
            "creators": ["Tool: baldr-router-release-metadata"],
        },
        "packages": packages,
        "relationships": relationships,
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return document


def generate_provenance(version: str, artifacts: list[Path], output: Path) -> dict[str, Any]:
    subjects = [
        {"name": path.name, "digest": {"sha256": sha256(path)}}
        for path in sorted(artifacts)
        if path.is_file()
    ]
    statement = {
        "_type": "https://in-toto.io/Statement/v1",
        "subject": subjects,
        "predicateType": "https://slsa.dev/provenance/v1",
        "predicate": {
            "buildDefinition": {
                "buildType": "https://baldr.invalid/build/release-v1",
                "externalParameters": {"version": version, "featureFreeze": True},
                "internalParameters": {},
                "resolvedDependencies": [],
            },
            "runDetails": {
                "builder": {"id": "baldr-router/scripts/build_release.py"},
                "metadata": {
                    "invocationId": os.environ.get("GITHUB_RUN_ID") or "local-build",
                    "startedOn": datetime.now(timezone.utc).isoformat(),
                    "finishedOn": datetime.now(timezone.utc).isoformat(),
                },
                "byproducts": [
                    {"name": "python", "content": platform.python_version()},
                    {"name": "platform", "content": platform.platform()},
                ],
            },
        },
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(statement, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return statement


def secret_scan(output: Path) -> dict[str, Any]:
    findings: list[dict[str, Any]] = []
    scanned = 0
    for path in ROOT.rglob("*"):
        if not path.is_file() or any(part in SKIP_PARTS for part in path.parts):
            continue
        if path.suffix.lower() in {".png", ".jpg", ".jpeg", ".gif", ".zip", ".whl", ".vsix", ".sqlite3", ".pyc"}:
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        scanned += 1
        for name, pattern in SECRET_PATTERNS.items():
            for match in pattern.finditer(text):
                snippet = match.group(0)
                if "synthetic" in snippet.lower() or "your_api_key" in snippet.lower() or "<redacted>" in snippet.lower():
                    continue
                findings.append(
                    {
                        "path": path.relative_to(ROOT).as_posix(),
                        "pattern": name,
                        "line": text.count("\n", 0, match.start()) + 1,
                    }
                )
    report = {
        "ok": not findings,
        "scanner": "baldr-static-secret-scan-v1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "files_scanned": scanned,
        "findings": findings,
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return report


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--version", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--artifact", action="append", default=[])
    args = parser.parse_args(argv)
    output = Path(args.output_dir)
    artifacts = [Path(item) for item in args.artifact]
    generate_spdx(args.version, output / "SBOM.spdx.json")
    generate_provenance(args.version, artifacts, output / "provenance.intoto.json")
    scan = secret_scan(output / "secret-scan.json")
    print(json.dumps({"ok": scan["ok"], "output_dir": str(output)}, indent=2))
    return 0 if scan["ok"] else 2


if __name__ == "__main__":
    raise SystemExit(main())

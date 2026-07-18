from __future__ import annotations

import argparse
import json
import re
import tomllib
import zipfile
from pathlib import Path
from typing import Any, Mapping


ROOT = Path(__file__).resolve().parents[1]


class ReleaseConsistencyError(ValueError):
    """Raised when release surfaces disagree about the current release."""


def _json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ReleaseConsistencyError(f"{path.relative_to(ROOT)} must contain a JSON object")
    return value


def _toml(path: Path) -> dict[str, Any]:
    return tomllib.loads(path.read_text(encoding="utf-8"))


def _extract(path: Path, pattern: str, *, label: str) -> str:
    match = re.search(pattern, path.read_text(encoding="utf-8"), flags=re.MULTILINE)
    if not match:
        raise ReleaseConsistencyError(
            f"Could not read {label} from {path.relative_to(ROOT)}"
        )
    return match.group(1)


def assert_uniform_versions(values: Mapping[str, str]) -> str:
    if not values:
        raise ReleaseConsistencyError("No release version surfaces were provided")
    normalized = {name: str(value).strip() for name, value in values.items()}
    versions = set(normalized.values())
    if "" in versions or len(versions) != 1:
        detail = ", ".join(f"{name}={value!r}" for name, value in sorted(values.items()))
        raise ReleaseConsistencyError(f"Release versions are inconsistent: {detail}")
    return versions.pop()


def _workspace_package_version(lock: dict[str, Any], package_name: str) -> str:
    for package in lock.get("package", []):
        if isinstance(package, dict) and package.get("name") == package_name:
            return str(package.get("version") or "")
    raise ReleaseConsistencyError(f"uv.lock has no {package_name!r} workspace package")


def source_version_values(root: Path = ROOT) -> dict[str, str]:
    core_project = _toml(root / "router" / "pyproject.toml")
    adapter_project = _toml(root / "facades" / "kiro" / "adapter" / "pyproject.toml")
    sdk_project = _toml(root / "sdks" / "python" / "pyproject.toml")
    typescript_sdk = _json(root / "sdks" / "typescript" / "package.json")
    builder_project = _toml(root / "tooling" / "agent-builder" / "pyproject.toml")
    typescript_builder = _json(
        root / "tooling" / "agent-builder-typescript" / "package.json"
    )
    runner_project = _toml(root / "runtimes" / "agent-runner" / "pyproject.toml")
    launcher = _json(root / "launcher" / "package.json")
    extension = _json(root / "facades" / "vscode-extension" / "package.json")
    extension_lock = _json(root / "facades" / "vscode-extension" / "package-lock.json")
    plugin = _json(root / "facades" / "vscode-agent-plugin" / "plugin.json")
    plugin_mcp = _json(root / "facades" / "vscode-agent-plugin" / ".mcp.json")
    uv_lock = _toml(root / "uv.lock")
    npm_lock = _json(root / "package-lock.json")

    lock_root = extension_lock.get("packages", {}).get("", {})
    plugin_server = plugin_mcp.get("mcpServers", {}).get("baldr-router", {})
    plugin_env = plugin_server.get("env", {}) if isinstance(plugin_server, dict) else {}

    return {
        "core project": str(core_project.get("project", {}).get("version") or ""),
        "core module": _extract(
            root / "router" / "src" / "baldr_router" / "__init__.py",
            r'^__version__\s*=\s*["\']([^"\']+)["\']',
            label="core module version",
        ),
        "app server": _extract(
            root / "router" / "src" / "baldr_router" / "codex_app_server.py",
            r'^[ \t]*["\']version["\']\s*:\s*["\']([^"\']+)["\']',
            label="app-server protocol version",
        ),
        "adapter project": str(adapter_project.get("project", {}).get("version") or ""),
        "adapter module": _extract(
            root / "facades" / "kiro" / "adapter" / "src" / "baldr_kiro_adapter" / "__init__.py",
            r'^__version__\s*=\s*["\']([^"\']+)["\']',
            label="adapter module version",
        ),
        "agent SDK project": str(sdk_project.get("project", {}).get("version") or ""),
        "agent SDK module": _extract(
            root / "sdks" / "python" / "src" / "baldr_agent_sdk" / "__init__.py",
            r'^__version__\s*=\s*["\']([^"\']+)["\']',
            label="agent SDK module version",
        ),
        "TypeScript agent SDK package": str(typescript_sdk.get("version") or ""),
        "TypeScript agent SDK lock": str(
            npm_lock.get("packages", {})
            .get("sdks/typescript", {})
            .get("version", "")
        ),
        "agent builder project": str(
            builder_project.get("project", {}).get("version") or ""
        ),
        "agent builder module": _extract(
            root
            / "tooling"
            / "agent-builder"
            / "src"
            / "baldr_agent_builder"
            / "__init__.py",
            r'^__version__\s*=\s*["\']([^"\']+)["\']',
            label="agent builder module version",
        ),
        "TypeScript Builder driver package": str(
            typescript_builder.get("version") or ""
        ),
        "TypeScript Builder driver lock": str(
            npm_lock.get("packages", {})
            .get("tooling/agent-builder-typescript", {})
            .get("version", "")
        ),
        "agent runner project": str(
            runner_project.get("project", {}).get("version") or ""
        ),
        "agent runner module": _extract(
            root
            / "runtimes"
            / "agent-runner"
            / "src"
            / "baldr_agent_runner"
            / "__init__.py",
            r'^__version__\s*=\s*["\']([^"\']+)["\']',
            label="agent runner module version",
        ),
        "launcher package": str(launcher.get("version") or ""),
        "launcher bootstrap": _extract(
            root / "launcher" / "lib" / "runtime-bootstrap.mjs",
            r"^export const VERSION\s*=\s*['\"]([^'\"]+)['\"]",
            label="launcher bootstrap version",
        ),
        "extension package": str(extension.get("version") or ""),
        "extension lock": str(extension_lock.get("version") or ""),
        "extension lock root": str(lock_root.get("version") or ""),
        "extension runtime": _extract(
            root / "facades" / "vscode-extension" / "src" / "runtime.ts",
            r"^export const EXTENSION_VERSION\s*=\s*['\"]([^'\"]+)['\"]",
            label="extension runtime version",
        ),
        "extension fallback wheel": _extract(
            root / "facades" / "vscode-extension" / "src" / "runtime.ts",
            r"baldr_router-([0-9]+\.[0-9]+\.[0-9]+)-py3-none-any\.whl",
            label="extension fallback wheel version",
        ),
        "extension bootstrap": _extract(
            root / "facades" / "vscode-extension" / "runtime" / "runtime-bootstrap.mjs",
            r"^export const VERSION\s*=\s*['\"]([^'\"]+)['\"]",
            label="extension bootstrap version",
        ),
        "agent plugin": str(plugin.get("version") or ""),
        "agent plugin MCP client": str(plugin_env.get("BALDR_CLIENT_VERSION") or ""),
        "release builder": _extract(
            root / "scripts" / "build_release.py",
            r'^VERSION\s*=\s*["\']([^"\']+)["\']',
            label="release builder version",
        ),
        "uv core workspace": _workspace_package_version(uv_lock, "baldr-router"),
        "uv adapter workspace": _workspace_package_version(uv_lock, "baldr-kiro-adapter"),
        "uv agent SDK workspace": _workspace_package_version(uv_lock, "baldr-agent-sdk"),
        "uv agent builder workspace": _workspace_package_version(
            uv_lock, "baldr-agent-builder"
        ),
        "uv agent runner workspace": _workspace_package_version(
            uv_lock, "baldr-agent-runner"
        ),
    }


def check_source_consistency(root: Path = ROOT) -> str:
    values = source_version_values(root)
    version = assert_uniform_versions(values)
    parts = version.split(".")
    if len(parts) != 3 or not all(part.isdigit() for part in parts):
        raise ReleaseConsistencyError(f"Release version must be numeric semver, got {version!r}")
    major, minor, _patch = parts

    adapter = _toml(root / "facades" / "kiro" / "adapter" / "pyproject.toml")
    dependencies = adapter.get("project", {}).get("dependencies", [])
    expected_dependency = f"baldr-router>={version},<{major}.{int(minor) + 1}.0"
    if expected_dependency not in dependencies:
        raise ReleaseConsistencyError(
            f"Kiro adapter must depend on {expected_dependency!r}; got {dependencies!r}"
        )

    runner = _toml(root / "runtimes" / "agent-runner" / "pyproject.toml")
    runner_dependencies = runner.get("project", {}).get("dependencies", [])
    expected_sdk_dependency = (
        f"baldr-agent-sdk>={version},<{major}.{int(minor) + 1}.0"
    )
    if expected_sdk_dependency not in runner_dependencies:
        raise ReleaseConsistencyError(
            "Agent runner must depend on "
            f"{expected_sdk_dependency!r}; got {runner_dependencies!r}"
        )

    builder = _toml(root / "tooling" / "agent-builder" / "pyproject.toml")
    builder_dependencies = builder.get("project", {}).get("dependencies", [])
    if expected_sdk_dependency not in builder_dependencies:
        raise ReleaseConsistencyError(
            "Agent Builder must depend on "
            f"{expected_sdk_dependency!r}; got {builder_dependencies!r}"
        )

    typescript_builder = _json(
        root / "tooling" / "agent-builder-typescript" / "package.json"
    )
    typescript_dependencies = typescript_builder.get("dependencies", {})
    if typescript_dependencies.get("@baldr/agent-sdk") != version:
        raise ReleaseConsistencyError(
            "TypeScript Builder driver must depend on the exact release of "
            f"@baldr/agent-sdk ({version}); got {typescript_dependencies!r}"
        )
    typescript_sdk = _json(root / "sdks" / "typescript" / "package.json")
    for label, package in (
        ("TypeScript SDK", typescript_sdk),
        ("TypeScript Builder driver", typescript_builder),
    ):
        if package.get("publishConfig") != {"access": "public"}:
            raise ReleaseConsistencyError(
                f"{label} must declare public npm publication metadata"
            )
    expected_bin = {
        "baldr-builder-driver-typescript": (
            "./bin/baldr-builder-driver-typescript.mjs"
        )
    }
    if typescript_builder.get("bin") != expected_bin:
        raise ReleaseConsistencyError(
            "TypeScript Builder driver must expose the PATH discovery executable"
        )
    if typescript_builder.get("scripts", {}).get("build") != "tsc -p .":
        raise ReleaseConsistencyError(
            "TypeScript Builder driver build must not depend on monorepo paths"
        )
    for license_path in (
        root / "sdks" / "typescript" / "LICENSE",
        root / "tooling" / "agent-builder-typescript" / "LICENSE",
    ):
        if license_path.read_bytes() != (root / "LICENSE").read_bytes():
            raise ReleaseConsistencyError(
                f"Packaged license differs from root LICENSE: {license_path}"
            )

    freeze_line = _extract(
        root / "router" / "src" / "baldr_router" / "release_policy.py",
        r'^FEATURE_FREEZE_LINE\s*=\s*["\']([^"\']+)["\']',
        label="feature-freeze line",
    )
    if freeze_line != f"{major}.{minor}":
        raise ReleaseConsistencyError(
            f"Feature-freeze line {freeze_line!r} does not match release {version!r}"
        )

    release_workflow = (root / ".github" / "workflows" / "release.yml").read_text(
        encoding="utf-8"
    )
    tag_pattern = f"v{major}.{minor}.*"
    if tag_pattern not in release_workflow:
        raise ReleaseConsistencyError(
            f"Release workflow must select tags matching {tag_pattern!r}"
        )

    launcher_bootstrap = root / "launcher" / "lib" / "runtime-bootstrap.mjs"
    extension_bootstrap = (
        root / "facades" / "vscode-extension" / "runtime" / "runtime-bootstrap.mjs"
    )
    if launcher_bootstrap.read_bytes() != extension_bootstrap.read_bytes():
        raise ReleaseConsistencyError(
            "VS Code must bundle the exact launcher runtime-bootstrap.mjs"
        )

    for schema_name in (
        "work-item-progress-v1.schema.json",
        "phase-deliverable-v1.schema.json",
        "phase-deliverable-page-v1.schema.json",
        "phase-deliverable-index-page-v1.schema.json",
    ):
        canonical_schema = root / "contracts" / schema_name
        schema_copies = [
            root / "router" / "src" / "baldr_router" / "contracts" / schema_name,
            root / "facades" / "vscode-extension" / "resources" / schema_name,
        ]
        canonical = json.loads(canonical_schema.read_text(encoding="utf-8"))
        for schema in schema_copies:
            if json.loads(schema.read_text(encoding="utf-8")) != canonical:
                raise ReleaseConsistencyError(
                    f"Generated contract schema is stale: {schema.relative_to(root)}"
                )

    for schema_name in (
        "agent-registry-v1.schema.json",
        "agent-transport-http-v1.schema.json",
        "agent-execution-v1.schema.json",
        "agent-manager-v1.schema.json",
        "agent-source-v1.schema.json",
        "agent-catalog-sync-v1.schema.json",
        "agent-team-resolution-v1.schema.json",
        "orchestration-policy-v1.schema.json",
    ):
        canonical = json.loads(
            (root / "contracts" / schema_name).read_text(encoding="utf-8")
        )
        packaged_path = (
            root / "router" / "src" / "baldr_router" / "contracts" / schema_name
        )
        if json.loads(packaged_path.read_text(encoding="utf-8")) != canonical:
            raise ReleaseConsistencyError(
                f"Generated contract schema is stale: {packaged_path.relative_to(root)}"
            )

    return version


def _archive_members(path: Path) -> set[str]:
    with zipfile.ZipFile(path) as archive:
        return set(archive.namelist())


def _require_members(path: Path, expected: set[str], *, label: str) -> None:
    missing = sorted(expected - _archive_members(path))
    if missing:
        raise ReleaseConsistencyError(
            f"{label} {path.name} is missing required files: {', '.join(missing)}"
        )


def check_artifact_consistency(
    core_wheel: Path,
    vsix: Path,
    *,
    root: Path = ROOT,
    version: str | None = None,
) -> str:
    release = version or check_source_consistency(root)
    wheel_name = f"baldr_router-{release}-py3-none-any.whl"
    if core_wheel.name != wheel_name:
        raise ReleaseConsistencyError(
            f"Core wheel must be named {wheel_name!r}, got {core_wheel.name!r}"
        )

    wheel_members = {
        "baldr_router/phase_deliverables.py",
        "baldr_router/provider_activity.py",
        "baldr_router/work_item_progress.py",
        "baldr_router/contracts/phase-deliverable-v1.schema.json",
        "baldr_router/contracts/phase-deliverable-page-v1.schema.json",
        "baldr_router/contracts/phase-deliverable-index-page-v1.schema.json",
        "baldr_router/contracts/work-item-progress-v1.schema.json",
        "baldr_router/contracts/agent-registry-v1.schema.json",
        "baldr_router/contracts/agent-transport-http-v1.schema.json",
        "baldr_router/contracts/agent-execution-v1.schema.json",
        "baldr_router/contracts/agent-manager-v1.schema.json",
        "baldr_router/contracts/agent-source-v1.schema.json",
        "baldr_router/contracts/agent-catalog-sync-v1.schema.json",
        "baldr_router/contracts/agent-team-resolution-v1.schema.json",
        "baldr_router/contracts/orchestration-policy-v1.schema.json",
        f"baldr_router-{release}.dist-info/METADATA",
    }
    _require_members(core_wheel, wheel_members, label="Core wheel")

    vsix_members = {
        "extension/dist/workItemPresentation.js",
        "extension/resources/phase-deliverable-v1.schema.json",
        "extension/resources/phase-deliverable-page-v1.schema.json",
        "extension/resources/phase-deliverable-index-page-v1.schema.json",
        "extension/resources/work-item-progress-v1.schema.json",
        f"extension/resources/runtime/{wheel_name}",
        "extension/package.json",
    }
    _require_members(vsix, vsix_members, label="VSIX")

    canonical_schema = json.loads(
        (root / "contracts" / "work-item-progress-v1.schema.json").read_text(encoding="utf-8")
    )
    canonical_deliverable_schema = json.loads(
        (root / "contracts" / "phase-deliverable-v1.schema.json").read_text(encoding="utf-8")
    )
    canonical_deliverable_page_schema = json.loads(
        (root / "contracts" / "phase-deliverable-page-v1.schema.json").read_text(encoding="utf-8")
    )
    canonical_deliverable_index_page_schema = json.loads(
        (root / "contracts" / "phase-deliverable-index-page-v1.schema.json").read_text(encoding="utf-8")
    )
    canonical_agent_registry_schema = json.loads(
        (root / "contracts" / "agent-registry-v1.schema.json").read_text(encoding="utf-8")
    )
    canonical_agent_http_schema = json.loads(
        (root / "contracts" / "agent-transport-http-v1.schema.json").read_text(encoding="utf-8")
    )
    canonical_agent_execution_schema = json.loads(
        (root / "contracts" / "agent-execution-v1.schema.json").read_text(
            encoding="utf-8"
        )
    )
    canonical_agent_manager_schema = json.loads(
        (root / "contracts" / "agent-manager-v1.schema.json").read_text(encoding="utf-8")
    )
    canonical_agent_source_schema = json.loads(
        (root / "contracts" / "agent-source-v1.schema.json").read_text(encoding="utf-8")
    )
    canonical_agent_sync_schema = json.loads(
        (root / "contracts" / "agent-catalog-sync-v1.schema.json").read_text(encoding="utf-8")
    )
    canonical_agent_team_schema = json.loads(
        (root / "contracts" / "agent-team-resolution-v1.schema.json").read_text(encoding="utf-8")
    )
    canonical_orchestration_schema = json.loads(
        (root / "contracts" / "orchestration-policy-v1.schema.json").read_text(encoding="utf-8")
    )
    with zipfile.ZipFile(core_wheel) as wheel_archive:
        metadata = wheel_archive.read(f"baldr_router-{release}.dist-info/METADATA").decode()
        wheel_schema = json.loads(
            wheel_archive.read("baldr_router/contracts/work-item-progress-v1.schema.json")
        )
        wheel_deliverable_schema = json.loads(
            wheel_archive.read("baldr_router/contracts/phase-deliverable-v1.schema.json")
        )
        wheel_deliverable_page_schema = json.loads(
            wheel_archive.read("baldr_router/contracts/phase-deliverable-page-v1.schema.json")
        )
        wheel_deliverable_index_page_schema = json.loads(
            wheel_archive.read("baldr_router/contracts/phase-deliverable-index-page-v1.schema.json")
        )
        wheel_agent_registry_schema = json.loads(
            wheel_archive.read("baldr_router/contracts/agent-registry-v1.schema.json")
        )
        wheel_agent_http_schema = json.loads(
            wheel_archive.read("baldr_router/contracts/agent-transport-http-v1.schema.json")
        )
        wheel_agent_execution_schema = json.loads(
            wheel_archive.read("baldr_router/contracts/agent-execution-v1.schema.json")
        )
        wheel_agent_manager_schema = json.loads(
            wheel_archive.read("baldr_router/contracts/agent-manager-v1.schema.json")
        )
        wheel_agent_source_schema = json.loads(
            wheel_archive.read("baldr_router/contracts/agent-source-v1.schema.json")
        )
        wheel_agent_sync_schema = json.loads(
            wheel_archive.read("baldr_router/contracts/agent-catalog-sync-v1.schema.json")
        )
        wheel_agent_team_schema = json.loads(
            wheel_archive.read("baldr_router/contracts/agent-team-resolution-v1.schema.json")
        )
        wheel_orchestration_schema = json.loads(
            wheel_archive.read("baldr_router/contracts/orchestration-policy-v1.schema.json")
        )
    if f"Version: {release}\n" not in metadata.replace("\r\n", "\n"):
        raise ReleaseConsistencyError("Core wheel metadata does not match the release version")
    if wheel_schema != canonical_schema:
        raise ReleaseConsistencyError("Core wheel contains a stale progress schema")
    if wheel_deliverable_schema != canonical_deliverable_schema:
        raise ReleaseConsistencyError("Core wheel contains a stale deliverable schema")
    if wheel_deliverable_page_schema != canonical_deliverable_page_schema:
        raise ReleaseConsistencyError("Core wheel contains a stale deliverable page schema")
    if wheel_deliverable_index_page_schema != canonical_deliverable_index_page_schema:
        raise ReleaseConsistencyError("Core wheel contains a stale deliverable index schema")
    if wheel_agent_registry_schema != canonical_agent_registry_schema:
        raise ReleaseConsistencyError("Core wheel contains a stale agent registry schema")
    if wheel_agent_http_schema != canonical_agent_http_schema:
        raise ReleaseConsistencyError("Core wheel contains a stale agent HTTP schema")
    if wheel_agent_execution_schema != canonical_agent_execution_schema:
        raise ReleaseConsistencyError("Core wheel contains a stale agent execution schema")
    if wheel_agent_manager_schema != canonical_agent_manager_schema:
        raise ReleaseConsistencyError("Core wheel contains a stale Agent Manager schema")
    if wheel_agent_source_schema != canonical_agent_source_schema:
        raise ReleaseConsistencyError("Core wheel contains a stale agent source schema")
    if wheel_agent_sync_schema != canonical_agent_sync_schema:
        raise ReleaseConsistencyError("Core wheel contains a stale agent catalog sync schema")
    if wheel_agent_team_schema != canonical_agent_team_schema:
        raise ReleaseConsistencyError("Core wheel contains a stale team resolution schema")
    if wheel_orchestration_schema != canonical_orchestration_schema:
        raise ReleaseConsistencyError("Core wheel contains a stale orchestration policy schema")

    with zipfile.ZipFile(vsix) as vsix_archive:
        runtime_wheels = sorted(
            name
            for name in vsix_archive.namelist()
            if name.startswith("extension/resources/runtime/baldr_router-")
            and name.endswith(".whl")
        )
        expected_runtime_wheels = [f"extension/resources/runtime/{wheel_name}"]
        if runtime_wheels != expected_runtime_wheels:
            raise ReleaseConsistencyError(
                "VSIX must contain exactly the current core wheel; "
                f"expected {expected_runtime_wheels!r}, got {runtime_wheels!r}"
            )
        vsix_manifest = json.loads(vsix_archive.read("extension/package.json"))
        vsix_schema = json.loads(
            vsix_archive.read("extension/resources/work-item-progress-v1.schema.json")
        )
        vsix_deliverable_schema = json.loads(
            vsix_archive.read("extension/resources/phase-deliverable-v1.schema.json")
        )
        vsix_deliverable_page_schema = json.loads(
            vsix_archive.read("extension/resources/phase-deliverable-page-v1.schema.json")
        )
        vsix_deliverable_index_page_schema = json.loads(
            vsix_archive.read("extension/resources/phase-deliverable-index-page-v1.schema.json")
        )
        embedded_wheel = vsix_archive.read(f"extension/resources/runtime/{wheel_name}")
        presenter = vsix_archive.read("extension/dist/workItemPresentation.js")
    if vsix_manifest.get("version") != release:
        raise ReleaseConsistencyError("VSIX manifest does not match the release version")
    if vsix_schema != canonical_schema:
        raise ReleaseConsistencyError("VSIX contains a stale progress schema")
    if vsix_deliverable_schema != canonical_deliverable_schema:
        raise ReleaseConsistencyError("VSIX contains a stale deliverable schema")
    if vsix_deliverable_page_schema != canonical_deliverable_page_schema:
        raise ReleaseConsistencyError("VSIX contains a stale deliverable page schema")
    if vsix_deliverable_index_page_schema != canonical_deliverable_index_page_schema:
        raise ReleaseConsistencyError("VSIX contains a stale deliverable index schema")
    if embedded_wheel != core_wheel.read_bytes():
        raise ReleaseConsistencyError("VSIX does not embed the exact validated core wheel")
    if not presenter.strip():
        raise ReleaseConsistencyError("VSIX narrative presenter is empty")

    return release


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Fail when Baldr release versions or packaged narrative UX files drift"
    )
    parser.add_argument("--core-wheel", type=Path)
    parser.add_argument("--vsix", type=Path)
    args = parser.parse_args(argv)
    if bool(args.core_wheel) != bool(args.vsix):
        parser.error("--core-wheel and --vsix must be provided together")

    version = check_source_consistency()
    checked = ["source versions", "dependency range", "bootstraps", "contract schemas"]
    if args.core_wheel and args.vsix:
        check_artifact_consistency(args.core_wheel, args.vsix, version=version)
        checked.extend(["core wheel", "VSIX"])
    print(json.dumps({"ok": True, "version": version, "checked": checked}, indent=2))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except ReleaseConsistencyError as exc:
        raise SystemExit(f"Release consistency failed: {exc}") from exc

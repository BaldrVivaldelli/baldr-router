from __future__ import annotations

import tomllib
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]


def _project(path: Path) -> dict:
    return tomllib.loads(path.read_text(encoding="utf-8"))


def test_sdk_contains_only_authoring_runtime_surfaces() -> None:
    sdk_root = ROOT / "sdks" / "python"
    modules = {
        path.name
        for path in (sdk_root / "src" / "baldr_agent_sdk").glob("*.py")
    }
    assert modules == {"__init__.py", "agent.py", "contract.py"}
    sdk = _project(sdk_root / "pyproject.toml")
    assert "scripts" not in sdk.get("project", {})
    assert sdk["project"]["dependencies"] == []


def test_builder_owns_lifecycle_entrypoint_and_depends_only_on_sdk() -> None:
    builder_root = ROOT / "tooling" / "agent-builder"
    builder = _project(ROOT / "tooling" / "agent-builder" / "pyproject.toml")
    assert builder["project"]["scripts"] == {
        "baldr-agent": "baldr_agent_builder.cli:main"
    }
    assert builder["project"]["dependencies"] == [
        "baldr-agent-sdk>=0.20.0,<0.21.0"
    ]

    package = builder_root / "src" / "baldr_agent_builder"
    modules = {path.name for path in package.glob("*.py")}
    assert modules == {
        "__init__.py",
        "build.py",
        "backend.py",
        "client.py",
        "cli.py",
        "config.py",
        "conformance.py",
        "diagnostics.py",
        "driver.py",
        "drivers.py",
        "execution.py",
        "inventory.py",
        "models.py",
        "protocol.py",
        "release.py",
        "scaffold.py",
    }
    assert not (package / "project.py").exists()

    templates = {path.name for path in (package / "templates").glob("*.tpl")}
    assert templates == {
        "Makefile.tpl",
        "README.md.tpl",
        "README.typescript.md.tpl",
        "agent.py.tpl",
        "agent.ts.tpl",
        "baldr-agent.toml.tpl",
        "baldr-agent.typescript.toml.tpl",
        "package.json.tpl",
        "test_agent.py.tpl",
        "test_agent.mjs.tpl",
        "tsconfig.json.tpl",
    }

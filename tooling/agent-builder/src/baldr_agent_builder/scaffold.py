from __future__ import annotations

import json
import re
from importlib import resources
from pathlib import Path
from typing import Any

from baldr_agent_sdk.contract import ContractError

from .config import PROJECT_FILE, validate_name


_PLACEHOLDER = re.compile(r"\{\{[A-Z][A-Z0-9_]*\}\}")


def _write_new(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("x", encoding="utf-8") as handle:
            handle.write(content)
    except FileExistsError as exc:
        raise ContractError(f"Refusing to overwrite existing file: {path}.") from exc


def _render_template(name: str, values: dict[str, str]) -> str:
    resource = resources.files("baldr_agent_builder").joinpath("templates").joinpath(name)
    content = resource.read_text(encoding="utf-8")
    for key, value in values.items():
        content = content.replace("{{" + key + "}}", value)
    unresolved = sorted(set(_PLACEHOLDER.findall(content)))
    if unresolved:
        raise ContractError(
            f"Template {name!r} has unresolved values: {', '.join(unresolved)}."
        )
    return content


def _scaffold_name(value: str, field: str) -> str:
    normalized = validate_name(value, field)
    if normalized != value:
        raise ContractError(f"{field} must be a lowercase agent identifier.")
    return normalized


def init_project(
    directory: str | Path,
    *,
    name: str,
    owner: str,
    namespace: str,
    registry: str,
    language: str = "python",
) -> dict[str, Any]:
    name = _scaffold_name(name, "name")
    namespace = _scaffold_name(namespace, "namespace")
    registry = _scaffold_name(registry, "registry")
    owner = owner.strip()
    if not owner or len(owner) > 160:
        raise ContractError("owner must be a bounded non-empty string.")
    language = language.strip().lower()
    if language not in {"python", "typescript"}:
        raise ContractError("language must be 'python' or 'typescript'.")
    root = Path(directory).expanduser().resolve()
    if root.exists() and any(root.iterdir()):
        raise ContractError(f"Target directory is not empty: {root}.")
    root.mkdir(parents=True, exist_ok=True)
    _write_new(
        root / PROJECT_FILE,
        _render_template(
            (
                "baldr-agent.toml.tpl"
                if language == "python"
                else "baldr-agent.typescript.toml.tpl"
            ),
            {
                "NAME": name,
                "OWNER_LITERAL": json.dumps(owner, ensure_ascii=False),
                "REGISTRY": registry,
                "NAMESPACE": namespace,
            },
        ),
    )
    output_name = f"{name}_result.md"
    if language == "python":
        _write_new(
            root / "agent.py",
            _render_template("agent.py.tpl", {"OUTPUT_NAME": output_name}),
        )
        _write_new(
            root / "tests" / "test_agent.py",
            _render_template("test_agent.py.tpl", {"NAME": name}),
        )
        _write_new(
            root / "README.md",
            _render_template("README.md.tpl", {"NAME": name}),
        )
        ignore = "dist/\n__pycache__/\n*.pyc\n"
    else:
        values = {
            "NAME": name,
            "OWNER_LITERAL": json.dumps(owner, ensure_ascii=False),
            "OUTPUT_NAME": output_name,
        }
        _write_new(root / "src" / "agent.ts", _render_template("agent.ts.tpl", values))
        _write_new(
            root / "tests" / "agent.test.mjs",
            _render_template("test_agent.mjs.tpl", values),
        )
        _write_new(
            root / "package.json",
            _render_template("package.json.tpl", {"NAME": name}),
        )
        _write_new(root / "tsconfig.json", _render_template("tsconfig.json.tpl", {}))
        _write_new(
            root / "README.md",
            _render_template("README.typescript.md.tpl", {"NAME": name}),
        )
        ignore = "dist/\nnode_modules/\n"
    _write_new(root / "Makefile", _render_template("Makefile.tpl", {}))
    _write_new(
        root / "CHANGELOG.md",
        "# Changelog\n\n## 1.0.0\n\n- Initial external agent release.\n",
    )
    _write_new(root / ".gitignore", ignore)
    return {
        "ok": True,
        "project": str(root),
        "config": str(root / PROJECT_FILE),
        "language": language,
        "next": ["baldr-agent test", "baldr-agent build", "baldr-agent publish"],
    }

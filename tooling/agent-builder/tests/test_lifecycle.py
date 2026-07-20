from __future__ import annotations

import json
import os
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

from baldr_agent_sdk.contract import ContractError
from baldr_agent_builder.build import build_project
from baldr_agent_builder.cli import main
from baldr_agent_builder.config import load_project
from baldr_agent_builder.diagnostics import run_project_tests
from baldr_agent_builder.release import (
    activate_version,
    install_release,
    publish_release,
)
from baldr_agent_builder.scaffold import init_project


def _project(tmp_path: Path, name: str = "example-agent") -> Path:
    root = tmp_path / name
    result = init_project(
        root,
        name=name,
        owner="example-team",
        namespace="example",
        registry="local",
    )
    assert result["ok"] is True
    return root


def test_init_test_and_deterministic_self_contained_build(tmp_path: Path) -> None:
    root = _project(tmp_path)
    project = load_project(root)

    assert run_project_tests(project)["ok"] is True
    first = build_project(project, output_dir=tmp_path / "build-one")
    second = build_project(project, output_dir=tmp_path / "build-two")

    assert first.artifact_digest == second.artifact_digest
    assert first.metadata["builder_version"] == "0.20.0"
    assert first.metadata["sdk_version"] == "0.20.0"
    with zipfile.ZipFile(first.artifact) as archive:
        assert "baldr_agent_sdk/agent.py" in archive.namelist()
    env = os.environ.copy()
    env["BALDR_AGENT_REF"] = "local://example/example-agent-planner@1.0.0"
    health = subprocess.run(
        [sys.executable, str(first.artifact)],
        input=json.dumps(
            {
                "contract": "baldr-agent-execution",
                "version": 1,
                "kind": "health-request",
                "request_id": "build-health",
            }
        )
        + "\n",
        text=True,
        capture_output=True,
        env=env,
        check=True,
    )
    assert json.loads(health.stdout)["status"] == "ok"


def test_same_exact_version_cannot_install_changed_content(tmp_path: Path) -> None:
    root = _project(tmp_path)
    project = load_project(root)
    first = build_project(project)
    install_root = tmp_path / "installed"
    release = install_release(project, first, install_root=install_root)
    assert ".venv" not in str(release.artifact)
    assert "baldr-router" not in str(release.artifact)

    with (root / "agent.py").open("a", encoding="utf-8") as handle:
        handle.write("\n# changed without a version bump\n")
    changed = build_project(project)

    with pytest.raises(ContractError, match="bump version"):
        install_release(project, changed, install_root=install_root)


def test_same_exact_version_cannot_change_definition_or_manifest(
    tmp_path: Path,
) -> None:
    root = _project(tmp_path)
    project = load_project(root)
    build = build_project(project)
    install_root = tmp_path / "installed"
    install_release(project, build, install_root=install_root)

    config = root / "baldr-agent.toml"
    config.write_text(
        config.read_text(encoding="utf-8").replace(
            'owner = "example-team"', 'owner = "different-team"'
        ),
        encoding="utf-8",
    )
    changed_project = load_project(root)
    changed_build = build_project(changed_project)
    assert changed_build.artifact_digest != build.artifact_digest
    with pytest.raises(ContractError, match="bump version"):
        install_release(
            changed_project,
            changed_build,
            install_root=install_root,
        )

    with pytest.raises(ContractError, match="release metadata"):
        install_release(
            project,
            build,
            install_root=install_root,
            python_command="different-python",
        )


def test_local_publish_activates_new_version_and_preserves_rollback(
    tmp_path: Path,
) -> None:
    root = _project(tmp_path)
    project = load_project(root)
    release = install_release(
        project,
        build_project(project),
        install_root=tmp_path / "installed",
    )
    calls: list[list[str]] = []
    agents = [
        {
            "ref": f"local://example/example-agent-{role}@0.9.0",
            "registry": "local",
            "namespace": "example",
            "name": f"example-agent-{role}",
            "version": "0.9.0",
            "enabled": True,
            "revoked": False,
        }
        for role in ("planner", "writer", "reviewer")
    ]

    def runner(arguments, cwd):
        del cwd
        command = list(arguments)
        calls.append(command)
        if command[1:3] == ["agent", "sync"]:
            for manifest in release.manifests:
                name = manifest["ref"].rsplit("/", 1)[-1].split("@", 1)[0]
                agents.append(
                    {
                        "ref": manifest["ref"],
                        "registry": "local",
                        "namespace": "example",
                        "name": name,
                        "version": "1.0.0",
                        "enabled": True,
                        "revoked": False,
                    }
                )
            return {"ok": True}
        if command[1:3] == ["agent", "list"]:
            return {"ok": True, "agents": agents}
        return {"ok": True}

    result = publish_release(project, release, runner=runner)

    assert result["catalog"] == "local"
    disabled = [command[-1] for command in calls if command[2] == "disable"]
    assert disabled == [
        "local://example/example-agent-planner@0.9.0",
        "local://example/example-agent-reviewer@0.9.0",
        "local://example/example-agent-writer@0.9.0",
    ]
    assert all(
        (tmp_path / "installed")
        in Path(item["target"]["artifact_path"]).parents
        for item in release.manifests
    )


def test_rollback_enables_requested_version_and_disables_current(tmp_path: Path) -> None:
    project = load_project(_project(tmp_path))
    calls: list[list[str]] = []
    agents = []
    for version, enabled in (("0.9.0", False), ("1.0.0", True)):
        for role in ("planner", "writer", "reviewer"):
            agents.append(
                {
                    "ref": f"local://example/example-agent-{role}@{version}",
                    "registry": "local",
                    "namespace": "example",
                    "name": f"example-agent-{role}",
                    "version": version,
                    "enabled": enabled,
                    "revoked": False,
                }
            )

    def runner(arguments, cwd):
        del cwd
        command = list(arguments)
        calls.append(command)
        if command[1:3] == ["agent", "list"]:
            return {"ok": True, "agents": agents}
        return {"ok": True}

    result = activate_version(project, "0.9.0", runner=runner)

    assert result["version"] == "0.9.0"
    assert len(result["enabled"]) == 3
    assert len(result["disabled"]) == 3
    assert [command[2] for command in calls[1:]] == [
        "enable",
        "enable",
        "enable",
        "disable",
        "disable",
        "disable",
    ]


def test_cli_refuses_to_overwrite_an_initialized_directory(
    tmp_path: Path, capsys
) -> None:
    root = _project(tmp_path)

    exit_code = main(
        [
            "init",
            str(root),
            "--name",
            "second-agent",
            "--owner",
            "example-team",
            "--namespace",
            "example",
        ]
    )

    assert exit_code == 2
    assert json.loads(capsys.readouterr().out)["error"]["code"] == (
        "baldr_agent_operation_failed"
    )


def test_cli_sets_exact_version_without_rewriting_project_config(
    tmp_path: Path, capsys
) -> None:
    root = _project(tmp_path)
    config = root / "baldr-agent.toml"
    before = config.read_text(encoding="utf-8")

    exit_code = main(["version", "1.1.0", "--project", str(root)])
    result = json.loads(capsys.readouterr().out)
    after = config.read_text(encoding="utf-8")

    assert exit_code == 0
    assert result["previous_version"] == "1.0.0"
    assert result["version"] == "1.1.0"
    assert result["changed"] is True
    assert load_project(root).version == "1.1.0"
    assert after == before.replace('version = "1.0.0"', 'version = "1.1.0"', 1)

    assert main(["version", "1.1.0", "--project", str(root)]) == 0
    assert json.loads(capsys.readouterr().out)["changed"] is False


def test_cli_rejects_moving_project_version(tmp_path: Path, capsys) -> None:
    root = _project(tmp_path)

    exit_code = main(["version", "latest", "--project", str(root)])
    result = json.loads(capsys.readouterr().out)

    assert exit_code == 2
    assert result["error"]["code"] == "baldr_agent_operation_failed"
    assert "exact and immutable" in result["error"]["message"]
    assert load_project(root).version == "1.0.0"


def test_cli_version_preserves_literal_toml_quoting(tmp_path: Path, capsys) -> None:
    root = _project(tmp_path)
    config = root / "baldr-agent.toml"
    config.write_text(
        config.read_text(encoding="utf-8").replace(
            'version = "1.0.0"', "version = '1.0.0'", 1
        ),
        encoding="utf-8",
    )

    assert main(["version", "1.1.0", "--project", str(root)]) == 0
    capsys.readouterr()

    assert "version = '1.1.0'" in config.read_text(encoding="utf-8")
    assert load_project(root).version == "1.1.0"


def test_cli_exposes_run_and_driver_conformance(capsys) -> None:
    with pytest.raises(SystemExit) as run_help:
        main(["run", "--help"])
    assert run_help.value.code == 0
    assert "--workspace" in capsys.readouterr().out

    with pytest.raises(SystemExit) as conformance_help:
        main(["driver", "conformance", "--help"])
    assert conformance_help.value.code == 0
    assert "driver_id" in capsys.readouterr().out


def test_typescript_scaffold_uses_neutral_project_fields(tmp_path: Path) -> None:
    root = tmp_path / "typescript-agent"
    initialized = init_project(
        root,
        name="typescript-agent",
        owner="example-team",
        namespace="example",
        registry="local",
        language="typescript",
    )

    project = load_project(root)

    assert initialized["language"] == "typescript"
    assert project.schema_version == 2
    assert project.language == "typescript"
    assert str(project.entrypoint) == "src/agent.ts"
    assert project.driver == "baldr.typescript"
    assert project.runtime_command == "node"
    assert (root / "tests" / "agent.test.mjs").is_file()
    assert (root / "package.json").is_file()


def test_legacy_python_project_remains_loadable_and_buildable(tmp_path: Path) -> None:
    root = _project(tmp_path, "legacy-agent")
    config = root / "baldr-agent.toml"
    content = config.read_text(encoding="utf-8")
    content = content.replace("schema_version = 2", "schema_version = 1", 1)
    content = content.replace('language = "python"\n', "", 1)
    content = content.replace('entrypoint = "agent.py"', 'entry_module = "agent"', 1)
    content = content.replace('driver = "baldr.python"\n', "", 1)
    config.write_text(content, encoding="utf-8")

    project = load_project(root)
    build = build_project(project, output_dir=tmp_path / "legacy-dist")

    assert project.schema_version == 1
    assert project.language == "python"
    assert project.entry_module == "agent"
    assert build.artifact.suffix == ".pyz"

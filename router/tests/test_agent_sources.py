from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

import baldr_router.agent_gateway as agent_gateway_module
from baldr_router import cli
from baldr_router.agent_api import (
    AgentContractError,
    AgentDigestMismatchError,
    AgentManifest,
    AgentRef,
)
from baldr_router.agent_gateway import verify_kiro_agent_definition
from baldr_router.agent_manager import HttpAgentManagerResolver
from baldr_router.agent_sources import (
    AgentManagerSource,
    AgentSourceCandidate,
    AgentSourceContext,
    AgentSourceInfo,
    AgentSourceProvenance,
    AgentSourceResult,
    KiroAgentSource,
    ManifestAgentSource,
    parse_kiro_agent_list,
)
from baldr_router.config import AgentManagerConfig


def _manifest(reference: str = "local://team/reviewer@1.0.0") -> AgentManifest:
    return AgentManifest(
        reference=AgentRef.parse(reference),
        owner="platform",
        transport="provider",
        target={"provider": "codex", "model": "fixture"},
        capabilities=("workspace.read",),
        input_schema="baldr.Task/v1",
        output_schema="baldr.StructuredReport/v1",
    )


def _source_document() -> dict:
    source = AgentSourceInfo("team.catalog", "file", "Team catalog")
    result = AgentSourceResult(
        source=source,
        candidates=(
            AgentSourceCandidate(
                manifest=_manifest(),
                provenance=AgentSourceProvenance(
                    source_id=source.identifier,
                    source_kind=source.kind,
                    locator="agents/reviewer.json",
                    scope="workspace",
                    native_id="reviewer",
                ),
                label="Reviewer",
                description="Reviews repository changes.",
            ),
        ),
    )
    return result.to_dict()


def _write_executable(path: Path, *, version: str = "kiro-cli 1.2.3") -> Path:
    if os.name == "nt":
        path = path.with_suffix(".cmd")
        path.write_text(
            "@echo off\n"
            'if "%~1"=="--version" goto version\n'
            'if "%~1"=="agent" if "%~2"=="list" goto list\n'
            "exit /b 2\n"
            ":version\n"
            f"echo {version}\n"
            "exit /b 0\n"
            ":list\n"
            "echo Workspace: ~/.kiro/agents\n"
            "echo Global:    ~/.kiro/agents\n"
            "echo * kiro_default    ^(Built-in^)    Default agent\n"
            "echo   kiro_help       ^(Built-in^)    Help agent\n"
            "exit /b 0\n",
            encoding="utf-8",
        )
        return path
    path.write_text(
        "#!/bin/sh\n"
        "if [ \"$1\" = \"--version\" ]; then\n"
        f"  echo '{version}'\n"
        "  exit 0\n"
        "fi\n"
        "if [ \"$1\" = \"agent\" ] && [ \"$2\" = \"list\" ]; then\n"
        "  echo 'Workspace: ~/.kiro/agents'\n"
        "  echo 'Global:    ~/.kiro/agents'\n"
        "  echo '* kiro_default    (Built-in)    Default agent'\n"
        "  echo '  kiro_help       (Built-in)    Help agent'\n"
        "  exit 0\n"
        "fi\n"
        "exit 2\n",
        encoding="utf-8",
    )
    path.chmod(0o755)
    return path


def _write_stderr_executable(path: Path) -> Path:
    if os.name == "nt":
        path = path.with_suffix(".cmd")
        path.write_text(
            "@echo off\n"
            'if "%~1"=="--version" goto version\n'
            'if "%~1"=="agent" if "%~2"=="list" goto list\n'
            "exit /b 2\n"
            ":version\n"
            "echo kiro-cli 1.2.3\n"
            "exit /b 0\n"
            ":list\n"
            "echo MCP functionality disabled 1>&2\n"
            "echo * kiro_default    ^(Built-in^)    Default agent 1>&2\n"
            "exit /b 0\n",
            encoding="utf-8",
        )
        return path
    path.write_text(
        "#!/bin/sh\n"
        "if [ \"$1\" = \"--version\" ]; then\n"
        "  echo 'kiro-cli 1.2.3'\n"
        "  exit 0\n"
        "fi\n"
        "if [ \"$1\" = \"agent\" ] && [ \"$2\" = \"list\" ]; then\n"
        "  echo 'MCP functionality disabled' >&2\n"
        "  echo '* kiro_default    (Built-in)    Default agent' >&2\n"
        "  exit 0\n"
        "fi\n"
        "exit 2\n",
        encoding="utf-8",
    )
    path.chmod(0o755)
    return path


def test_agent_source_v1_round_trips_exact_manifests() -> None:
    document = _source_document()
    parsed = AgentSourceResult.from_dict(document)

    assert parsed.to_dict() == document
    assert parsed.candidates[0].manifest.digest.startswith("sha256:")
    assert parsed.candidates[0].manifest.reference == AgentRef.parse(
        "local://team/reviewer@1.0.0"
    )

    document["candidates"][0]["provenance"]["source_id"] = "different.source"
    with pytest.raises(AgentContractError, match="does not match"):
        AgentSourceResult.from_dict(document)

    with pytest.raises(AgentContractError, match="credentials, queries"):
        AgentSourceProvenance(
            source_id="team.catalog",
            source_kind="endpoint",
            locator="https://agents.example.test/source?token=inline",
        )


def test_manifest_file_source_reads_metadata_without_importing_code(
    tmp_path: Path,
) -> None:
    path = tmp_path / "agents.source.json"
    path.write_text(json.dumps(_source_document()), encoding="utf-8")
    source = ManifestAgentSource(path=path, expected_source_id="team.catalog")

    result = source.discover(context=AgentSourceContext(tmp_path))

    assert result.source.identifier == "team.catalog"
    assert [str(item.manifest.reference) for item in result.candidates] == [
        "local://team/reviewer@1.0.0"
    ]

    link = tmp_path / "agents.link.json"
    link.symlink_to(path)
    with pytest.raises(AgentContractError, match="regular file"):
        ManifestAgentSource(path=link).discover(
            context=AgentSourceContext(tmp_path)
        )


def test_agent_discover_cli_exposes_source_documents(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "agents.source.json"
    path.write_text(json.dumps(_source_document()), encoding="utf-8")

    assert cli.main(
        [
            "agent",
            "discover",
            "--source",
            "file",
            "--path",
            str(path),
            "--workspace",
            str(tmp_path),
            "--expected-source-id",
            "team.catalog",
        ]
    ) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["ok"] is True
    assert output["source_count"] == 1
    assert output["candidate_count"] == 1
    assert output["sources"][0]["contract"] == "baldr-agent-source"


def test_manifest_endpoint_source_uses_referenced_auth_without_exposing_it(
    tmp_path: Path,
) -> None:
    class Client:
        def __init__(self) -> None:
            self.request = None

        def request_json(self, **kwargs):
            self.request = kwargs
            return _source_document()

    client = Client()
    source = ManifestAgentSource(
        endpoint="https://agents.example.test/v1/source",
        authorization_env="TEAM_AGENT_TOKEN",
        expected_source_id="team.catalog",
        client=client,  # type: ignore[arg-type]
    )

    result = source.discover(context=AgentSourceContext(tmp_path))

    assert result.source.identifier == "team.catalog"
    assert client.request == {
        "method": "GET",
        "url": "https://agents.example.test/v1/source",
        "auth_env": "TEAM_AGENT_TOKEN",
        "timeout_seconds": 10,
    }
    assert "TEAM_AGENT_TOKEN" not in json.dumps(result.to_dict())


def test_parse_kiro_agent_list_only_accepts_builtin_rows() -> None:
    output = (
        "Workspace: ~/.kiro/agents\n"
        "* kiro_default    (Built-in)    Default agent\n"
        "  project_agent   (Workspace)   Do not treat this prose as a built-in\n"
        "\x1b[32m  kiro_help       (Built-in)    Help agent\x1b[0m\n"
        "                                       using documentation\n"
    )

    assert parse_kiro_agent_list(output) == (
        ("kiro_default", "Default agent"),
        ("kiro_help", "Help agent using documentation"),
    )


def test_kiro_source_discovers_builtins_and_attested_definition_versions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "workspace"
    home = tmp_path / "home"
    workspace_agents = workspace / ".kiro" / "agents"
    global_agents = home / ".kiro" / "agents"
    workspace_agents.mkdir(parents=True)
    global_agents.mkdir(parents=True)
    (workspace_agents / "team_worker.json").write_text(
        json.dumps(
            {
                "name": "team_worker",
                "description": "Workspace worker",
                "allowedTools": ["read", "write"],
            }
        ),
        encoding="utf-8",
    )
    (global_agents / "team_worker.json").write_text(
        json.dumps({"name": "team_worker", "description": "Global worker"}),
        encoding="utf-8",
    )
    (global_agents / "global_reviewer.json").write_text(
        json.dumps(
            {
                "name": "global_reviewer",
                "description": "Global reviewer",
                "allowedTools": ["read", "grep"],
            }
        ),
        encoding="utf-8",
    )
    command = _write_executable(tmp_path / "kiro-cli")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))

    result = KiroAgentSource(command=str(command)).discover(
        context=AgentSourceContext(workspace)
    )

    by_native_and_scope = {
        (item.provenance.native_id, item.provenance.scope): item
        for item in result.candidates
    }
    assert set(by_native_and_scope) == {
        ("team_worker", "workspace"),
        ("team_worker", "global"),
        ("global_reviewer", "global"),
        ("kiro_default", "builtin"),
        ("kiro_help", "builtin"),
    }
    workspace_worker = by_native_and_scope[("team_worker", "workspace")]
    global_worker = by_native_and_scope[("team_worker", "global")]
    builtin = by_native_and_scope[("kiro_default", "builtin")]
    assert workspace_worker.state == "available"
    assert global_worker.state == "shadowed"
    assert global_worker.reason == "workspace-definition-shadows-global"
    assert workspace_worker.manifest.target["definition_digest"].startswith("sha256:")
    assert workspace_worker.manifest.reference.version.startswith("sha256-")
    assert workspace_worker.manifest.effect_mode == "workspace-write"
    assert (
        by_native_and_scope[("global_reviewer", "global")].manifest.effect_mode
        == "read-only"
    )
    assert builtin.manifest.target["provider_version"] == "kiro-cli 1.2.3"
    assert builtin.manifest.target["definition_scope"] == "builtin"


def test_kiro_source_accepts_successful_catalog_written_to_stderr(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    command = _write_stderr_executable(tmp_path / "kiro-cli")
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))

    result = KiroAgentSource(command=str(command)).discover(
        context=AgentSourceContext(tmp_path)
    )

    assert [item.provenance.native_id for item in result.candidates] == [
        "kiro_default"
    ]


def test_builtin_kiro_identity_is_rechecked_before_invocation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    command = _write_executable(tmp_path / "kiro-cli")
    monkeypatch.setattr(
        agent_gateway_module,
        "load_config",
        lambda: SimpleNamespace(kiro_cli=SimpleNamespace(command=str(command))),
    )
    result = KiroAgentSource(command=str(command)).discover(
        context=AgentSourceContext(tmp_path)
    )
    builtin = next(
        item for item in result.candidates if item.provenance.native_id == "kiro_default"
    )

    verified = verify_kiro_agent_definition(
        target=builtin.manifest.target,
        cwd=tmp_path,
    )
    assert verified["definition_scope"] == "builtin"

    _write_executable(command, version="kiro-cli 2.0.0")
    with pytest.raises(AgentDigestMismatchError, match="version"):
        verify_kiro_agent_definition(
            target=builtin.manifest.target,
            cwd=tmp_path,
        )


def test_agent_manager_source_adapts_catalog_without_resolving_or_invoking(
    tmp_path: Path,
) -> None:
    class Resolver:
        registry = "company"

        def __init__(self) -> None:
            self.catalog_calls = 0

        def catalog(self):
            self.catalog_calls += 1
            return (_manifest("company://team/reviewer@2.0.0"),)

    resolver = Resolver()
    source = AgentManagerSource(
        AgentManagerConfig(),
        resolver=resolver,  # type: ignore[arg-type]
    )

    result = source.discover(context=AgentSourceContext(tmp_path))

    assert resolver.catalog_calls == 1
    assert result.source.identifier == "agent-manager.company"
    assert result.candidates[0].provenance.scope == "remote"
    assert str(result.candidates[0].manifest.reference) == (
        "company://team/reviewer@2.0.0"
    )
    assert not isinstance(resolver, HttpAgentManagerResolver)

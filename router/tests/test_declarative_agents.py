"""Agents a repository declares in a file.

The file is what a person edits and a reviewer reads in a diff; the manifest and
its digest are derived from it, never written by hand. That is the whole point:
the same file produces the same catalog on every machine, and the reconciliation
that already existed converges to it without learning anything new.

These tests hold the authoring rules, and the one that matters most — an agent
is immutable per exact version, so changing what one may touch means publishing
a new one.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from baldr_router.agent_api import AgentContractError
from baldr_router.agent_sources import AgentSourceContext, DeclarativeAgentSource

REVIEWER = """
[source]
id = "repo.agents"
label = "Agentes de este repositorio"

[[agent]]
ref = "local://equipo/revisor@1.0.0"
owner = "equipo-plataforma"
description = "Revisa la superficie pública"
capabilities = ["workspace.read", "role.reviewer"]
provider = "claude"
model = "opus"
tools = "Read,Grep"
instructions = "Mirá autenticación y manejo de secretos."
"""


def _discover(tmp_path: Path, body: str, *, name: str = "baldr-agents.toml"):
    (tmp_path / name).write_text(body, encoding="utf-8")
    source = DeclarativeAgentSource(path=Path(name))
    return source.discover(context=AgentSourceContext(tmp_path))


# --- what the file produces ---------------------------------------------------


def test_a_declared_agent_becomes_a_manifest_with_a_computed_digest(
    tmp_path: Path,
) -> None:
    result = _discover(tmp_path, REVIEWER)

    assert result.source.identifier == "repo.agents"
    assert result.source.kind == "declarative"
    candidate = result.candidates[0]
    manifest = candidate.manifest
    assert str(manifest.reference) == "local://equipo/revisor@1.0.0"
    # Derived, not authored: nobody can write one of these by hand.
    assert manifest.digest.startswith("sha256:")
    assert len(manifest.digest) == 71
    assert candidate.state == "available"
    assert candidate.provenance.source_kind == "declarative"


def test_the_flat_fields_land_where_the_manifest_expects_them(
    tmp_path: Path,
) -> None:
    """One table per agent; the code knows which half each key belongs to."""

    manifest = _discover(tmp_path, REVIEWER).candidates[0].manifest

    assert manifest.transport == "provider"
    assert dict(manifest.target) == {
        "provider": "claude",
        "model": "opus",
        "tools": "Read,Grep",
        "instructions": "Mirá autenticación y manejo de secretos.",
    }
    assert manifest.owner == "equipo-plataforma"
    # The contract every Baldr phase speaks, filled in so a file need not.
    assert manifest.input_schema == "baldr.Task/v1"
    assert manifest.output_schema == "baldr.StructuredReport/v1"


def test_an_agent_is_read_only_unless_the_file_says_otherwise(
    tmp_path: Path,
) -> None:
    """An agent that can change a workspace should be visible in the diff."""

    minimal = """
[[agent]]
ref = "local://equipo/minimo@1.0.0"
owner = "equipo"
provider = "codex"
"""
    manifest = _discover(tmp_path, minimal).candidates[0].manifest

    assert manifest.effect_mode == "read-only"
    assert manifest.capabilities == ("workspace.read",)


def test_a_writing_agent_declares_both_halves(tmp_path: Path) -> None:
    writing = """
[[agent]]
ref = "local://equipo/ejecutor@1.0.0"
owner = "equipo"
capabilities = ["workspace.read", "workspace.write", "role.implementer"]
effect_mode = "workspace-write"
provider = "claude"
"""
    manifest = _discover(tmp_path, writing).candidates[0].manifest

    assert manifest.effect_mode == "workspace-write"
    assert "workspace.write" in manifest.capabilities


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        (
            """
[[agent]]
ref = "local://equipo/a@1.0.0"
owner = "equipo"
provider = "claude"
effect_mode = "workspace-write"
""",
            "workspace.write capability",
        ),
        (
            """
[[agent]]
ref = "local://equipo/a@1.0.0"
owner = "equipo"
provider = "claude"
capabilities = ["workspace.read", "workspace.write"]
""",
            "not workspace-write",
        ),
    ],
)
def test_the_two_halves_of_write_permission_must_agree(
    tmp_path: Path, body: str, expected: str
) -> None:
    """The resolver refuses a mismatch later; the file refuses it now."""

    with pytest.raises(AgentContractError, match=expected):
        _discover(tmp_path, body)


# --- refusing a file rather than half-reading it ------------------------------


def test_an_unknown_field_stops_the_plan(tmp_path: Path) -> None:
    """A typo in a security-relevant file must not become a missing rule."""

    body = """
[[agent]]
ref = "local://equipo/a@1.0.0"
owner = "equipo"
provider = "claude"
tolls = "Read"
"""
    with pytest.raises(AgentContractError, match="tolls"):
        _discover(tmp_path, body)


def test_an_agent_without_a_ref_is_refused(tmp_path: Path) -> None:
    with pytest.raises(AgentContractError, match="needs a ref"):
        _discover(tmp_path, '[[agent]]\nowner = "equipo"\nprovider = "claude"\n')


def test_an_agent_without_a_target_is_refused(tmp_path: Path) -> None:
    body = '[[agent]]\nref = "local://equipo/a@1.0.0"\nowner = "equipo"\n'

    with pytest.raises(AgentContractError, match="at least a provider"):
        _discover(tmp_path, body)


def test_the_same_reference_twice_is_refused(tmp_path: Path) -> None:
    """Two tables for one agent is a merge conflict nobody resolved."""

    body = REVIEWER + """
[[agent]]
ref = "local://equipo/revisor@1.0.0"
owner = "equipo"
provider = "codex"
"""
    with pytest.raises(AgentContractError, match="more than once"):
        _discover(tmp_path, body)


def test_an_unknown_section_is_refused(tmp_path: Path) -> None:
    with pytest.raises(AgentContractError, match="agents"):
        _discover(tmp_path, '[agents]\nfoo = 1\n')


def test_a_missing_file_says_which_one(tmp_path: Path) -> None:
    source = DeclarativeAgentSource(path=Path("no-such-file.toml"))

    with pytest.raises(AgentContractError, match="no-such-file.toml"):
        source.discover(context=AgentSourceContext(tmp_path))


def test_invalid_toml_is_reported_rather_than_crashing(tmp_path: Path) -> None:
    with pytest.raises(AgentContractError, match="not valid"):
        _discover(tmp_path, "[[agent]\nref = ")


def test_json_is_accepted_for_a_generated_file(tmp_path: Path) -> None:
    """The same shape, for a file something else wrote."""

    body = (
        '{"agent": [{"ref": "local://equipo/a@1.0.0", "owner": "equipo", '
        '"provider": "claude"}]}'
    )
    result = _discover(tmp_path, body, name="baldr-agents.json")

    assert str(result.candidates[0].manifest.reference) == "local://equipo/a@1.0.0"


# --- the rule that shapes the workflow ----------------------------------------


def test_changing_an_agent_changes_its_digest(tmp_path: Path) -> None:
    """Which is why editing one in place is a conflict, not an update.

    An exact version is immutable, so the reconciliation refuses a file whose
    agent no longer matches what was registered under that reference. Changing
    what an agent may touch means publishing a new version, and a durable run's
    record of who did what stays true afterwards.
    """
    before = _discover(tmp_path, REVIEWER).candidates[0].manifest.digest
    after = (
        _discover(tmp_path, REVIEWER.replace('"Read,Grep"', '"Read,Grep,Glob"'))
        .candidates[0]
        .manifest.digest
    )

    assert before != after


def test_the_same_file_produces_the_same_catalog_twice(tmp_path: Path) -> None:
    """Reproducible across machines is the point of declaring it in a file."""

    first = _discover(tmp_path, REVIEWER).candidates[0].manifest.digest
    second = _discover(tmp_path, REVIEWER).candidates[0].manifest.digest

    assert first == second


def test_more_agents_than_the_limit_are_reported(tmp_path: Path) -> None:
    body = "".join(
        f'[[agent]]\nref = "local://equipo/a{n}@1.0.0"\nowner = "e"\nprovider = "codex"\n'
        for n in range(5)
    )
    (tmp_path / "baldr-agents.toml").write_text(body, encoding="utf-8")

    result = DeclarativeAgentSource().discover(
        context=AgentSourceContext(tmp_path, limit=2)
    )

    assert len(result.candidates) == 2
    assert result.warnings[0].code == "candidate-limit-reached"

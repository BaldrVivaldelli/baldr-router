from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest

from baldr_router import cli
from baldr_router.agent_api import (
    AgentContractError,
    AgentManifest,
    AgentNotFoundError,
    AgentRef,
    AgentResolutionContext,
)
from baldr_router.agent_registry import (
    LocalAgentRegistry,
    LocalAgentRegistryAdmin,
    agent_registry_status,
)
from baldr_router.durability.store import DurableStore


def _manifest(version: str = "1.0.0", *, agent: str = "worker") -> AgentManifest:
    return AgentManifest(
        reference=AgentRef.parse(f"local://pilot/worker@{version}"),
        owner="pilot-team",
        transport="provider",
        target={"provider": "kiro-cli", "agent": agent},
        capabilities=("workspace.read",),
        input_schema="baldr.Task/v1",
        output_schema="baldr.StructuredReport/v1",
        effect_mode="read-only",
    )


def test_local_registry_admin_publishes_idempotently_and_versions_updates(
    tmp_path: Path,
) -> None:
    path = tmp_path / "config" / "agents.json"
    admin = LocalAgentRegistryAdmin(path)
    first = _manifest()

    created = admin.publish(first)
    repeated = admin.publish(first)
    second = _manifest("1.1.0", agent="worker-v2")
    updated = admin.publish(second)

    assert created["created"] is True
    assert repeated["created"] is False
    assert updated["created"] is True
    assert [str(item.reference) for item in LocalAgentRegistry(path).manifests()] == [
        "local://pilot/worker@1.0.0",
        "local://pilot/worker@1.1.0",
    ]
    if os.name != "nt":
        assert stat.S_IMODE(path.stat().st_mode) == 0o600

    changed_in_place = _manifest(agent="silently-changed")
    with pytest.raises(AgentContractError, match="publish a new exact version"):
        admin.publish(changed_in_place)


def test_local_registry_admin_disables_and_removes_only_when_safe(
    tmp_path: Path,
) -> None:
    path = tmp_path / "agents.json"
    admin = LocalAgentRegistryAdmin(path)
    manifest = _manifest()
    reference = str(manifest.reference)
    admin.publish(manifest)

    with pytest.raises(AgentContractError, match="Disable agent"):
        admin.remove(reference)

    disabled = admin.set_enabled(reference, enabled=False)
    assert disabled["agent"]["enabled"] is False
    assert admin.inspect(reference)["agent"]["enabled"] is False
    assert agent_registry_status(path)["agents"][0]["enabled"] is False
    with pytest.raises(AgentNotFoundError):
        LocalAgentRegistry(path).resolve(
            manifest.reference,
            context=AgentResolutionContext(),
        )
    with pytest.raises(AgentContractError, match="active durable runs"):
        admin.remove(reference, active_run_ids=["workflow-active"])

    removed = admin.remove(reference)
    assert removed["removed"] == reference
    assert agent_registry_status(path)["agent_count"] == 0


def test_local_registry_admin_reenables_an_exact_version(tmp_path: Path) -> None:
    path = tmp_path / "agents.json"
    admin = LocalAgentRegistryAdmin(path)
    manifest = _manifest()
    admin.publish(manifest)
    admin.set_enabled(manifest.reference, enabled=False)

    enabled = admin.set_enabled(manifest.reference, enabled=True)
    resolved = LocalAgentRegistry(path).resolve(
        manifest.reference,
        context=AgentResolutionContext(),
        expected_digest=manifest.digest,
    )

    assert enabled["agent"]["enabled"] is True
    assert resolved.manifest.digest == manifest.digest


def test_local_registry_revoke_is_irreversible_and_tombstones_removed_refs(
    tmp_path: Path,
) -> None:
    path = tmp_path / "agents.json"
    admin = LocalAgentRegistryAdmin(path)
    manifest = _manifest()
    reference = str(manifest.reference)
    admin.publish(manifest)

    first = admin.revoke(reference)
    repeated = admin.revoke(reference)

    assert first["changed"] is True
    assert repeated["changed"] is False
    assert LocalAgentRegistry(path).revoked_references() == frozenset({reference})
    with pytest.raises(AgentContractError, match="cannot be enabled"):
        admin.set_enabled(reference, enabled=True)
    with pytest.raises(AgentNotFoundError):
        LocalAgentRegistry(path).resolve(
            manifest.reference,
            context=AgentResolutionContext(),
        )

    with pytest.raises(AgentContractError, match="irreversible tombstone"):
        admin.remove(reference)

    removable = _manifest("2.0.0", agent="removable")
    removable_reference = str(removable.reference)
    admin.publish(removable)
    admin.set_enabled(removable_reference, enabled=False)
    admin.remove(removable_reference)
    assert LocalAgentRegistry(path).tombstones()[removable_reference] == removable.digest
    changed = _manifest(agent="different-content")
    changed_removable = AgentManifest(
        reference=removable.reference,
        owner=changed.owner,
        transport=changed.transport,
        target=changed.target,
        capabilities=changed.capabilities,
        input_schema=changed.input_schema,
        output_schema=changed.output_schema,
        effect_mode=changed.effect_mode,
    )
    with pytest.raises(AgentContractError, match="tombstoned"):
        admin.publish(changed_removable)
    restored = admin.publish(removable)
    assert restored["created"] is True
    assert LocalAgentRegistry(path).resolve(
        removable.reference,
        context=AgentResolutionContext(),
        expected_digest=removable.digest,
    ).manifest.digest == removable.digest


def test_local_registry_reconcile_validates_then_writes_once(tmp_path: Path) -> None:
    path = tmp_path / "agents.json"
    admin = LocalAgentRegistryAdmin(path)
    first = _manifest()
    second = _manifest("2.0.0", agent="worker-v2")

    applied = admin.reconcile(publish=[first, second], disable=[str(first.reference)])
    repeated = admin.reconcile(publish=[first, second], disable=[str(first.reference)])

    assert applied["changed"] is True
    assert applied["created"] == [str(first.reference), str(second.reference)]
    assert applied["disabled"] == [str(first.reference)]
    assert repeated["changed"] is False
    assert LocalAgentRegistry(path).disabled_references() == frozenset(
        {str(first.reference)}
    )

    conflicting = _manifest(agent="changed-in-place")
    with pytest.raises(AgentContractError, match="immutable"):
        admin.reconcile(publish=[conflicting], enable=[str(first.reference)])
    assert LocalAgentRegistry(path).disabled_references() == frozenset(
        {str(first.reference)}
    )


def test_agent_cli_manages_the_registry_without_manual_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("BALDR_AGENT_REGISTRY_PATH", str(tmp_path / "agents.json"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    reference = "local://pilot/cli-worker@1.0.0"

    assert cli.main(
        [
            "agent",
            "publish",
            reference,
            "--owner",
            "pilot-team",
            "--transport",
            "provider",
            "--target",
            "provider=kiro-cli",
            "--target",
            "agent=cli-worker",
            "--capability",
            "workspace.read",
        ]
    ) == 0
    published = json.loads(capsys.readouterr().out)
    assert published["agent"]["ref"] == reference

    assert cli.main(["agent", "disable", reference]) == 0
    capsys.readouterr()
    assert cli.main(["agent", "inspect", reference]) == 0
    inspected = json.loads(capsys.readouterr().out)
    assert inspected["agent"]["enabled"] is False

    assert cli.main(["agent", "remove", reference]) == 0
    removed = json.loads(capsys.readouterr().out)
    assert removed["removed"] == reference


def test_agent_cli_requires_exact_confirmation_before_revocation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("BALDR_AGENT_REGISTRY_PATH", str(tmp_path / "agents.json"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    manifest = _manifest()
    reference = str(manifest.reference)
    LocalAgentRegistryAdmin().publish(manifest)

    assert cli.main(
        ["agent", "revoke", reference, "--confirm-reference", "local://pilot/worker@2"]
    ) == 2
    error = json.loads(capsys.readouterr().out)
    assert "--confirm-reference" in error["error"]["message"]
    assert LocalAgentRegistryAdmin().inspect(reference)["agent"]["revoked"] is False

    assert cli.main(
        ["agent", "revoke", reference, "--confirm-reference", reference]
    ) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["agent"]["revoked"] is True
    assert LocalAgentRegistryAdmin().inspect(reference)["agent"]["revoked"] is True


def test_durable_store_blocks_removal_for_frozen_nonterminal_agent_refs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    store = DurableStore(path=tmp_path / "state.sqlite3")
    reference = "local://pilot/worker@1.0.0"
    run_id = "workflow-agent-active"
    store.create_run_with_input(
        run_id=run_id,
        idempotency_key="agent-active",
        request_fingerprint="agent-active-fingerprint",
        resume_token="synthetic-agent-active-resume",
        workflow_name="architect-implement-review",
        workflow_version=1,
        workspace_root=str(tmp_path),
        workspace_id="workspace-agent-active",
        repository_identity={},
        client_name="test",
        input_value={"task": "pilot"},
        config_snapshot={
            "role_plans": {
                "reviewer": {"profiles": [{"agent_ref": reference}]}
            }
        },
    )

    assert store.active_runs_using_agent(reference) == [run_id]
    store.transition_run(run_id, "cancelled")
    assert store.active_runs_using_agent(reference) == []

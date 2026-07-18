from __future__ import annotations

import json
import stat
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from baldr_router import cli
from baldr_router.agent_api import AgentContractError, AgentManifest, AgentRef
from baldr_router.agent_registry import LocalAgentRegistry, LocalAgentRegistryAdmin
from baldr_router.agent_sources import (
    AgentSourceCandidate,
    AgentSourceInfo,
    AgentSourceProvenance,
    AgentSourceResult,
    AgentSourceWarning,
)
from baldr_router.agent_sync import (
    AgentCatalogSynchronizer,
    agent_sync_state_status,
)


SOURCE = AgentSourceInfo("product.agents", "file", "Product agents")


def _manifest(version: str = "1.0.0", *, agent: str = "reviewer") -> AgentManifest:
    return AgentManifest(
        reference=AgentRef.parse(f"local://product/reviewer@{version}"),
        owner="product",
        transport="provider",
        target={"provider": "codex", "model": agent},
        capabilities=("workspace.read",),
        input_schema="baldr.Task/v1",
        output_schema="baldr.StructuredReport/v1",
        effect_mode="read-only",
    )


def _result(
    manifests: tuple[AgentManifest, ...] = (),
    *,
    state: str = "available",
    reason: str = "",
    warnings: tuple[AgentSourceWarning, ...] = (),
) -> AgentSourceResult:
    return AgentSourceResult(
        source=SOURCE,
        candidates=tuple(
            AgentSourceCandidate(
                manifest=manifest,
                provenance=AgentSourceProvenance(
                    source_id=SOURCE.identifier,
                    source_kind=SOURCE.kind,
                    native_id=manifest.reference.name,
                    scope="fixture",
                ),
                state=state,
                reason=reason,
                label=manifest.reference.name,
            )
            for manifest in manifests
        ),
        warnings=warnings,
    )


def _sync(tmp_path: Path, *, active=None) -> AgentCatalogSynchronizer:
    return AgentCatalogSynchronizer(
        tmp_path / "agents.json",
        active_run_lookup=active or (lambda reference: []),
    )


def test_sync_preview_apply_is_idempotent_and_versions_without_overwrite(
    tmp_path: Path,
) -> None:
    sync = _sync(tmp_path)
    first = _manifest()
    first_result = _result((first,))

    preview = sync.preview(first_result)
    applied = sync.apply(first_result)
    repeated = sync.apply(first_result)

    assert preview.summary["new"] == 1
    assert preview.items[0].suggested_action == "publish"
    assert applied["registry"]["created"] == [str(first.reference)]
    assert repeated["changed"] is False
    assert repeated["applied"] == []
    assert agent_sync_state_status(sync.state_path)["event_count"] == 1

    second = _manifest("2.0.0", agent="reviewer-v2")
    second_result = _result((second,))
    version_preview = sync.preview(second_result)

    assert version_preview.summary["new-version"] == 1
    assert version_preview.summary["absent"] == 1
    assert {
        item.status: item.suggested_action for item in version_preview.items
    } == {"absent": "disable", "new-version": "publish"}

    sync.apply(second_result)
    registry = LocalAgentRegistry(sync.registry_path)
    assert [str(item.reference) for item in registry.manifests()] == [
        "local://product/reviewer@1.0.0",
        "local://product/reviewer@2.0.0",
    ]
    assert registry.disabled_references() == frozenset()

    disabled = sync.apply(second_result, missing_action="disable")
    repeated_disable = sync.apply(second_result, missing_action="disable")
    assert disabled["registry"]["disabled"] == [str(first.reference)]
    assert repeated_disable["changed"] is False
    assert registry.disabled_references() == frozenset({str(first.reference)})


def test_sync_observes_existing_manual_agent_without_taking_lifecycle_control(
    tmp_path: Path,
) -> None:
    sync = _sync(tmp_path)
    manifest = _manifest()
    admin = LocalAgentRegistryAdmin(sync.registry_path)
    admin.publish(manifest)
    admin.set_enabled(manifest.reference, enabled=False)

    preview = sync.preview(_result((manifest,)))
    first = sync.apply(_result((manifest,)))
    second = sync.apply(_result((manifest,)))

    assert preview.items[0].status == "disabled"
    assert preview.items[0].management == "untracked"
    assert preview.items[0].suggested_action == "track"
    assert first["registry"]["enabled"] == []
    assert first["applied"][0]["action"] == "track"
    assert second["changed"] is False
    assert LocalAgentRegistry(sync.registry_path).disabled_references() == frozenset(
        {str(manifest.reference)}
    )
    status = agent_sync_state_status(sync.state_path)
    assert status["sources"][SOURCE.identifier]["observed_count"] == 1


def test_sync_blocks_an_exact_reference_digest_conflict_atomically(
    tmp_path: Path,
) -> None:
    sync = _sync(tmp_path)
    original = _manifest()
    LocalAgentRegistryAdmin(sync.registry_path).publish(original)
    changed = _manifest(agent="changed-in-place")

    preview = sync.preview(_result((changed,)))

    assert preview.items[0].status == "conflict"
    assert preview.items[0].suggested_action == "blocked"
    with pytest.raises(AgentContractError, match="immutable/revoked"):
        sync.apply(_result((changed,)))
    stored = LocalAgentRegistry(sync.registry_path).manifests()[0]
    assert stored.digest == original.digest
    assert not sync.state_path.exists()


def test_sync_never_removes_missing_agents_from_incomplete_discovery(
    tmp_path: Path,
) -> None:
    sync = _sync(tmp_path)
    manifest = _manifest()
    sync.apply(_result((manifest,)))
    incomplete = _result(
        warnings=(
            AgentSourceWarning(
                "source-unavailable",
                "The source returned only a partial catalog.",
            ),
        )
    )

    preview = sync.preview(incomplete)

    assert preview.complete is False
    assert preview.items[0].status == "absent"
    with pytest.raises(AgentContractError, match="incomplete discovery"):
        sync.apply(incomplete, missing_action="disable")
    kept = sync.apply(incomplete)
    assert kept["changed"] is False
    assert LocalAgentRegistry(sync.registry_path).disabled_references() == frozenset()


def test_sync_blocks_lifecycle_changes_used_by_active_durable_runs(
    tmp_path: Path,
) -> None:
    active_reference = ""

    def active(reference: str) -> list[str]:
        return ["workflow-active"] if reference == active_reference else []

    sync = _sync(tmp_path, active=active)
    manifest = _manifest()
    active_reference = str(manifest.reference)
    sync.apply(_result((manifest,)))

    with pytest.raises(AgentContractError, match="workflow-active"):
        sync.apply(_result(), missing_action="disable")
    assert LocalAgentRegistry(sync.registry_path).disabled_references() == frozenset()


def test_sync_revoke_requires_source_confirmation_and_is_irreversible(
    tmp_path: Path,
) -> None:
    sync = _sync(tmp_path)
    manifest = _manifest()
    sync.apply(_result((manifest,)))

    with pytest.raises(AgentContractError, match="confirm_revoke"):
        sync.apply(_result(), missing_action="revoke")
    revoked = sync.apply(
        _result(),
        missing_action="revoke",
        confirm_revoke=SOURCE.identifier,
        actor="catalog-admin",
    )

    assert revoked["registry"]["revoked"] == [str(manifest.reference)]
    registry = LocalAgentRegistry(sync.registry_path)
    assert registry.revoked_references() == frozenset({str(manifest.reference)})
    preview = sync.preview(_result((manifest,)))
    assert preview.items[0].status == "revoked"
    assert preview.items[0].suggested_action == "blocked"
    with pytest.raises(AgentContractError, match="immutable/revoked"):
        sync.apply(_result((manifest,)))

    state = json.loads(sync.state_path.read_text(encoding="utf-8"))
    assert state["events"][-1]["actor"] == "catalog-admin"
    assert state["events"][-1]["changes"][0]["action"] == "revoke"
    assert stat.S_IMODE(sync.state_path.stat().st_mode) == 0o600


def test_sync_unavailable_managed_candidate_requires_explicit_lifecycle_action(
    tmp_path: Path,
) -> None:
    sync = _sync(tmp_path)
    manifest = _manifest()
    sync.apply(_result((manifest,)))
    unavailable = _result(
        (manifest,),
        state="shadowed",
        reason="workspace-definition-shadows-global",
    )

    preview = sync.preview(unavailable)
    kept = sync.apply(unavailable)
    disabled = sync.apply(unavailable, missing_action="disable")

    assert preview.items[0].status == "unavailable"
    assert preview.items[0].suggested_action == "disable"
    assert kept["changed"] is False
    assert disabled["registry"]["disabled"] == [str(manifest.reference)]


def test_agent_sync_cli_previews_then_applies_the_same_contract(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    registry = tmp_path / "config" / "agents.json"
    source_file = tmp_path / "product.source.json"
    source_file.write_text(json.dumps(_result((_manifest(),)).to_dict()), encoding="utf-8")
    monkeypatch.setenv("BALDR_AGENT_REGISTRY_PATH", str(registry))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    arguments = [
        "agent",
        "sync",
        "--source",
        "file",
        "--path",
        str(source_file),
        "--workspace",
        str(tmp_path),
        "--expected-source-id",
        SOURCE.identifier,
    ]

    assert cli.main(arguments) == 0
    preview = json.loads(capsys.readouterr().out)
    assert preview["mode"] == "preview"
    assert preview["results"][0]["summary"]["new"] == 1
    assert not registry.exists()

    assert cli.main([*arguments, "--apply"]) == 0
    applied = json.loads(capsys.readouterr().out)
    assert applied["mode"] == "apply"
    assert applied["results"][0]["registry"]["created"] == [
        "local://product/reviewer@1.0.0"
    ]

    assert cli.main([*arguments, "--apply"]) == 0
    repeated = json.loads(capsys.readouterr().out)
    assert repeated["results"][0]["changed"] is False


def test_agent_sync_plan_and_state_match_the_packaged_json_schema(
    tmp_path: Path,
) -> None:
    sync = _sync(tmp_path)
    result = _result((_manifest(),))
    plan = sync.preview(result)
    sync.apply(result)
    state = json.loads(sync.state_path.read_text(encoding="utf-8"))
    schema = json.loads(
        (
            Path(__file__).parents[2]
            / "contracts"
            / "agent-catalog-sync-v1.schema.json"
        ).read_text(encoding="utf-8")
    )
    validator = Draft202012Validator(schema)

    validator.validate(plan.to_dict())
    validator.validate(state)

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from baldr_router.team_resolution import (
    TeamResolutionError,
    normalize_agent_overrides,
    normalize_team_mode,
    resolve_team,
)


def _digest(character: str) -> str:
    return "sha256:" + character * 64


def _agent(
    reference: str,
    *,
    capabilities: tuple[str, ...],
    effect_mode: str,
    ready: bool = True,
    enabled: bool = True,
    digest_character: str = "a",
) -> dict:
    version = reference.rsplit("@", 1)[1]
    return {
        "ref": reference,
        "version": version,
        "digest": _digest(digest_character),
        "transport": "provider",
        "capabilities": list(capabilities),
        "effect_mode": effect_mode,
        "ready": ready,
        "enabled": enabled,
        "state": "ready" if ready else "unavailable",
        "reason": None if ready else "provider-login-required",
    }


def _plans(*, preferred_writer: str = "") -> dict:
    result = {}
    for role, can_write in (
        ("architect", False),
        ("implementer", True),
        ("reviewer", False),
    ):
        result[role] = {
            "role": role,
            "strategy": "first-success",
            "min_successes": 1,
            "min_approvals": 1,
            "resolution": "first-success",
            "can_write": can_write,
            "sandbox": "workspace-write" if can_write else "read-only",
            "profiles": [
                {
                    "name": f"configured-{role}",
                    "provider": "codex",
                    "model": "fixture",
                    "reasoning_effort": "medium",
                    "agent": "",
                    "effort": "",
                    "runner": "exec-json",
                    "session_scope": "workspace",
                    "can_write": can_write,
                    "sandbox": "workspace-write" if can_write else "read-only",
                    "description": "Configured fallback",
                    "agent_ref": preferred_writer if role == "implementer" else "",
                    "agent_manifest_digest": "",
                }
            ],
        }
    return result


def _catalog() -> dict:
    return {
        "agents": [
            _agent(
                "local://kiro/planner@1.0.0",
                capabilities=("workspace.read", "role.architect"),
                effect_mode="read-only",
                digest_character="1",
            ),
            _agent(
                "local://kiro/writer@1.0.0",
                capabilities=(
                    "workspace.read",
                    "workspace.write",
                    "role.implementer",
                ),
                effect_mode="workspace-write",
                digest_character="2",
            ),
            _agent(
                "local://kiro/writer@2.0.0",
                capabilities=(
                    "workspace.read",
                    "workspace.write",
                    "role.implementer",
                ),
                effect_mode="workspace-write",
                digest_character="3",
            ),
            _agent(
                "local://kiro/reviewer@1.0.0",
                capabilities=("workspace.read", "role.reviewer"),
                effect_mode="read-only",
                digest_character="4",
            ),
            _agent(
                "local://kiro/help@1.0.0",
                capabilities=("workspace.read", "workspace.write", "role.help"),
                effect_mode="workspace-write",
                digest_character="5",
            ),
            _agent(
                "local://kiro/offline@9.0.0",
                capabilities=("workspace.read", "role.architect"),
                effect_mode="read-only",
                ready=False,
                digest_character="6",
            ),
        ]
    }


def test_automatic_team_filters_by_role_effect_health_and_version() -> None:
    resolution = resolve_team(_plans(), _catalog(), mode="automatic")

    assert resolution.roles["architect"].agent_ref == "local://kiro/planner@1.0.0"
    assert resolution.roles["implementer"].agent_ref == "local://kiro/writer@2.0.0"
    assert resolution.roles["reviewer"].agent_ref == "local://kiro/reviewer@1.0.0"
    assert all(
        role.selection == "automatic-agent" for role in resolution.roles.values()
    )
    assert resolution.plans["implementer"]["profiles"][0]["agent_ref"] == (
        "local://kiro/writer@2.0.0"
    )
    assert resolution.plans["implementer"]["profiles"][0][
        "agent_manifest_digest"
    ] == _digest("3")
    assert resolution.plans["implementer"]["profiles"][0]["can_write"] is True
    assert resolution.to_dict()["contract"] == "baldr-team-resolution"


def test_configured_mode_preserves_profiles_and_automatic_has_clear_fallback() -> None:
    plans = _plans()
    configured = resolve_team(plans, _catalog(), mode="configured")
    fallback = resolve_team(plans, {"agents": []}, mode="automatic")

    assert configured.plans == plans
    assert configured.roles["architect"].selection == "configured-profile"
    assert "configuración elegida" in configured.roles["architect"].message
    assert fallback.plans == plans
    assert "No hay un agente externo compatible" in fallback.roles["reviewer"].message


def test_existing_exact_profile_is_a_preference_over_newer_version() -> None:
    preferred = "local://kiro/writer@1.0.0"
    resolution = resolve_team(
        _plans(preferred_writer=preferred),
        _catalog(),
        mode="automatic",
    )

    assert resolution.roles["implementer"].agent_ref == preferred


def test_explicit_override_wins_and_fails_loudly_when_unusable() -> None:
    override = "local://kiro/writer@1.0.0"
    resolution = resolve_team(
        _plans(),
        _catalog(),
        mode="automatic",
        overrides={"implementer": override},
    )

    assert resolution.roles["implementer"].selection == "explicit-override"
    assert resolution.roles["implementer"].agent_ref == override

    with pytest.raises(TeamResolutionError) as missing:
        resolve_team(
            _plans(),
            _catalog(),
            mode="automatic",
            overrides={"reviewer": "local://kiro/missing@1.0.0"},
        )
    assert missing.value.code == "team_override_not_found"
    assert missing.value.role == "reviewer"
    assert "no está registrada" in str(missing.value)

    with pytest.raises(TeamResolutionError) as incompatible:
        resolve_team(
            _plans(),
            _catalog(),
            mode="automatic",
            overrides={"implementer": "local://kiro/reviewer@1.0.0"},
        )
    assert incompatible.value.code == "team_override_incompatible"
    assert "solo lectura" in str(incompatible.value)


def test_catalog_identity_conflicts_and_invalid_overrides_are_rejected() -> None:
    catalog = _catalog()
    duplicate = copy.deepcopy(catalog["agents"][0])
    duplicate["digest"] = _digest("f")
    catalog["agents"].append(duplicate)

    with pytest.raises(TeamResolutionError) as conflict:
        resolve_team(_plans(), catalog, mode="automatic")
    assert conflict.value.code == "team_catalog_identity_conflict"

    with pytest.raises(ValueError, match="Unknown agent override roles"):
        normalize_agent_overrides({"unknown": "local://kiro/a@1"})
    with pytest.raises(ValueError, match="Unsupported team mode"):
        normalize_team_mode("random")


def test_legacy_manifests_without_role_capabilities_remain_compatible() -> None:
    catalog = {
        "agents": [
            _agent(
                "local://legacy/reader@1.0.0",
                capabilities=("workspace.read",),
                effect_mode="read-only",
            ),
            _agent(
                "local://legacy/writer@1.0.0",
                capabilities=("workspace.read", "workspace.write"),
                effect_mode="workspace-write",
                digest_character="b",
            ),
        ]
    }

    resolution = resolve_team(_plans(), catalog, mode="automatic")

    assert resolution.roles["architect"].agent_ref == "local://legacy/reader@1.0.0"
    assert resolution.roles["reviewer"].agent_ref == "local://legacy/reader@1.0.0"
    assert resolution.roles["implementer"].agent_ref == "local://legacy/writer@1.0.0"


def test_resolution_document_satisfies_the_public_v1_schema() -> None:
    schema_path = (
        Path(__file__).resolve().parents[2]
        / "contracts"
        / "agent-team-resolution-v1.schema.json"
    )
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)

    errors = list(
        Draft202012Validator(schema).iter_errors(
            resolve_team(_plans(), _catalog(), mode="automatic").to_dict()
        )
    )

    assert errors == []

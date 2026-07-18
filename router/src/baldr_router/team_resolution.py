from __future__ import annotations

import copy
import re
from dataclasses import dataclass
from typing import Any, Mapping

from .agent_api import AgentContractError, AgentRef

TEAM_RESOLUTION_CONTRACT = "baldr-team-resolution"
TEAM_RESOLUTION_VERSION = 1
TEAM_ROLES = ("architect", "implementer", "reviewer")
TEAM_MODES = {"automatic", "configured"}

_SEMVER = re.compile(
    r"^(?P<major>\d+)(?:\.(?P<minor>\d+))?(?:\.(?P<patch>\d+))?(?:[-+].*)?$"
)
_ROLE_CAPABILITIES = {
    "architect": {"role.architect", "planning", "plan"},
    "implementer": {"role.implementer", "implementation", "implement"},
    "reviewer": {"role.reviewer", "review", "verification"},
}


class TeamResolutionError(AgentContractError):
    def __init__(self, message: str, *, code: str, role: str = "") -> None:
        super().__init__(message)
        self.code = code
        self.role = role


def normalize_team_mode(value: Any) -> str:
    mode = str(value or "configured").strip().lower().replace("_", "-")
    aliases = {
        "auto": "automatic",
        "manual": "configured",
        "profiles": "configured",
    }
    mode = aliases.get(mode, mode)
    if mode not in TEAM_MODES:
        raise ValueError(f"Unsupported team mode: {mode}")
    return mode


def normalize_agent_overrides(value: Any) -> dict[str, str]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise ValueError("Agent overrides must be a role-to-AgentRef object.")
    result: dict[str, str] = {}
    unexpected = sorted(str(key) for key in set(value) - set(TEAM_ROLES))
    if unexpected:
        raise ValueError("Unknown agent override roles: " + ", ".join(unexpected))
    for role in TEAM_ROLES:
        raw = str(value.get(role) or "").strip()
        if raw:
            result[role] = str(AgentRef.parse(raw))
    return result


def _capabilities(item: Mapping[str, Any]) -> set[str]:
    raw = item.get("capabilities")
    if not isinstance(raw, list):
        return set()
    return {
        str(value or "").strip().lower() for value in raw if str(value or "").strip()
    }


def _role_compatible(item: Mapping[str, Any], role: str) -> tuple[bool, str]:
    capabilities = _capabilities(item)
    if "workspace.read" not in capabilities:
        return False, "No declara lectura del workspace."
    can_write = role == "implementer"
    effect_mode = str(item.get("effect_mode") or "read-only").strip().lower()
    if can_write and (
        "workspace.write" not in capabilities or effect_mode != "workspace-write"
    ):
        return (
            False,
            "La ejecución necesita escritura y este agente es de solo lectura.",
        )
    declared_roles = {value for value in capabilities if value.startswith("role.")}
    if declared_roles and not declared_roles.intersection(_ROLE_CAPABILITIES[role]):
        return False, f"El agente no declara la función {role}."
    return True, ""


def _health(item: Mapping[str, Any]) -> tuple[bool, str]:
    reference = str(item.get("ref") or "")
    digest = str(item.get("digest") or "")
    try:
        AgentRef.parse(reference)
    except AgentContractError:
        return False, "La identidad AgentRef no es válida."
    if (
        len(digest) != 71
        or not digest.startswith("sha256:")
        or any(character not in "0123456789abcdef" for character in digest[7:])
    ):
        return False, "El digest del manifest no es válido."
    if item.get("revoked") is True:
        return False, "La versión está revocada."
    if item.get("enabled") is False:
        return False, "La versión está deshabilitada."
    if item.get("ready") is False or str(item.get("state") or "") == "unavailable":
        reason = str(item.get("reason") or "no está disponible")
        return False, f"El agente no está listo: {reason}."
    return True, ""


def _version_rank(value: str) -> tuple[int, int, int, int]:
    match = _SEMVER.fullmatch(str(value or "").strip())
    if not match:
        return (0, 0, 0, 0)
    return (
        1,
        int(match.group("major") or 0),
        int(match.group("minor") or 0),
        int(match.group("patch") or 0),
    )


def _preferred_references(plan: Mapping[str, Any]) -> set[str]:
    profiles = plan.get("profiles")
    if not isinstance(profiles, list):
        return set()
    return {
        str(profile.get("agent_ref") or "").strip()
        for raw in profiles
        if isinstance(raw, Mapping)
        for profile in (raw,)
        if str(profile.get("agent_ref") or "").strip()
    }


def _candidate_key(
    item: Mapping[str, Any], *, role: str, preferred: set[str]
) -> tuple[Any, ...]:
    reference = str(item.get("ref") or "")
    capabilities = _capabilities(item)
    role_specific = bool(capabilities.intersection(_ROLE_CAPABILITIES[role]))
    least_privilege = (
        role == "implementer"
        or str(item.get("effect_mode") or "read-only") == "read-only"
    )
    version = _version_rank(str(item.get("version") or ""))
    # Negative numeric values let a normal ascending sort pick the strongest
    # candidate while the exact AgentRef remains the stable final tiebreaker.
    return (
        -int(reference in preferred),
        -int(role_specific),
        -int(least_privilege),
        *(-part for part in version),
        reference,
    )


@dataclass(frozen=True)
class RoleTeamResolution:
    role: str
    selection: str
    message: str
    agent_ref: str = ""
    agent_manifest_digest: str = ""
    profile_name: str = ""
    considered: int = 0
    rejected: tuple[Mapping[str, str], ...] = ()

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "selection": self.selection,
            "message": self.message,
            "considered": self.considered,
            "rejected": [dict(value) for value in self.rejected],
        }
        if self.agent_ref:
            result["agent_ref"] = self.agent_ref
            result["agent_manifest_digest"] = self.agent_manifest_digest
        if self.profile_name:
            result["profile_name"] = self.profile_name
        return result


@dataclass(frozen=True)
class TeamResolution:
    mode: str
    roles: Mapping[str, RoleTeamResolution]
    plans: Mapping[str, Mapping[str, Any]]

    def to_dict(self) -> dict[str, Any]:
        return {
            "contract": TEAM_RESOLUTION_CONTRACT,
            "version": TEAM_RESOLUTION_VERSION,
            "mode": self.mode,
            "roles": {role: self.roles[role].to_dict() for role in TEAM_ROLES},
        }


def _external_profile(
    *, role: str, plan: Mapping[str, Any], item: Mapping[str, Any]
) -> dict[str, Any]:
    raw_profiles = plan.get("profiles")
    fallback = (
        copy.deepcopy(raw_profiles[0])
        if isinstance(raw_profiles, list)
        and raw_profiles
        and isinstance(raw_profiles[0], Mapping)
        else {}
    )
    reference = str(item.get("ref") or "")
    transport = str(item.get("transport") or "provider")
    fallback.update(
        {
            "name": f"auto-agent-{role}",
            "provider": f"external-{transport}",
            "model": "",
            "reasoning_effort": "",
            "agent": "",
            "effort": "",
            "runner": "",
            "agent_ref": reference,
            "agent_manifest_digest": str(item.get("digest") or ""),
            "description": (f"Selección automática para {role}: {reference}."),
        }
    )
    return fallback


def resolve_team(
    role_plans: Mapping[str, Mapping[str, Any]],
    catalog: Mapping[str, Any],
    *,
    mode: str = "configured",
    overrides: Mapping[str, str] | None = None,
) -> TeamResolution:
    selected_mode = normalize_team_mode(mode)
    selected_overrides = normalize_agent_overrides(overrides)
    raw_agents = catalog.get("agents")
    agents = (
        [item for item in raw_agents if isinstance(item, Mapping)]
        if isinstance(raw_agents, list)
        else []
    )
    by_reference: dict[str, Mapping[str, Any]] = {}
    for item in agents:
        reference = str(item.get("ref") or "")
        if not reference:
            continue
        previous = by_reference.get(reference)
        if previous is not None and previous.get("digest") != item.get("digest"):
            raise TeamResolutionError(
                f"El catálogo contiene dos digests para {reference}.",
                code="team_catalog_identity_conflict",
            )
        by_reference[reference] = item
    plans = copy.deepcopy(dict(role_plans))
    resolutions: dict[str, RoleTeamResolution] = {}

    for role in TEAM_ROLES:
        plan = plans.get(role)
        if not isinstance(plan, dict):
            raise ValueError(f"Missing role plan: {role}")
        override = selected_overrides.get(role, "")
        preferred = _preferred_references(plan)
        rejected: list[Mapping[str, str]] = []
        compatible: list[Mapping[str, Any]] = []
        for item in agents:
            reference = str(item.get("ref") or "")
            healthy, health_reason = _health(item)
            if not healthy:
                rejected.append({"ref": reference, "reason": health_reason})
                continue
            matches, match_reason = _role_compatible(item, role)
            if not matches:
                rejected.append({"ref": reference, "reason": match_reason})
                continue
            compatible.append(item)

        selected: Mapping[str, Any] | None = None
        selection = "configured-profile"
        if override:
            selected = by_reference.get(override)
            if selected is None:
                raise TeamResolutionError(
                    f"El override de {role} apunta a {override}, pero esa versión no está registrada.",
                    code="team_override_not_found",
                    role=role,
                )
            healthy, reason = _health(selected)
            compatible_override, compatibility_reason = _role_compatible(selected, role)
            if not healthy or not compatible_override:
                raise TeamResolutionError(
                    f"El override de {role} ({override}) no se puede usar: "
                    f"{reason or compatibility_reason}",
                    code="team_override_incompatible",
                    role=role,
                )
            selection = "explicit-override"
        elif selected_mode == "automatic" and compatible:
            selected = sorted(
                compatible,
                key=lambda item: _candidate_key(item, role=role, preferred=preferred),
            )[0]
            selection = "automatic-agent"

        if selected is not None:
            reference = str(selected.get("ref") or "")
            digest = str(selected.get("digest") or "")
            plan["profiles"] = [_external_profile(role=role, plan=plan, item=selected)]
            plan["strategy"] = "first-success"
            plan["min_successes"] = 1
            plan["min_approvals"] = 1
            message = (
                f"Override explícito: {reference}."
                if selection == "explicit-override"
                else f"Baldr eligió {reference} por compatibilidad, salud y preferencia."
            )
            resolutions[role] = RoleTeamResolution(
                role=role,
                selection=selection,
                message=message,
                agent_ref=reference,
                agent_manifest_digest=digest,
                considered=len(agents),
                rejected=tuple(rejected[:20]),
            )
            continue

        raw_profiles = plan.get("profiles")
        first_profile = (
            raw_profiles[0]
            if isinstance(raw_profiles, list)
            and raw_profiles
            and isinstance(raw_profiles[0], Mapping)
            else {}
        )
        profile_name = str(first_profile.get("name") or "configured")
        message = (
            "Se conserva la configuración elegida por el usuario."
            if selected_mode == "configured"
            else "No hay un agente externo compatible y listo; se usa la configuración normal."
        )
        resolutions[role] = RoleTeamResolution(
            role=role,
            selection="configured-profile",
            message=message,
            profile_name=profile_name,
            considered=len(agents),
            rejected=tuple(rejected[:20]),
        )

    return TeamResolution(mode=selected_mode, roles=resolutions, plans=plans)

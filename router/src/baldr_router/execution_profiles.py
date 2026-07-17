from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

from .config import AppConfig, ExecutionProfileConfig, RoleConfig

VALID_ROLE_STRATEGIES = {"first-success", "all"}


@dataclass(frozen=True)
class ResolvedExecutionProfile:
    name: str
    provider: str
    model: str
    reasoning_effort: str
    agent: str
    effort: str
    runner: str
    session_scope: str
    can_write: bool
    sandbox: str
    description: str
    agent_ref: str
    agent_manifest_digest: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _inline_profile(role: RoleConfig) -> ExecutionProfileConfig:
    return ExecutionProfileConfig(
        provider=role.provider,
        model=role.model,
        reasoning_effort=role.reasoning_effort,
        agent=role.agent,
        effort=role.effort,
        runner=role.runner,
        session_scope=role.session_scope,
        enabled=True,
        description=role.description,
        agent_ref=role.agent_ref,
        agent_manifest_digest=role.agent_manifest_digest,
    )


def _provider_fallbacks(
    cfg: AppConfig, provider: str
) -> tuple[str, str, str, str, str, str]:
    normalized = provider.strip().lower().replace("_", "-")
    if normalized in {"codex", "openai-codex"}:
        return (
            cfg.codex.model,
            cfg.codex.reasoning_effort,
            "",
            "",
            cfg.codex.runner,
            cfg.codex.session_scope,
        )
    if normalized in {"kiro", "kiro-cli"}:
        return (
            "",
            "",
            cfg.kiro_cli.default_agent,
            cfg.kiro_cli.default_effort,
            "cli",
            "workflow",
        )
    return ("", "", "", "", "", "workflow")


def resolve_role_profiles(
    cfg: AppConfig,
    role_name: str,
    role: RoleConfig,
    *,
    provider_override: str | None = None,
) -> list[ResolvedExecutionProfile]:
    """Resolve one or many execution profiles for a workflow phase.

    A role can reference a single shared profile, or an arbitrary list. The
    list length is independent for architect, implementer, and reviewer, which
    gives Baldr the requested 1-for-all or n/m/l-per-phase abstraction.
    """

    raw_profiles: list[tuple[str, ExecutionProfileConfig]] = []
    if role.profiles:
        for name in role.profiles:
            profile = cfg.execution_profiles.get(name)
            if profile is None or not profile.enabled:
                continue
            raw_profiles.append((name, profile))
    else:
        raw_profiles.append((f"{role_name}-inline", _inline_profile(role)))

    resolved: list[ResolvedExecutionProfile] = []
    for name, profile in raw_profiles:
        provider = provider_override or profile.provider or role.provider or cfg.router.default_provider
        (
            fallback_model,
            fallback_reasoning,
            fallback_agent,
            fallback_effort,
            fallback_runner,
            fallback_scope,
        ) = _provider_fallbacks(cfg, provider)
        resolved.append(
            ResolvedExecutionProfile(
                name=name,
                provider=provider,
                model=profile.model or fallback_model,
                reasoning_effort=profile.reasoning_effort or fallback_reasoning,
                agent=profile.agent or fallback_agent,
                effort=profile.effort or fallback_effort,
                runner=profile.runner or fallback_runner,
                session_scope=profile.session_scope or fallback_scope,
                can_write=bool(role.can_write),
                sandbox=role.sandbox,
                description=profile.description or role.description,
                agent_ref=profile.agent_ref,
                agent_manifest_digest=profile.agent_manifest_digest,
            )
        )

    if not resolved:
        # A missing/disabled named profile must fail loudly rather than falling
        # back to a surprising provider.
        raise ValueError(
            f"Role {role_name!r} has no enabled execution profiles. "
            "Check [roles] and [execution_profiles] in config.toml."
        )
    return resolved


def role_execution_plan(
    cfg: AppConfig,
    role_name: str,
    role: RoleConfig,
    *,
    provider_override: str | None = None,
) -> dict[str, Any]:
    strategy = role.strategy if role.strategy in VALID_ROLE_STRATEGIES else "first-success"
    profiles = resolve_role_profiles(
        cfg, role_name, role, provider_override=provider_override
    )
    if strategy == "all" and role.can_write and len(profiles) > 1:
        raise ValueError(
            f"Role {role_name!r} is write-enabled and cannot use strategy='all' "
            "with multiple profiles. Use first-success to avoid concurrent writers."
        )
    return {
        "role": role_name,
        "strategy": strategy,
        "min_successes": max(1, min(int(role.min_successes), len(profiles))),
        "resolution": role.resolution or (
            "primary-with-advisors" if role_name == "architect" else
            "any-blocker" if role_name == "reviewer" else
            "first-success"
        ),
        "min_approvals": max(1, min(int(role.min_approvals), len(profiles))),
        "profiles": [profile.to_dict() for profile in profiles],
        "can_write": role.can_write,
        "sandbox": role.sandbox,
    }

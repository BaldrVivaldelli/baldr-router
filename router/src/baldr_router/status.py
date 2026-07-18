from __future__ import annotations

from dataclasses import asdict
from pathlib import Path
from typing import Any

from .codex import codex_found, codex_login_status, codex_version, npx_found
from .agent_gateway import (
    configured_agent_bindings_status,
    external_agent_catalog_status,
)
from .codex_config import context7_mcp_config_status
from .config import config_path, load_config, secrets_path
from .context7 import cache_status
from .extensions import extension_status
from .discovery.environment_probe import environment_probe
from .discovery.workspace_profile import workspace_profile
from .durability.recovery import recover_stale_runs
from .durability.identity import workspace_identity
from .durability.store import DurableStore
from .execution_profiles import role_execution_plan
from .evidence import latest_evidence
from .validation.lifecycle import ensure_quick_verification
from .platforming import environment_report
from .provider_registry import provider_status
from .release_policy import release_policy_status
from .secrets import read_context7_api_key
from .telemetry import runs_jsonl_path, telemetry_stats
from .workspace_policy import inspect_workspace


def _workspace_status(workspace_root: str, store: DurableStore) -> dict[str, Any]:
    identity = workspace_identity(Path(workspace_root))
    row = store.connect().execute(
        "SELECT safety_mode FROM workspace_preferences WHERE workspace_id = ?",
        (identity["workspace_id"],),
    ).fetchone()
    safety_mode = str(row["safety_mode"] if row is not None else "current").lower()
    return inspect_workspace(
        workspace_root,
        access="read",
        protected_non_git=safety_mode in {"auto", "automatic"},
    )


def doctor(workspace_root: str | None = None) -> dict[str, Any]:
    cfg = load_config()
    providers = provider_status()
    agents = external_agent_catalog_status()
    implemented = providers.get("implemented_providers", [])
    store = DurableStore()
    recovery = (
        recover_stale_runs(store)
        if cfg.durability.enabled and cfg.durability.recovery_on_start
        else {"ok": True, "count": 0, "runs": []}
    )
    resolved_roles: dict[str, Any] = {}
    for role_name, role in cfg.roles.items():
        try:
            resolved_roles[role_name] = role_execution_plan(cfg, role_name, role)
        except Exception as exc:
            resolved_roles[role_name] = {"ok": False, "reason": str(exc)}
    binding_status = configured_agent_bindings_status(resolved_roles)
    agents = {**agents, "configured_bindings": binding_status}
    client_id = __import__("os").environ.get("BALDR_CLIENT_ID") or None
    verification: dict[str, Any]
    if (
        cfg.verification.enabled
        and cfg.verification.run_on_client_doctor
        and client_id
        and not __import__("os").environ.get("BALDR_VERIFY_IN_PROGRESS")
    ):
        verification = ensure_quick_verification(
            workspace_root=workspace_root,
            client_id=client_id,
        )
    else:
        verification = latest_evidence(kind="lifecycle")

    result: dict[str, Any] = {
        "ok": True,
        "environment": environment_report(),
        "environment_probe": environment_probe(client_id=client_id),
        "release_policy": release_policy_status(),
        "config_path": str(config_path()),
        "config_exists": config_path().exists(),
        "secrets_path": str(secrets_path()),
        "router": {
            "default_provider": cfg.router.default_provider,
            "default_workflow": cfg.router.default_workflow,
            "implemented_providers": implemented,
            "agents": agents,
            "roles": {name: asdict(role) for name, role in cfg.roles.items()},
            "execution_profiles": {
                name: asdict(profile) for name, profile in cfg.execution_profiles.items()
            },
            "resolved_role_plans": resolved_roles,
            "workflows": {
                name: asdict(workflow) for name, workflow in cfg.workflows.items()
            },
            "safety": asdict(cfg.safety),
            "workspace_policy": asdict(cfg.workspace),
            "durability": {
                **asdict(cfg.durability),
                "database_path": str(store.path),
                "schema": store.schema_status(),
                "nonterminal_runs": store.list_nonterminal_runs(),
                "recovery": recovery,
            },
        },
        "providers": providers,
        "agents": agents,
        "extensions": extension_status(),
        "codex": {
            "found": bool(codex_found()),
            "path": codex_found(),
            "configured_model": cfg.codex.model,
            "reasoning_effort": cfg.codex.reasoning_effort,
            "sandbox": cfg.codex.sandbox,
            "approval_policy": cfg.codex.approval_policy,
            "runner": cfg.codex.runner,
            "session_scope": cfg.codex.session_scope,
        },
        "context7": {
            "enabled": cfg.context7.enabled,
            "mode": cfg.context7.mode,
            "api_key_source": cfg.context7.api_key_source,
            "api_key_available": bool(
                read_context7_api_key(cfg.context7.api_key_source)
            ),
            "npx_found": bool(npx_found()),
            "npx_path": npx_found(),
            "codex_mcp_config": context7_mcp_config_status(),
            "cache_ttl_hours": cfg.context7.cache_ttl_hours,
            "inject_docs": cfg.context7.inject_docs,
            "cache": cache_status(),
        },
        "telemetry": {
            "enabled": cfg.telemetry.enabled,
            "runs_path": str(runs_jsonl_path()),
            "stats": telemetry_stats(),
        },
        "verification": verification,
    }

    if codex_found():
        result["codex"]["version"] = codex_version()
        result["codex"]["login_status"] = codex_login_status()

    if not agents["ok"] or not binding_status["ok"]:
        result["ok"] = False
        result.setdefault(
            "next_step",
            "Fix the configured external agent references or their registry manifests.",
        )

    if cfg.codex.runner == "sdk":
        try:
            import openai_codex  # type: ignore  # noqa: F401

            result["codex"]["python_sdk_available"] = True
        except Exception:
            result["codex"]["python_sdk_available"] = False
            result["codex"]["sdk_next_step"] = (
                "Install optional SDK with `pip install openai-codex`."
            )

    default_adapter = (providers.get("providers") or {}).get(
        cfg.router.default_provider
    )
    if default_adapter is None:
        result["ok"] = False
        result["next_step"] = (
            f"Configure an implemented default provider. Available: {', '.join(implemented)}."
        )
    elif (
        cfg.router.default_provider == "codex"
        and not default_adapter.get("found")
        and cfg.codex.runner != "sdk"
    ):
        result["ok"] = False
        result["next_step"] = "Install Codex CLI and run `codex login`."
    elif cfg.router.default_provider == "kiro-cli" and not default_adapter.get("ok"):
        result["ok"] = False
        result["next_step"] = (
            default_adapter.get("reason") or "Configure the kiro-cli provider."
        )

    verification_ok = verification.get("ok")
    verification_status = verification.get("status")
    if verification_ok is False and verification_status not in {"disabled", "in_progress"}:
        result["ok"] = False
        result.setdefault(
            "next_step",
            "Baldr lifecycle verification failed. Inspect the latest evidence bundle with `baldr-router evidence --latest`.",
        )

    if workspace_root:
        workspace = _workspace_status(workspace_root, store)
        result["workspace"] = workspace
        if workspace.get("ok") and cfg.probe.enabled:
            result["workspace_profile"] = workspace_profile(workspace_root)
        if not workspace.get("ok"):
            result["ok"] = False
            result.setdefault("next_step", workspace.get("reason"))

    return result

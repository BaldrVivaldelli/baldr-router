from __future__ import annotations

from typing import Any

from mcp.server.fastmcp import FastMCP

from .codex_config import install_context7_mcp_config, remove_context7_mcp_config
from .config import load_config
from .context7 import cache_status, clear_cache, lookup_docs_for_library
from .context7_setup import (
    context7_onboarding_plan,
    disable_context7,
    enable_context7_env_source,
)
from .extensions import extension_status, load_installed_extensions
from .facade_contract import render_facade_prompt
from .facade_runtime import run_facade
from .discovery.environment_probe import environment_probe
from .provider_registry import provider_status
from .secrets import read_context7_api_key
from .status import doctor
from .tasks import delegate_task_impl, review_current_diff_impl
from .telemetry import recent_runs, telemetry_stats
from .workflows import (
    list_roles,
    list_workflows,
    run_workflow_impl,
    set_role_provider,
    workflow_status,
)

mcp = FastMCP("baldr-router")


@mcp.prompt(
    name="setup",
    title="Baldr Setup",
    description="Configure Baldr providers, roles, optional Context7, and runtime health.",
)
def baldr_setup_prompt(workspace_root: str = "") -> str:
    """Shared setup intent used by every client facade."""
    return render_facade_prompt("setup", workspace_root=workspace_root or None)


@mcp.prompt(
    name="status",
    title="Baldr Status",
    description="Inspect runtime health, providers, roles, Context7, and recent runs.",
)
def baldr_status_prompt(workspace_root: str = "") -> str:
    """Shared status intent used by every client facade."""
    return render_facade_prompt("status", workspace_root=workspace_root or None)


@mcp.prompt(
    name="run",
    title="Baldr Run",
    description="Run the architect → implementer → reviewer workflow for a task.",
)
def baldr_run_prompt(task: str, workspace_root: str = "") -> str:
    """Shared run intent used by every client facade."""
    return render_facade_prompt("run", workspace_root=workspace_root or None, task=task)


@mcp.tool()
def router_environment_report() -> dict[str, Any]:
    """Return runtime/platform information, including WSL/path-normalization hints."""
    return environment_probe()


@mcp.tool()
def router_doctor(workspace_root: str | None = None) -> dict[str, Any]:
    """Check core config, providers, Context7, telemetry, extensions, and an optional workspace path."""
    return doctor(workspace_root)


@mcp.tool()
def router_extension_status() -> dict[str, Any]:
    """Show installed client-adapter extensions loaded into this MCP process."""
    return extension_status()


@mcp.tool()
def context7_onboarding() -> dict[str, Any]:
    """Return a non-secret Context7 setup decision tree for any MCP client."""
    return context7_onboarding_plan()


@mcp.tool()
def context7_enable_env_source(
    mode: str = "hybrid",
    env_name: str = "CONTEXT7_API_KEY",
    install_codex_mcp: bool = True,
    force_codex_mcp: bool = False,
) -> dict[str, Any]:
    """Enable Context7 using an existing env var. Does not ask for or store the API key."""
    return enable_context7_env_source(
        mode=mode,
        env_name=env_name,
        install_codex_mcp=install_codex_mcp,
        force_codex_mcp=force_codex_mcp,
    )


@mcp.tool()
def context7_disable(remove_codex_mcp: bool = False) -> dict[str, Any]:
    """Disable Context7 in router config, optionally removing the managed Codex MCP block."""
    return disable_context7(remove_codex_mcp=remove_codex_mcp)


@mcp.tool()
def context7_status() -> dict[str, Any]:
    """Show Context7 configuration status without exposing secrets."""
    cfg = load_config()
    return {
        "enabled": cfg.context7.enabled,
        "mode": cfg.context7.mode,
        "api_key_source": cfg.context7.api_key_source,
        "api_key_available": bool(read_context7_api_key(cfg.context7.api_key_source)),
        "install_codex_mcp": cfg.context7.install_codex_mcp,
        "cache_ttl_hours": cfg.context7.cache_ttl_hours,
        "inject_docs": cfg.context7.inject_docs,
        "cache": cache_status(),
    }


@mcp.tool()
def context7_lookup_docs(
    library: str, query: str, fast: bool | None = None
) -> dict[str, Any]:
    """Resolve a library through Context7, fetch docs, and use the router cache."""
    return lookup_docs_for_library(library, query, fast=fast)


@mcp.tool()
def context7_cache_status() -> dict[str, Any]:
    """Show Context7 cache location and size."""
    return cache_status()


@mcp.tool()
def context7_cache_clear(older_than_hours: int | None = None) -> dict[str, Any]:
    """Clear Context7 cache. Pass older_than_hours to keep newer entries."""
    return clear_cache(older_than_hours=older_than_hours)


@mcp.tool()
def install_codex_context7_mcp(force: bool = False) -> dict[str, Any]:
    """Install Context7 MCP config into ~/.codex/config.toml without writing the API key there."""
    return install_context7_mcp_config(force=force)


@mcp.tool()
def remove_codex_context7_mcp() -> dict[str, Any]:
    """Remove the managed Context7 MCP block from ~/.codex/config.toml."""
    return remove_context7_mcp_config()


@mcp.tool()
def router_recent_runs(limit: int = 20) -> dict[str, Any]:
    """Return recent provider runs captured by baldr-router telemetry."""
    return recent_runs(limit=limit)


@mcp.tool()
def router_stats() -> dict[str, Any]:
    """Return aggregated provider telemetry."""
    return telemetry_stats()


@mcp.tool()
def router_provider_status() -> dict[str, Any]:
    """Return provider availability, capabilities, and configuration."""
    return provider_status()


@mcp.tool()
def router_workflow_status() -> dict[str, Any]:
    """Return configured roles, workflows, providers, and safety settings."""
    return workflow_status()


@mcp.tool()
def router_list_roles() -> dict[str, Any]:
    """List configured multi-agent roles such as architect, implementer, and reviewer."""
    return list_roles()


@mcp.tool()
def router_list_workflows() -> dict[str, Any]:
    """List available baldr-router workflows."""
    return list_workflows()


@mcp.tool()
def router_set_role_provider(
    role: str, provider: str, agent: str | None = None, effort: str | None = None
) -> dict[str, Any]:
    """Set which provider backs a role, e.g. architect=kiro-cli and implementer=codex."""
    return set_role_provider(role, provider, agent=agent, effort=effort)


@mcp.tool()
def run_workflow(
    workspace_root: str,
    task: str,
    workflow: str | None = None,
    extra_context: str = "",
    architect_provider: str | None = None,
    implementer_provider: str | None = None,
    reviewer_provider: str | None = None,
    max_rounds: int | None = None,
    context7_libraries: list[str] | None = None,
    dry_run: bool = False,
    idempotency_key: str | None = None,
    resume_run_id: str | None = None,
    reconciliation_action: str | None = None,
    cancel: bool = False,
    cancel_reason: str = "Cancellation requested by client.",
    team_mode: str | None = None,
    agent_overrides: dict[str, str] | None = None,
    client_name: str = "generic-mcp",
) -> dict[str, Any]:
    """Run or resume the durable Baldr-led workflow.

    Fresh executions of the frozen default workflow are materialized as durable
    Work Items so every MCP client shares the same task list as the VS Code
    console. Low-level resume/cancel/idempotency calls retain the historical
    workflow API and do not create a second item.
    """
    if (
        not dry_run
        and not idempotency_key
        and not resume_run_id
        and not reconciliation_action
        and not cancel
        and (workflow is None or workflow == "architect-implement-review")
    ):
        return run_facade(
            workspace_root=workspace_root,
            task=task,
            extra_context=extra_context,
            architect_provider=architect_provider,
            implementer_provider=implementer_provider,
            reviewer_provider=reviewer_provider,
            max_rounds=max_rounds,
            context7_libraries=context7_libraries,
            team_mode=team_mode,
            agent_overrides=agent_overrides,
            client_name=client_name,
        )
    return run_workflow_impl(
        workspace_root=workspace_root,
        task=task,
        workflow=workflow,
        extra_context=extra_context,
        architect_provider=architect_provider,
        implementer_provider=implementer_provider,
        reviewer_provider=reviewer_provider,
        max_rounds=max_rounds,
        context7_libraries=context7_libraries,
        dry_run=dry_run,
        idempotency_key=idempotency_key,
        resume_run_id=resume_run_id,
        reconciliation_action=reconciliation_action,
        cancel=cancel,
        cancel_reason=cancel_reason,
        team_mode=team_mode,
        agent_overrides=agent_overrides,
        client_name=client_name,
    )


@mcp.tool()
def run_architect_implement_review(
    workspace_root: str,
    task: str,
    extra_context: str = "",
    architect_provider: str | None = None,
    implementer_provider: str | None = None,
    reviewer_provider: str | None = None,
    max_rounds: int | None = None,
    context7_libraries: list[str] | None = None,
    dry_run: bool = False,
    idempotency_key: str | None = None,
    resume_run_id: str | None = None,
    reconciliation_action: str | None = None,
    cancel: bool = False,
    cancel_reason: str = "Cancellation requested by client.",
    team_mode: str | None = None,
    agent_overrides: dict[str, str] | None = None,
    client_name: str = "generic-mcp",
) -> dict[str, Any]:
    """Convenience wrapper for the durable architect -> implementer -> reviewer workflow."""
    if (
        not dry_run
        and not idempotency_key
        and not resume_run_id
        and not reconciliation_action
        and not cancel
    ):
        return run_facade(
            workspace_root=workspace_root,
            task=task,
            extra_context=extra_context,
            architect_provider=architect_provider,
            implementer_provider=implementer_provider,
            reviewer_provider=reviewer_provider,
            max_rounds=max_rounds,
            context7_libraries=context7_libraries,
            team_mode=team_mode,
            agent_overrides=agent_overrides,
            client_name=client_name,
        )
    return run_workflow_impl(
        workspace_root=workspace_root,
        task=task,
        workflow="architect-implement-review",
        extra_context=extra_context,
        architect_provider=architect_provider,
        implementer_provider=implementer_provider,
        reviewer_provider=reviewer_provider,
        max_rounds=max_rounds,
        context7_libraries=context7_libraries,
        dry_run=dry_run,
        idempotency_key=idempotency_key,
        resume_run_id=resume_run_id,
        reconciliation_action=reconciliation_action,
        cancel=cancel,
        cancel_reason=cancel_reason,
        team_mode=team_mode,
        agent_overrides=agent_overrides,
        client_name=client_name,
    )


@mcp.tool()
def delegate_task(
    workspace_root: str,
    task: str,
    acceptance_criteria: str = "",
    relevant_files: list[str] | None = None,
    extra_context: str = "",
    context7_libraries: list[str] | None = None,
    provider: str | None = None,
) -> dict[str, Any]:
    """Delegate one implementation task through the configured implementer provider."""
    return delegate_task_impl(
        workspace_root=workspace_root,
        task=task,
        acceptance_criteria=acceptance_criteria,
        relevant_files=relevant_files,
        extra_context=extra_context,
        context7_libraries=context7_libraries,
        provider=provider,
    )


@mcp.tool()
def review_current_diff(
    workspace_root: str,
    focus: str = "correctness, tests, regressions, security, and task compliance",
    extra_context: str = "",
    context7_libraries: list[str] | None = None,
    provider: str | None = None,
) -> dict[str, Any]:
    """Ask the configured reviewer provider to review the current git diff."""
    return review_current_diff_impl(
        workspace_root=workspace_root,
        focus=focus,
        extra_context=extra_context,
        context7_libraries=context7_libraries,
        provider=provider,
    )


def run_mcp() -> None:
    load_installed_extensions(mcp)
    mcp.run()

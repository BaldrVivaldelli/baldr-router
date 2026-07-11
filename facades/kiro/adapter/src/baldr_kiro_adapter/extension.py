from __future__ import annotations

from typing import Any
import os

from baldr_router.tasks import delegate_task_impl
from baldr_router.workspace_policy import trust_workspace
from baldr_router.discovery.workspace_profile import workspace_profile
from baldr_router.validation.lifecycle import ensure_quick_verification
from baldr_router.qualification.receipts import record_client_receipt

from . import __version__
from .hooks import (
    install_workspace_hooks,
    uninstall_workspace_hooks,
    workspace_hooks_status,
)


def kiro_workspace_status(workspace_root: str) -> dict[str, Any]:
    """Return the managed Kiro hook status for one workspace."""
    return workspace_hooks_status(workspace_root)


def kiro_install_workspace(
    workspace_root: str,
    include_context7_prompt_hook: bool = False,
    git_exclude_generated: bool = True,
    force: bool = False,
    backup_on_update: bool = True,
) -> dict[str, Any]:
    """Create/update generated Kiro hooks idempotently without overwriting user edits by default."""
    trust = trust_workspace(workspace_root, force=force)
    if not trust.get("ok"):
        return {
            "ok": False,
            "action": "workspace_not_trusted",
            "workspace_trust": trust,
            "reason": trust.get("reason"),
        }
    result = install_workspace_hooks(
        workspace_root,
        include_context7_prompt_hook=include_context7_prompt_hook,
        git_exclude_generated=git_exclude_generated,
        force=force,
        backup_on_update=backup_on_update,
    )
    result["workspace_trust"] = trust
    result["workspace_profile"] = workspace_profile(workspace_root)
    result["client_receipt"] = record_client_receipt(
        client="kiro-power",
        client_version=__version__,
        facts={
            "client": "kiro-power",
            "extension_host": os.environ.get("BALDR_CLIENT_HOST_OS", "windows"),
            "router_runtime": os.environ.get("BALDR_RUNTIME_TRANSPORT", "wsl"),
            "runtime_source": os.environ.get("BALDR_RUNTIME_SOURCE", ""),
            "wsl_distro": os.environ.get("BALDR_RUNTIME_WSL_DISTRO", os.environ.get("WSL_DISTRO_NAME", "")),
            "workspace_hooks_installed": bool(result.get("ok")),
            "workspace_root_count": 1,
        },
    )
    if os.environ.get("BALDR_CLIENT_ID"):
        result["verification"] = ensure_quick_verification(
            workspace_root=workspace_root,
            client_id="kiro-power",
        )
    else:
        result["verification"] = {
            "ok": True,
            "status": "deferred",
            "reason": "Automatic verification runs when the adapter is launched by a declared client facade.",
        }
    return result


def kiro_uninstall_workspace(
    workspace_root: str,
    force: bool = False,
    backup_on_remove: bool = False,
) -> dict[str, Any]:
    """Remove managed Kiro hooks without deleting user edits by default."""
    return uninstall_workspace_hooks(
        workspace_root,
        force=force,
        backup_on_remove=backup_on_remove,
    )


def delegate_spec_task(
    workspace_root: str,
    task_title: str,
    task_details: str,
    acceptance_criteria: str = "",
    relevant_files: list[str] | None = None,
    extra_context: str = "",
    context7_libraries: list[str] | None = None,
    provider: str | None = None,
) -> dict[str, Any]:
    """Deprecated Kiro compatibility alias. Prefer the core `delegate_task` tool."""
    task = f"{task_title.strip()}\n\n{task_details.strip()}".strip()
    result = delegate_task_impl(
        workspace_root=workspace_root,
        task=task,
        acceptance_criteria=acceptance_criteria,
        relevant_files=relevant_files,
        extra_context=extra_context,
        context7_libraries=context7_libraries,
        provider=provider,
    )
    result.setdefault(
        "deprecation",
        "delegate_spec_task is a Kiro adapter alias; prefer delegate_task.",
    )
    return result


def register(mcp: Any) -> dict[str, Any]:
    """Register Kiro-only tools into a running baldr-router MCP server."""
    tools = [
        kiro_workspace_status,
        kiro_install_workspace,
        kiro_uninstall_workspace,
        delegate_spec_task,
    ]
    for tool in tools:
        mcp.tool()(tool)
    return {
        "adapter": "kiro",
        "version": __version__,
        "tools": [tool.__name__ for tool in tools],
    }

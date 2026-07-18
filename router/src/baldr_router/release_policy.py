from __future__ import annotations

from typing import Any

FEATURE_FREEZE_ACTIVE = True
FEATURE_FREEZE_LINE = "0.20"

FROZEN_CORE_MCP_TOOLS = (
    "context7_cache_clear",
    "context7_cache_status",
    "context7_disable",
    "context7_enable_env_source",
    "context7_lookup_docs",
    "context7_onboarding",
    "context7_status",
    "delegate_task",
    "install_codex_context7_mcp",
    "remove_codex_context7_mcp",
    "review_current_diff",
    "router_doctor",
    "router_environment_report",
    "router_extension_status",
    "router_list_roles",
    "router_list_workflows",
    "router_provider_status",
    "router_recent_runs",
    "router_set_role_provider",
    "router_stats",
    "router_workflow_status",
    "run_architect_implement_review",
    "run_workflow",
)

FROZEN_CORE_MCP_PROMPTS = ("run", "setup", "status")
FROZEN_FACADE_INTENTS = ("setup", "status", "run")
FROZEN_BUILTIN_PROVIDERS = ("codex", "kiro-cli")
FROZEN_ROLES = ("architect", "implementer", "reviewer")
FROZEN_WORKFLOWS = ("architect-implement-review",)


def release_policy_status() -> dict[str, Any]:
    return {
        "feature_freeze_active": FEATURE_FREEZE_ACTIVE,
        "feature_freeze_line": FEATURE_FREEZE_LINE,
        "frozen_core_tools": list(FROZEN_CORE_MCP_TOOLS),
        "frozen_core_prompts": list(FROZEN_CORE_MCP_PROMPTS),
        "frozen_facade_intents": list(FROZEN_FACADE_INTENTS),
        "frozen_builtin_providers": list(FROZEN_BUILTIN_PROVIDERS),
        "frozen_roles": list(FROZEN_ROLES),
        "frozen_workflows": list(FROZEN_WORKFLOWS),
        "policy_document": "FEATURE_FREEZE.md",
    }

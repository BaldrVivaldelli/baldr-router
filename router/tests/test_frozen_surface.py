from __future__ import annotations

import asyncio

from baldr_router.config import AppConfig
from baldr_router.facade import FACADE_INTENTS
from baldr_router.provider_registry import get_provider_registry
from baldr_router.release_policy import (
    FEATURE_FREEZE_ACTIVE,
    FROZEN_BUILTIN_PROVIDERS,
    FROZEN_CORE_MCP_PROMPTS,
    FROZEN_CORE_MCP_TOOLS,
    FROZEN_FACADE_INTENTS,
    FROZEN_ROLES,
    FROZEN_WORKFLOWS,
)
from baldr_router.server import mcp


def test_feature_freeze_contract_matches_core_surface():
    assert FEATURE_FREEZE_ACTIVE is True
    tools = asyncio.run(mcp.list_tools())
    names = tuple(sorted(tool.name for tool in tools))
    assert names == FROZEN_CORE_MCP_TOOLS

    prompts = asyncio.run(mcp.list_prompts())
    prompt_names = tuple(sorted(prompt.name for prompt in prompts))
    assert prompt_names == FROZEN_CORE_MCP_PROMPTS


def test_feature_freeze_contract_matches_builtin_providers_and_workflows():
    assert tuple(get_provider_registry().canonical_names()) == FROZEN_BUILTIN_PROVIDERS
    defaults = AppConfig.defaults()
    assert tuple(sorted(defaults.roles)) == FROZEN_ROLES
    assert tuple(sorted(defaults.workflows)) == FROZEN_WORKFLOWS
    assert FACADE_INTENTS == FROZEN_FACADE_INTENTS

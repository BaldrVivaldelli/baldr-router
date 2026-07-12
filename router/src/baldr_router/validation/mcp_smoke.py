from __future__ import annotations

import asyncio
import os
import sys
from typing import Any

from baldr_router.release_policy import FROZEN_CORE_MCP_PROMPTS, FROZEN_CORE_MCP_TOOLS


async def _handshake() -> dict[str, Any]:
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    env = os.environ.copy()
    env["BALDR_VERIFY_DISABLE"] = "1"
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "baldr_router", "mcp"],
        env=env,
    )
    stage = "starting server"
    try:
        async with asyncio.timeout(15):
            async with stdio_client(params) as streams:
                stage = "opening client session"
                read_stream, write_stream = streams
                async with ClientSession(read_stream, write_stream) as session:
                    stage = "initializing"
                    initialized = await session.initialize()
                    stage = "listing tools"
                    tools = await session.list_tools()
                    stage = "listing prompts"
                    prompts = await session.list_prompts()
                    stage = "closing client session"
                stage = "stopping server"
    except TimeoutError as exc:
        raise RuntimeError(f"MCP smoke timed out while {stage}.") from exc
    tool_names = sorted(tool.name for tool in tools.tools)
    prompt_names = sorted(prompt.name for prompt in prompts.prompts)
    missing_tools = sorted(set(FROZEN_CORE_MCP_TOOLS) - set(tool_names))
    missing_prompts = sorted(set(FROZEN_CORE_MCP_PROMPTS) - set(prompt_names))
    return {
        "ok": not missing_tools and not missing_prompts,
        "server": {
            "name": getattr(initialized.serverInfo, "name", None),
            "version": getattr(initialized.serverInfo, "version", None),
            "protocol_version": initialized.protocolVersion,
        },
        "tools": tool_names,
        "prompts": prompt_names,
        "missing_core_tools": missing_tools,
        "missing_core_prompts": missing_prompts,
    }


def mcp_handshake() -> dict[str, Any]:
    try:
        return asyncio.run(_handshake())
    except Exception as exc:
        return {
            "ok": False,
            "error": {
                "type": type(exc).__name__,
                "message": str(exc),
            },
        }


def main() -> int:
    import json

    result = mcp_handshake()
    print(json.dumps(result, ensure_ascii=False), flush=True)
    return 0 if result.get("ok") else 2


if __name__ == "__main__":
    raise SystemExit(main())

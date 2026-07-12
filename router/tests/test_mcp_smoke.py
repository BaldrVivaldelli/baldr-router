from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

import mcp
from mcp.client import stdio

from baldr_router.validation import mcp_smoke


def test_handshake_timeout_reports_the_stage(monkeypatch):
    @asynccontextmanager
    async def stalled_stdio_client(_params):
        yield object(), object()

    class StalledClientSession:
        def __init__(self, *_streams):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc_info):
            return None

        async def initialize(self):
            await asyncio.Event().wait()

    real_timeout = asyncio.timeout
    monkeypatch.setattr(stdio, "stdio_client", stalled_stdio_client)
    monkeypatch.setattr(mcp, "ClientSession", StalledClientSession)
    monkeypatch.setattr(
        mcp_smoke.asyncio,
        "timeout",
        lambda _seconds: real_timeout(0.01),
    )

    result = mcp_smoke.mcp_handshake()

    assert result == {
        "ok": False,
        "error": {
            "type": "RuntimeError",
            "message": "MCP smoke timed out while initializing.",
        },
    }

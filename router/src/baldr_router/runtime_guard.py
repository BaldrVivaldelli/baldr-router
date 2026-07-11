from __future__ import annotations

import os
import uuid
from typing import Any

from .config import load_config


def new_run_id(prefix: str = "workflow") -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


def current_depth() -> int:
    raw = os.environ.get("BALDR_ROUTER_DEPTH", "0")
    try:
        return max(0, int(raw))
    except ValueError:
        return 0


def _block(action: str, code: str, reason: str) -> dict[str, Any]:
    return {
        "ok": False,
        "blocked": True,
        "reason": reason,
        "error": {"code": code, "message": reason, "retryable": False},
        "action": action,
        "parent_run_id": os.environ.get("BALDR_ROUTER_RUN_ID"),
        "parent_workflow": os.environ.get("BALDR_ROUTER_WORKFLOW"),
        "parent_role": os.environ.get("BALDR_ROUTER_ACTIVE_ROLE"),
        "parent_provider": os.environ.get("BALDR_ROUTER_PARENT_PROVIDER"),
        "depth": current_depth(),
    }


def reentry_block_reason(action: str = "run") -> dict[str, Any] | None:
    """Block nested orchestration when the configured depth/re-entry guard is hit."""
    cfg = load_config()
    depth = current_depth()
    if cfg.safety.prevent_router_reentry and os.environ.get("BALDR_ROUTER_DISABLE_REENTRY") == "1":
        return _block(
            action,
            "router_reentry_blocked",
            "baldr-router re-entry is disabled for this provider child process.",
        )
    if depth > cfg.safety.max_depth:
        return _block(
            action,
            "router_max_depth_exceeded",
            f"Router depth {depth} exceeds configured max_depth={cfg.safety.max_depth}.",
        )
    return None


def provider_recursion_block_reason(provider: str, action: str = "provider_run") -> dict[str, Any] | None:
    cfg = load_config()
    depth = current_depth()
    if depth >= cfg.safety.max_depth and depth > 0:
        return _block(
            action,
            "provider_max_depth_reached",
            f"Provider invocation blocked at depth {depth}; max_depth={cfg.safety.max_depth}.",
        )
    parent = (os.environ.get("BALDR_ROUTER_PARENT_PROVIDER") or "").strip().lower()
    selected = provider.strip().lower()
    if cfg.safety.prevent_same_provider_recursion and parent and parent == selected:
        return _block(
            action,
            "same_provider_recursion_blocked",
            f"Recursive invocation of provider {provider!r} is blocked.",
        )
    return None


def child_provider_env(
    *, run_id: str, workflow: str, role: str, provider: str
) -> dict[str, str]:
    """Environment variables passed to child providers for hard recursion guards."""
    return {
        "BALDR_ROUTER_RUN_ID": run_id,
        "BALDR_ROUTER_WORKFLOW": workflow,
        "BALDR_ROUTER_ACTIVE_ROLE": role,
        "BALDR_ROUTER_PARENT_PROVIDER": provider,
        "BALDR_ROUTER_DEPTH": str(current_depth() + 1),
        "BALDR_ROUTER_DISABLE_REENTRY": "1",
    }

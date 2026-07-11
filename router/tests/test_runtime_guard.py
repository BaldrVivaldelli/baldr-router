from __future__ import annotations

from baldr_router.config import load_config, save_config
from baldr_router.runtime_guard import provider_recursion_block_reason, reentry_block_reason


def test_max_depth_is_enforced(tmp_path, monkeypatch):
    monkeypatch.delenv("BALDR_ROUTER_DISABLE_REENTRY", raising=False)
    monkeypatch.setenv("BALDR_ROUTER_DEPTH", "2")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))

    result = reentry_block_reason("test")

    assert result is not None
    assert result["error"]["code"] == "router_max_depth_exceeded"


def test_same_provider_recursion_is_enforced(tmp_path, monkeypatch):
    monkeypatch.delenv("BALDR_ROUTER_DISABLE_REENTRY", raising=False)
    monkeypatch.setenv("BALDR_ROUTER_DEPTH", "1")
    monkeypatch.setenv("BALDR_ROUTER_PARENT_PROVIDER", "codex")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    cfg = load_config()
    cfg.safety.max_depth = 2
    save_config(cfg)

    result = provider_recursion_block_reason("codex", action="review")

    assert result is not None
    assert result["error"]["code"] == "same_provider_recursion_blocked"

from baldr_router.context7_setup import (
    context7_onboarding_plan,
    enable_context7_env_source,
    disable_context7,
)


def test_context7_onboarding_plan_never_contains_secret(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setenv("CONTEXT7_API_KEY", "test-context7-secret-value")

    plan = context7_onboarding_plan()

    payload = repr(plan)
    assert plan["ok"] is True
    assert plan["ask_user_first"] is True
    assert "test-context7-secret-value" not in payload
    assert any(choice["id"] == "skip" for choice in plan["choices"])
    assert any(choice["id"] == "local_file" for choice in plan["choices"])
    assert any(choice["id"] == "env_var" for choice in plan["choices"])


def test_enable_context7_env_source_without_secret(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.delenv("CONTEXT7_API_KEY", raising=False)

    result = enable_context7_env_source(mode="router-cache", install_codex_mcp=False)

    assert result["ok"] is True
    assert result["context7"]["enabled"] is True
    assert result["context7"]["api_key_source"] == "env:CONTEXT7_API_KEY"
    assert result["api_key_available"] is False
    assert "CONTEXT7_API_KEY" in result["next_step"]

    disabled = disable_context7()
    assert disabled["ok"] is True
    assert disabled["context7_enabled"] is False

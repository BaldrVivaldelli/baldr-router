from pathlib import Path

from baldr_router.codex_config import (
    install_context7_mcp_config,
    remove_context7_mcp_config,
    context7_mcp_config_status,
)


def test_install_and_remove_context7_block(tmp_path: Path):
    p = tmp_path / "config.toml"
    p.write_text('model = "gpt-5.6-sol"\n')
    result = install_context7_mcp_config(path=p)
    assert result["ok"] is True
    text = p.read_text()
    assert "[mcp_servers.context7]" in text
    assert "CONTEXT7_API_KEY" in text
    status = context7_mcp_config_status(path=p)
    assert status["managed"] is True
    result = remove_context7_mcp_config(path=p)
    assert result["changed"] is True
    assert "[mcp_servers.context7]" not in p.read_text()


def test_refuse_unmanaged_context7(tmp_path: Path):
    p = tmp_path / "config.toml"
    p.write_text('[mcp_servers.context7]\ncommand = "custom"\n')
    result = install_context7_mcp_config(path=p)
    assert result["ok"] is False

from __future__ import annotations

from baldr_kiro_adapter.extension import register


class FakeMcp:
    def __init__(self):
        self.tools = []

    def tool(self):
        def decorator(func):
            self.tools.append(func.__name__)
            return func

        return decorator


def test_register_exposes_only_kiro_adapter_tools():
    mcp = FakeMcp()
    metadata = register(mcp)
    assert metadata["adapter"] == "kiro"
    assert set(mcp.tools) == {
        "kiro_workspace_status",
        "kiro_install_workspace",
        "kiro_uninstall_workspace",
        "delegate_spec_task",
    }

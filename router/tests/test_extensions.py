from __future__ import annotations

from baldr_router import extensions


class FakeEntryPoint:
    name = "test-adapter"
    value = "test:register"
    group = extensions.EXTENSION_ENTRY_POINT_GROUP

    def load(self):
        def register(mcp):
            mcp.registered.append("tool")
            return {"adapter": "test", "tools": ["tool"]}

        return register


class FakeMcp:
    def __init__(self):
        self.registered = []


def test_extension_loader_registers_once(monkeypatch):
    extensions._reset_for_tests()
    monkeypatch.setattr(extensions, "_entry_points", lambda: [FakeEntryPoint()])
    mcp = FakeMcp()
    first = extensions.load_installed_extensions(mcp)
    second = extensions.load_installed_extensions(mcp)
    assert first[0]["loaded"] is True
    assert first[0]["adapter"] == "test"
    assert second == first
    assert mcp.registered == ["tool"]

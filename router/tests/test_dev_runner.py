from __future__ import annotations

import importlib.util
from pathlib import Path


def _dev_module():
    path = Path(__file__).resolve().parents[2] / "scripts" / "dev.py"
    spec = importlib.util.spec_from_file_location("baldr_dev_script", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_official_runner_does_not_inherit_baldr_execution_context() -> None:
    dev = _dev_module()

    cleaned = dev._clean_test_environment(
        {
            "PATH": "/tools",
            "HOME": "/home/test",
            "BALDR_ROUTER_DEPTH": "2",
            "BALDR_ROUTER_DISABLE_REENTRY": "1",
            "baldr_agent_ref": "local://codex/example@1.0.0",
        }
    )

    assert cleaned == {"PATH": "/tools", "HOME": "/home/test"}

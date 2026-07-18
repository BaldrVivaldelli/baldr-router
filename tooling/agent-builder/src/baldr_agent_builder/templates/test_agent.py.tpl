from __future__ import annotations

import importlib.util
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("generated_agent", ROOT / "agent.py")
assert SPEC and SPEC.loader
AGENT = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(AGENT)


class Context:
    def emit(self, category: str, message: str) -> None:
        assert category and message


class GeneratedAgentTest(unittest.TestCase):
    def test_writer_and_reviewer(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            request = SimpleNamespace(workspace_root=Path(temporary))
            writer = AGENT.execute("writer", request, Context())
            reviewer = AGENT.execute("reviewer", request, Context())
            self.assertEqual(writer["final_report"]["status"], "implemented")
            self.assertEqual(reviewer["final_report"]["review_decision"], "approved")

    def test_explicit_role_identity(self) -> None:
        reference = "local://example/{{NAME}}-planner@1.0.0"
        self.assertEqual(AGENT.role_from_ref(reference), "planner")


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from baldr_agent_sdk import Agent


OUTPUT_NAME = "{{OUTPUT_NAME}}"


def role_from_ref(reference: str) -> str:
    agent_name = reference.rsplit("/", 1)[-1].split("@", 1)[0]
    for role in ("planner", "writer", "reviewer"):
        if agent_name.endswith("-" + role):
            return role
    raise ValueError("Unsupported AgentRef: " + reference)


def report(status: str, summary: str) -> dict[str, Any]:
    return {
        "status": status,
        "summary": summary,
        "interpretation": "Execute the generated external-agent example.",
        "scope": [OUTPUT_NAME],
        "approach": ["Plan", "Write", "Review"],
        "plan_steps": ["Plan", "Write", "Review"],
        "work_completed": [],
        "work_next": [],
        "findings": [],
        "corrections": [],
        "verification_evidence": [],
        "changes_added": [],
        "changes_modified": [],
        "changes_removed": [],
        "files_added": [],
        "files_modified": [],
        "files_deleted": [],
        "commands_run": [],
        "tests_run": [],
        "verification_needed": [],
        "risks": [],
        "follow_up": [],
        "decisions": {"delivery": "direct"},
        "constraints": [],
        "assumptions": [],
        "alternatives_rejected": [],
        "acceptance_criteria": [OUTPUT_NAME + " exists"],
        "blockers": [],
        "review_decision": "not_applicable",
    }


def execute(role: str, request: Any, context: Any) -> dict[str, Any]:
    if request.workspace_root is None:
        raise ValueError("The agent requires a workspace.")
    output = Path(request.workspace_root) / OUTPUT_NAME
    if role == "planner":
        context.emit("analyzing", "Planning the generated example.")
        value = report("planned", "The generated example is ready to run.")
        value["work_next"] = ["Create " + OUTPUT_NAME]
        return {"ok": True, "final_report": value}
    if role == "writer":
        existed = output.exists()
        context.emit("changing", "Writing " + OUTPUT_NAME + ".")
        output.write_text("# External agent result\n", encoding="utf-8")
        value = report("implemented", "The generated example wrote its result.")
        value["files_modified" if existed else "files_added"] = [OUTPUT_NAME]
        return {"ok": True, "final_report": value}
    context.emit("verifying", "Reviewing " + OUTPUT_NAME + ".")
    approved = output.is_file() and not output.is_symlink()
    value = report(
        "approved" if approved else "needs_changes",
        "The generated result is valid." if approved else "The generated result is missing.",
    )
    value["review_decision"] = "approved" if approved else "changes_required"
    if not approved:
        value["blockers"] = [OUTPUT_NAME + " is missing"]
    return {"ok": True, "final_report": value}


def create_agent() -> Agent:
    reference = os.environ["BALDR_AGENT_REF"]
    role = role_from_ref(reference)
    role_capability = {
        "planner": "role.architect",
        "writer": "role.implementer",
        "reviewer": "role.reviewer",
    }[role]
    capabilities = ["workspace.read", role_capability]
    if role == "writer":
        capabilities.append("workspace.write")
    agent = Agent(
        ref=reference,
        owner="generated-agent-team",
        capabilities=capabilities,
        effect_mode="workspace-write" if role == "writer" else "read-only",
    )

    @agent.invoke
    def invoke(request: Any, context: Any) -> dict[str, Any]:
        return execute(role, request, context)

    return agent


def main() -> int:
    return create_agent().serve_stdio()


if __name__ == "__main__":
    raise SystemExit(main())

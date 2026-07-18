import { writeFileSync, existsSync } from "node:fs";
import { join } from "node:path";

import { Agent, type AgentContext, type AgentRequest, type JsonObject } from "@baldr/agent-sdk";

const OUTPUT_NAME = "{{OUTPUT_NAME}}";

export function roleFromRef(reference: string): "planner" | "writer" | "reviewer" {
  const agentName = reference.split("/").at(-1)?.split("@")[0] ?? "";
  for (const role of ["planner", "writer", "reviewer"] as const) {
    if (agentName.endsWith(`-${role}`)) return role;
  }
  throw new Error(`Unsupported AgentRef: ${reference}`);
}

function report(status: string, summary: string): JsonObject {
  return {
    status,
    summary,
    interpretation: "Execute the generated TypeScript external-agent example.",
    scope: [OUTPUT_NAME],
    approach: ["Plan", "Write", "Review"],
    plan_steps: ["Plan", "Write", "Review"],
    work_completed: [],
    work_next: [],
    findings: [],
    corrections: [],
    verification_evidence: [],
    changes_added: [],
    changes_modified: [],
    changes_removed: [],
    files_added: [],
    files_modified: [],
    files_deleted: [],
    commands_run: [],
    tests_run: [],
    verification_needed: [],
    risks: [],
    follow_up: [],
    decisions: { delivery: "direct" },
    constraints: [],
    assumptions: [],
    alternatives_rejected: [],
    acceptance_criteria: [`${OUTPUT_NAME} exists`],
    blockers: [],
    review_decision: "not_applicable",
  };
}

export function execute(
  role: "planner" | "writer" | "reviewer",
  request: AgentRequest,
  context: AgentContext,
): JsonObject {
  if (request.workspaceRoot === null) throw new Error("The agent requires a workspace.");
  const output = join(request.workspaceRoot, OUTPUT_NAME);
  if (role === "planner") {
    context.emit("analyzing", "Planning the generated TypeScript example.");
    const value = report("planned", "The generated example is ready to run.");
    value.work_next = [`Create ${OUTPUT_NAME}`];
    return { ok: true, final_report: value };
  }
  if (role === "writer") {
    const existed = existsSync(output);
    context.emit("changing", `Writing ${OUTPUT_NAME}.`);
    writeFileSync(output, "# TypeScript external agent result\n", "utf8");
    const value = report("implemented", "The TypeScript agent wrote its result.");
    value[existed ? "files_modified" : "files_added"] = [OUTPUT_NAME];
    return { ok: true, final_report: value };
  }
  context.emit("verifying", `Reviewing ${OUTPUT_NAME}.`);
  const approved = existsSync(output);
  const value = report(
    approved ? "approved" : "needs_changes",
    approved ? "The generated result is valid." : "The generated result is missing.",
  );
  value.review_decision = approved ? "approved" : "changes_required";
  if (!approved) value.blockers = [`${OUTPUT_NAME} is missing`];
  return { ok: true, final_report: value };
}

export function createAgent(): Agent {
  const reference = process.env.BALDR_AGENT_REF;
  if (!reference) throw new Error("BALDR_AGENT_REF is required.");
  const role = roleFromRef(reference);
  const roleCapability = {
    planner: "role.architect",
    writer: "role.implementer",
    reviewer: "role.reviewer",
  }[role];
  const capabilities = ["workspace.read", roleCapability];
  if (role === "writer") capabilities.push("workspace.write");
  const agent = new Agent({
    ref: reference,
    owner: {{OWNER_LITERAL}},
    capabilities,
    effectMode: role === "writer" ? "workspace-write" : "read-only",
  });
  agent.invoke((request, context) => execute(role, request, context));
  return agent;
}

export async function main(): Promise<number> {
  return createAgent().serveStdio();
}

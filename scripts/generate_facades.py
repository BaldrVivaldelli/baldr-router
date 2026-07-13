from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CONTRACT_PATH = ROOT / "contracts" / "facade-v1.json"
SCHEMA_PATH = ROOT / "contracts" / "facade-v1.schema.json"
PROGRESS_SCHEMA_PATH = ROOT / "contracts" / "work-item-progress-v1.schema.json"
DELIVERABLE_SCHEMA_PATH = ROOT / "contracts" / "phase-deliverable-v1.schema.json"
DELIVERABLE_PAGE_SCHEMA_PATH = (
    ROOT / "contracts" / "phase-deliverable-page-v1.schema.json"
)
DELIVERABLE_INDEX_PAGE_SCHEMA_PATH = (
    ROOT / "contracts" / "phase-deliverable-index-page-v1.schema.json"
)


def rendered_json(path: Path) -> str:
    return json.dumps(json.loads(path.read_text(encoding="utf-8")), indent=2, ensure_ascii=False) + "\n"


def command_markdown(name: str, item: dict) -> str:
    front = ["---", f"description: {item['description']}"]
    if name == "run":
        front.append("argument-hint: <task>")
    front.append("---")
    if name == "setup":
        body = """
Use the Baldr Router MCP prompt `setup` and the shared Baldr tools to inspect runtime readiness, providers, role assignments, and optional Context7 onboarding.

Never request API keys in chat. Client-specific UX belongs in this facade; routing and workflow decisions remain in Baldr Router.
"""
    elif name == "status":
        body = """
Use the Baldr Router MCP prompt `status`. Return a concise report covering runtime readiness, role-to-provider assignments, optional Context7, recent runs, and one actionable next step. Do not modify files.
"""
    else:
        body = """
Use the Baldr Router MCP prompt `run` with the active workspace and the user's task. Let Baldr control architect, implementer, and reviewer dialogue and return the consolidated structured final report.

Task arguments: `$ARGUMENTS`
"""
    return "\n".join(front) + "\n" + body.strip() + "\n"


def generated_typescript(contract: dict) -> str:
    intents = contract["intents"]
    names = " | ".join(repr(name) for name in intents)
    blocks = []
    for name, item in intents.items():
        blocks.append(
            "  {\n"
            f"    id: {name!r},\n"
            f"    title: {item['title']!r},\n"
            f"    description: {item['description']!r},\n"
            f"    requiresWorkspace: {str(item['requiresWorkspace']).lower()},\n"
            f"    requiresTask: {str(item['requiresTask']).lower()},\n"
            f"    mcpPrompt: {item['mcpPrompt']!r},\n"
            f"    cli: {json.dumps(item['cli'])},\n"
            f"    instruction: {item['instruction']!r},\n"
            "  }"
        )
    return f"""// Generated from contracts/facade-v1.json. Do not edit by hand.
export type BaldrIntentId = {names};

export interface BaldrIntentDefinition {{
  readonly id: BaldrIntentId;
  readonly title: string;
  readonly description: string;
  readonly requiresWorkspace: boolean;
  readonly requiresTask: boolean;
  readonly mcpPrompt: BaldrIntentId;
  readonly cli: readonly string[];
  readonly instruction: string;
}}

export const BALDR_INTENTS: readonly BaldrIntentDefinition[] = [
{',\n'.join(blocks)}
] as const;
"""


def docs_table(contract: dict) -> str:
    rows = ["| Intent | Meaning | VS Code extension | Agent Plugin | Kiro |", "|---|---|---|---|---|"]
    for name, item in contract["intents"].items():
        aliases = item.get("aliases", {})
        rows.append(
            f"| `{name}` | {item['description']} | `{aliases.get('vscodeParticipant', '')}` | "
            f"`{aliases.get('vscodeAgentPlugin', '')}` | `{aliases.get('kiro', '')}` |"
        )
    return "\n".join(rows)


def outputs() -> dict[Path, str]:
    contract = json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))
    contract_json = rendered_json(CONTRACT_PATH)
    schema_json = rendered_json(SCHEMA_PATH)
    progress_schema_json = rendered_json(PROGRESS_SCHEMA_PATH)
    deliverable_schema_json = rendered_json(DELIVERABLE_SCHEMA_PATH)
    deliverable_page_schema_json = rendered_json(DELIVERABLE_PAGE_SCHEMA_PATH)
    deliverable_index_page_schema_json = rendered_json(
        DELIVERABLE_INDEX_PAGE_SCHEMA_PATH
    )
    table = docs_table(contract)

    out: dict[Path, str] = {
        ROOT / "router/src/baldr_router/contracts/facade-v1.json": contract_json,
        ROOT / "router/src/baldr_router/contracts/facade-v1.schema.json": schema_json,
        ROOT
        / "router/src/baldr_router/contracts/work-item-progress-v1.schema.json": progress_schema_json,
        ROOT
        / "router/src/baldr_router/contracts/phase-deliverable-v1.schema.json": deliverable_schema_json,
        ROOT
        / "router/src/baldr_router/contracts/phase-deliverable-page-v1.schema.json": deliverable_page_schema_json,
        ROOT
        / "router/src/baldr_router/contracts/phase-deliverable-index-page-v1.schema.json": deliverable_index_page_schema_json,
        ROOT / "facades/vscode-extension/resources/facade-v1.json": contract_json,
        ROOT / "facades/vscode-extension/resources/facade-v1.schema.json": schema_json,
        ROOT
        / "facades/vscode-extension/resources/work-item-progress-v1.schema.json": progress_schema_json,
        ROOT
        / "facades/vscode-extension/resources/phase-deliverable-v1.schema.json": deliverable_schema_json,
        ROOT
        / "facades/vscode-extension/resources/phase-deliverable-page-v1.schema.json": deliverable_page_schema_json,
        ROOT
        / "facades/vscode-extension/resources/phase-deliverable-index-page-v1.schema.json": deliverable_index_page_schema_json,
        ROOT / "facades/vscode-extension/src/generated/intents.ts": generated_typescript(contract),
        ROOT / "facades/kiro/baldr-orchestrator/steering/facade-intents.md": (
            "# Shared facade intents\n\n"
            "This Kiro facade consumes the same versioned contract as every other Baldr client. "
            "It must not duplicate provider, workflow, Context7, telemetry, or verification logic.\n\n"
            f"{table}\n\n"
            "The Power maps onboarding to `setup`, diagnostics to `status`, and spec-task execution to `run`.\n"
        ),
        ROOT / "facades/generic-mcp/INTENTS.md": (
            "# Shared Baldr intents\n\n"
            "Every generic MCP client can use the same three prompts/tools without a native facade.\n\n"
            f"{table}\n\n"
            "Domain behavior remains in `baldr-router`; client configuration only starts the MCP server.\n"
        ),
    }
    for name, item in contract["intents"].items():
        out[ROOT / f"facades/vscode-agent-plugin/commands/baldr-{name}.md"] = command_markdown(name, item)
    return out


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true", help="Fail if generated facade files are stale")
    args = parser.parse_args()
    stale: list[str] = []
    for path, content in outputs().items():
        expected = content.rstrip() + "\n"
        actual = path.read_text(encoding="utf-8") if path.exists() else None
        if actual == expected:
            continue
        if args.check:
            stale.append(str(path.relative_to(ROOT)))
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(expected, encoding="utf-8")
            print(f"generated {path.relative_to(ROOT)}")
    if stale:
        print("Generated facade files are stale:", file=sys.stderr)
        for item in stale:
            print(f"  - {item}", file=sys.stderr)
        print("Run: python scripts/generate_facades.py", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

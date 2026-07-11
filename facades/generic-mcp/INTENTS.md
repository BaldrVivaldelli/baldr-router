# Shared Baldr intents

Every generic MCP client can use the same three prompts/tools without a native facade.

| Intent | Meaning | VS Code extension | Agent Plugin | Kiro |
|---|---|---|---|---|
| `setup` | Prepare the runtime, durable state store, lifecycle verification, trusted-workspace profile, execution profiles, providers, and optional Context7 without exposing secrets. | `@baldr /setup` | `/baldr-setup` | `setup` |
| `status` | Return a compact health report for runtime, durable runs/recovery, lifecycle evidence, workspace profile, providers, execution profiles, workflow, Context7, extensions, and recent runs. | `@baldr /status` | `/baldr-status` | `status` |
| `run` | Create, resume, or idempotently reuse the configured durable orchestration workflow for a task in the active workspace. | `@baldr /run <task>` | `/baldr-run <task>` | `run` |

Domain behavior remains in `baldr-router`; client configuration only starts the MCP server.

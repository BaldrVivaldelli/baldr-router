---
name: baldr-router
description: Use the shared Baldr Router MCP setup, status, and controlled architect-implementer-reviewer workflow.
---

# Baldr Router

This Agent Plugin is a thin facade over the same versioned contract used by the native VS Code extension and Kiro Power.

Use only these public intents:

- `setup`: invoke the Baldr MCP prompt `setup`; never request secrets in chat.
- `status`: invoke the Baldr MCP prompt `status`; return concise actionable warnings.
- `run`: invoke the Baldr MCP prompt `run` with the active workspace and task.

Do not duplicate provider selection, role assignment, Context7 enrichment, telemetry, fix rounds, or verification. Those responsibilities belong to `baldr-router`.

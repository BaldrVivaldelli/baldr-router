---
name: "baldr-orchestrator"
displayName: "Baldr Orchestrator"
description: "Kiro facade for the client-agnostic Baldr Router MCP runtime. Uses the shared setup/status/run contract and Kiro-only idempotent hooks."
keywords: ["baldr", "router", "orchestrator", "mcp", "workflow", "codex", "context7", "spec", "task", "implementation", "review"]
author: "Baldr"
---

# Baldr Orchestrator

This Power is a **thin Kiro facade** over `baldr-router`.

```text
Kiro
  → baldr-orchestrator Power
      → shared facade intent: setup | status | run
          → baldr-router MCP core
```

Kiro-specific hook creation belongs to the separately installed `baldr-kiro-adapter`. Execution-profile resolution, provider selection, roles, workflows, durable recovery, Context7, telemetry, and verification belong to the core.

## Shared intents

Use the same versioned contract as every other client:

- `setup` for first-run health, durable state, resolved execution profiles, provider state, and optional Context7;
- `status` for concise diagnostics;
- `run` for the configured architect → implementer → reviewer workflow.

Do not call Codex, Kiro CLI, or Context7 directly when Baldr Router can own the operation.

## First use in a workspace

1. Use the shared `setup` MCP prompt/instructions.
2. Call `router_doctor` and `router_extension_status`.
3. Verify that the Kiro adapter is loaded.
4. If missing, explain that core and adapter must be installed in the same environment:

```bash
uv tool install --force --editable ./router \
  --with-editable ./facades/kiro/adapter \
  --with-executables-from baldr-kiro-adapter
```

5. Call `kiro_workspace_status` and then `kiro_install_workspace`.
6. The adapter must reuse the core workspace probe and cached lifecycle verification; do not implement a second scanner or self-test in the Power.
7. Treat installation as idempotent. Never force-overwrite a conflicting or modified hook automatically.
8. Ask whether optional Context7 should be enabled. Never request an API key in chat.
9. Confirm that `.kiro/hooks/baldr-router.generated.kiro.hook` is managed and clean.
10. Report the SQLite schema, nonterminal/recovery state, resolved phase profiles, and latest redacted evidence ID from shared setup/status. Do not claim a real-environment pass unless three clean runs are recorded in the matrix.

## Real-environment qualification

The adapter records a redacted Kiro/WSL client receipt during idempotent workspace installation. Qualification remains an operator-only core workflow and does not add MCP tools or public intents. After completing the client assertion and canary templates, run:

```bash
baldr-router qualification run \
  --profile kiro-windows-wsl \
  --workspace-root /path/to/repository \
  --client-assertions /path/to/client-assertions.json \
  --canary-results /path/to/canary-results.json \
  --repeat 3
```

Never claim `qualified` from synthetic tests or screenshots alone. Report the qualification ID and receipt SHA-256.

## Spec tasks

The generated Kiro hook translates a spec task to the shared `run` intent. Baldr controls the role dialogue. After Baldr returns, Kiro still inspects the diff and runs relevant verification before marking the task complete.

## Windows / WSL

The Power uses the optional `baldr-router-launcher` in `auto` mode. It uses the host first and bridges to WSL only when required. If startup fails, ask the user to run:

```bash
baldr-router-launcher detect
```

## Safety

- Never ask for API keys in chat.
- Never write secrets into the workspace.
- Never use `danger-full-access` or `--yolo`.
- Do not create provider-to-provider recursion; Baldr owns the dialogue.
- Do not duplicate the core workflow inside Power instructions.

## Steering

- `steering/facade-intents.md` — generated shared contract mapping.
- `steering/onboarding.md` — Kiro-specific first-run and hook materialization.
- `steering/router-policy.md` — thin mapping from Kiro events to shared intents.
- `steering/safety.md` — client-specific boundaries.

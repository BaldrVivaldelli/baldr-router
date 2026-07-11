# Architecture

## Product center

`baldr-router` is the single domain implementation and is exposed as an MCP server.

```text
client facade
  -> facade contract: setup | status | run
  -> baldr-router core
      -> workflow engine
      -> provider registry
      -> Context7/cache
      -> telemetry/structured reports
      -> safety and recursion guards
```

## Shared facade contract

The source of truth is:

```text
contracts/facade-v1.json
contracts/facade-v1.schema.json
```

It defines three stable user intents:

- `setup`: inspect/readiness and optional provider/Context7 configuration;
- `status`: compact health and recent-run report;
- `run`: execute the frozen orchestration workflow.

Generated facade files are synchronized with `make facades`. CI-style validation uses `make facades-check`.

## Core owns

```text
router/src/baldr_router/
```

- MCP prompts and tools that make sense for every client;
- provider protocol/registry;
- roles and workflows;
- Codex and optional Kiro CLI providers;
- Context7 and cache;
- structured output;
- telemetry;
- recursion/reentry guards;
- generic installed-extension discovery.

## Facades own

```text
facades/<client>/
```

- client-native installation and discovery;
- secure secret storage;
- aliases, slash commands, UI rendering;
- client-specific hooks and compatibility tools.

A facade must not duplicate provider selection logic, workflow execution, Context7 enrichment rules, telemetry aggregation, or verification gates.

## VS Code native extension

```text
VS Code extension
  -> one Command Palette entry: Baldr: Open
  -> @baldr /setup | /status | /run
  -> programmatic MCP registration
  -> private managed Python runtime
  -> host-first / WSL fallback
  -> SecretStorage for optional Context7 key
```

The extension invokes the same core CLI facade and also registers the same MCP server for the general VS Code agent.

## Kiro facade

```text
Kiro Power
  -> baldr-kiro-adapter
  -> shared baldr-router MCP
```

Kiro-specific hook materialization remains outside the core and is idempotent.

## Agent Plugin facade

The Agent Plugin contains only slash-command prompt files and an MCP declaration. It is a secondary, preview distribution path and reuses the same contract.


## Durable control, code, and artifact planes

v0.16 keeps orchestration deterministic in Baldr and treats provider output as external effects:

```text
control plane   -> SQLite state machine + append-only event journal
code plane      -> Git worktree/checkpoints + idempotent patch publication
artifact plane  -> content-addressed reports, patches, telemetry and evidence
```

The workflow snapshot freezes the resolved execution profiles, provider/model settings, permissions, round limits, and workflow version at creation time. Recovery therefore does not silently adopt a later configuration.

Each phase references one or many named execution profiles. A single shared profile can back all phases, or architecture/implementation/review can independently use n/m/l profiles. Provider sessions are keyed by scope, workspace/run, role, provider, model/agent, and profile.

See [`durable-orchestration.md`](durable-orchestration.md) and [`consistency-operator-control.md`](consistency-operator-control.md).

## Provider path

```text
workflow/task
  -> ProviderRegistry
      -> ProviderAdapter.run(ProviderRunRequest)
```

Providers never call one another directly. Baldr owns the conversation state and applies bounded rounds and reentry guards.

## Freeze boundary

v0.16 adds fencing, strict idempotency/resume, durable cancellation and operator reconciliation on top of durable SQLite orchestration while retaining validation/probe/evidence hardening and keeps the functional surface frozen. The functional surface remains frozen to the existing providers, roles, and workflow described in `FEATURE_FREEZE.md`.

# Architecture

## Product center

`baldr-router` is the single domain implementation and is exposed as an MCP server.

```text
client facade
  -> facade contract: setup | status | run
  -> public work-item progress projection
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

Conversation continuation is an internal action of `run`, not a fourth facade
intent. A durable work item owns immutable request turns; each continuation
increments the item revision, starts a related run, and carries forward only a
bounded allowlist from the previous structured final report plus the new
private client context.

Generated facade files are synchronized by `scripts/generate_facades.py`. CI-style validation uses `--check`.

The console hot path uses the additive `baldr-work-item-progress` v1 contract in `contracts/work-item-progress-v1.schema.json`. The projector derives a bounded public narrative from durable steps, structured reports, checkpoints, publications, and allowlisted activity events. Structured reports can carry explicit interpretation, scope, approach, plan steps, completed/next work, findings, corrections, and verification evidence; legacy reports remain valid when those additive fields are absent. Provider and command start events are projected as generic `working`, while `changing` and `verifying` require typed evidence. It is a presentation boundary: prompts, provider wire events, sessions, leases, private roots, raw stderr/stdout, and reasoning never cross it. Existing `phases`, `workflow`, and `timeline` remain available to compatible full-status clients; `status --workbench-only` returns the compact projection without health probes or internal workflow payloads.

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

The VS Code facade resolves workspace identity from explicit Chat references,
the active editor, a previously pinned root, or an explicit multi-root picker.
It captures bounded editor/selection/dirty-buffer/diagnostic context into
private artifacts. The public workbench exposes ordered request turns and a
context-presence flag, never the private context body.

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
code plane      -> autorización + escritura puntual + checkpoints durables
artifact plane  -> content-addressed reports, patches, telemetry and evidence
```

The workflow snapshot freezes the resolved execution profiles, provider/model settings, permissions, round limits, and workflow version at creation time. Recovery therefore does not silently adopt a later configuration.

Each phase references one or many named execution profiles. A single shared profile can back all phases, or architecture/implementation/review can independently use n/m/l profiles. Provider sessions are keyed by scope, workspace/run, role, provider, model/agent, and profile.

El code plane usa **Trabajar directamente** por defecto sin cambiar el alcance elegido por el usuario:

```text
arquitectura
  -> sandbox de solo lectura sobre el workspace elegido

implementación
  -> sandbox workspace-write sobre ese mismo workspace + journal durable
```

Todos los providers reciben sólo la ruta seleccionada. La arquitectura no puede escribir; la implementación escribe directamente y el reviewer comprueba el estado visible en la misma carpeta. `automatic` conserva la pausa opcional para quien la elija explícitamente. Los attempts, leases, checkpoints y eventos permanecen durables; una escritura interrumpida con efectos desconocidos exige reconciliación y nunca se repite automáticamente.

Los worktrees y shadows siguen disponibles para runs aislados existentes o configuraciones avanzadas. En esos modos, los manifests, blobs y journals conservan sus garantías de publicación y recuperación, pero no forman parte del camino predeterminado autorizado.

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

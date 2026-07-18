# Changelog

## 0.19.0 — Narrative Progress UX

- Replaced internal workflow-status rows with a durable narrative view that explains what Baldr is doing now, what each stage produced, what was verified, and whether the user must act.
- Added a versioned, bounded `work-item-progress` projection for Planning, Execution, and Review, including grouped correction rounds, milestones, evidence levels, final results, and contextual recovery actions.
- Added allowlisted live activity categories without exposing prompts, reasoning, provider output, commands, absolute paths, or secrets.
- Added durable, redacted and paginated deliverables for every phase round and retry, with a bounded recent index for polling and on-demand access to the complete history.
- Added progressive disclosure for technical metadata, accessible stage accordions, responsive narrow-panel behavior, persistent expansion state, and plain Spanish wording for non-technical users.
- Added a compact workbench-only status path and adaptive visible-only polling to reduce process, diagnostic, and log noise while preserving durable restart recovery.
- Added strict contract, privacy, retry, intervention, restart, legacy-compatibility, UI, packaging, and adversarial regression coverage.
- Preserved the frozen providers, roles, workflow, MCP tools, prompts, and public `setup/status/run` facade contract.

### External Agent Platform

- Added immutable external-agent identities based on exact `AgentRef + digest`, including lifecycle state, provenance, health diagnostics, deterministic team resolution, explicit per-stage overrides, and idempotent catalog synchronization.
- Added the language-neutral `agent-execution-v1` contract, the separately distributed Python authoring SDK, and the independently deployable `baldr-agent-runner` local data plane.
- Added persistent execution status, bounded events, cancellation, retries, idempotency, artifact SHA-256 attestation, and conservative reconciliation for uncertain write effects.
- Added least-privilege workspace boundaries: read-only roles receive disposable reduced snapshots, while exactly one write participant receives only the explicitly selected workspace.
- Made read-only snapshots usable in normal Python, Node, Rust and similar repositories by omitting generated dependency/build directories, symlinks and special entries without following or copying them.
- Added local-process and versioned HTTP execution connectors while preserving existing Codex and Kiro provider profiles as compatible fallbacks.
- Added Agent Manager operations, file/Kiro/manager discovery sources, safe preview/apply synchronization, immutable publication, RBAC/tenant policy, audit, metrics, backup and recovery support.
- Added automatic compatible-agent selection plus clear planner, writer and reviewer identities in VS Code, CLI and MCP clients; write-capable external agents run directly when their manifest and trusted workspace already grant the required scope.
- Added a separately owned `baldr-agents-pilot` repository demonstrating planning, writing and review through one external artifact, with a single `make pilot` test and real successful runs through CLI, VS Code and Kiro/MCP.
- Added the public `baldr-agent` lifecycle CLI with `init`, `test`, `build`, `publish`, `doctor` and rollback; generated agent repositories build deterministic self-contained artifacts and require no relative dependency on a Baldr checkout.
- Added stable per-version installation, local-catalog or Agent Manager publication, automatic exact-version activation, previous-version rollback and rejection of changed source, definitions or manifests under an immutable AgentRef.
- Split external-agent development into `Baldr Agent SDK`, containing only the language authoring/runtime API, and the separately distributed `Baldr Agent Builder`, which owns `baldr-agent`, project templates, deterministic builds, publication, diagnostics and rollback.
- Refactored Agent Builder into explicit configuration, models, scaffold, build, release and diagnostics modules; moved generated-project content into packaged templates and removed the monolithic internal `project.py` surface.
- Added Builder Protocol v1 with separate service and JSONL driver contracts, exact driver identity, source/artifact digests, idempotent job identities and a transport-neutral Python client.
- Routed `baldr-agent test`, `build` and `publish` through the local Builder backend and the built-in Python process driver, establishing the extension boundary for TypeScript and future language drivers without coupling authoring SDKs to Builder.
- Added neutral `baldr-agent.toml` schema v2 fields (`language`, `entrypoint`, `driver`) while preserving schema v1 Python projects without migration.
- Added bounded driver discovery from explicit registration paths, persisted registrations and `PATH`, with exact id/version/digest selection plus `driver list`, `doctor` and `register` commands.
- Added the public `@baldr/agent-sdk` TypeScript runtime, the external `baldr.typescript` Builder driver and a generated TypeScript agent template that produces deterministic self-contained Node artifacts.
- Added a real polyglot vertical gate covering TypeScript scaffold, tests, reproducible builds, immutable installation, local-catalog publication and Baldr-coordinated architect/implementer/reviewer execution through the independent Runner.
- Made the TypeScript SDK and Builder driver independently installable npm artifacts, with public-package metadata, licenses and a global driver executable discovered automatically from `PATH`.
- Fixed TypeScript driver identity so its digest is invariant across checkout, tarball and global installation paths.
- Added a clean-install release gate that installs only packaged wheels and tarballs, then validates discovery, reproducible builds, publication, immutable-version rejection, update and rollback.
- Added an opt-in trusted-publishing path for the two npm packages and a real external TypeScript pilot exercised through Agent Manager and the same facade contract used by VS Code and Kiro.

## 0.18.0 — Automatic Workspace Protection

- Made **Automatic protection** the recommended default: exact Git roots use managed worktrees, while non-Git folders and selected repository subdirectories use durable BALDR-managed shadow workspaces.
- Added private helper Git repositories, content-addressed manifests, original-state hash verification, and diff-only publication from protected copies.
- Added durable publication cursors, idempotent retry, conflict evidence, restart recovery, and safe inspect, continue, apply, and discard actions.
- Added portable file, mode, symlink, secret, generated-file, size, depth, and path-collision protections for shadow copies.
- Added dirty/unborn Git fallback to shadow, per-operation TOCTOU guards, a non-replaceable credential denylist, and hard provider/sandbox confinement gates.
- Retained shadows with inspect/continue/apply/discard actions after phase failures or review changes, and preserved exact selected scope in direct modes.
- Preserved the frozen providers, roles, workflows, MCP tools, prompts, and public `setup/status/run` facade contract.

## 0.17.0 — Baldr Console & Durable Work Items

- Added a dedicated Baldr Activity Bar section as the primary VS Code experience.
- Added durable SQLite-backed work items shared by the console, chat facade, and workflow engine.
- Added a fixed composer, `+` menu, slash autocomplete, and clickable Git/preset/role/Context7 chips.
- Added visual task status, architecture/implementation/review timeline, cancellation, and reconciliation actions.
- Added explicit Git worktree/current/non-Git modes; non-Git requires user confirmation and keeps reduced-guarantee semantics visible.
- Added lightweight Fast/Balanced/Deep/Custom presets and sequential role-profile Quick Picks instead of a configuration form.
- Kept Copilot Chat as an optional shortcut that creates the same durable items.
- Preserved the frozen public facade intents `setup`, `status`, and `run`; no provider, role, or workflow was added.

## 0.16.1 — Real Environment Qualification

- Added real-client qualification profiles for VS Code Windows/WSL, Remote WSL, native Linux, and Kiro/WSL.
- Added three-pass qualification receipts, client assertions, and ten canaries across two repositories.
- Added portable qualification evidence with canonical SHA-256 receipts.
- Split source, executable artifacts, and synthetic validation evidence into separate release bundles.
- Added one cross-platform developer entrypoint, CI matrices, release attestations, SBOM, provenance, and secret scanning.
- Added structured architecture decision fields and deterministic conflict blocking.
- Kept providers, roles, workflow, MCP tools, prompts, and facade intents frozen.

## 0.16.0 — Consistency & Operator Control

La superficie pública sigue congelada en `setup`, `status` y `run`. Esta versión endurece la orquestación durable sin agregar providers, roles ni workflows:

- fencing tokens monotónicos (`lease_epoch`) para bloquear workers obsoletos después de un takeover;
- request fingerprints ligados a idempotency keys y creación transaccional del input/run;
- resume estricto por ruta e identidad Git del repositorio original;
- cancelación durable con estado `cancelling`, process-tree cleanup y finalización idempotente;
- acciones de reconciliación operables para write attempts `unknown`;
- reconstrucción/validación de worktrees desde checkpoints y policy segura para repos sucios;
- integrity checks, backups, WAL checkpoint, retention y artifact/session GC en SQLite;
- TTL, turn limits e invalidación de sesiones por repositorio/provider/model;
- reducers determinísticos para participants n/m/l en architecture/review;
- pruebas de concurrencia, fencing, idempotency conflicts, cancelación, reconciliación, reconstruction, maintenance y random walks de la state machine.

## 0.15.0 — Durable Baldr-led Orchestration

La superficie pública sigue congelada en `setup`, `status` y `run`. Esta versión agrega hardening interno:

- perfiles de ejecución abstractos reutilizables: uno compartido o listas n/m/l independientes para architecture/implementation/review;
- selección de model/agent, reasoning/effort, runner y session scope por perfil;
- SQLite state store con migraciones verificadas por checksum, WAL y synchronous FULL;
- máquina de estados durable con journal append-only y estado materializado;
- leases, heartbeats, stale-run recovery e `unknown` para efectos de escritura inciertos;
- sesiones persistentes separadas por workspace/run, role, provider, model/agent y profile;
- Git worktrees, checkpoint commits, patches binarios y publicación idempotente;
- idempotency keys de workflow/attempt y snapshot de configuración inmutable;
- evidence generado desde SQLite;
- pruebas de crash/restart/upgrade en boundaries durables y preservación de profiles durante upgrades.

## 0.14.0 — Validation Lab, Workspace Probe & Evidence

La superficie funcional sigue congelada. Esta versión agrega:

- `Baldr Lab` para repetir el lifecycle y exigir tres pases consecutivos;
- `Baldr Probe` para fingerprint de entorno y perfil acotado de workspaces confiados;
- `Baldr Verify` con fixture worker determinístico, cancelación de process tree, MCP restart y fault injection;
- evidence bundles redactados con hashes y summaries;
- verificación automática/cacheada desde VS Code y onboarding de Kiro;
- assets para contenedor Linux, Windows Sandbox y matriz de entornos reales.


## 0.13.0 — Release Candidate Hardening

v0.13 mantiene congelada la superficie funcional de v0.12 y concentra el trabajo en seguridad, confiabilidad, distribución y validación. No agrega providers, roles, workflows ni tools MCP públicas.

### Hardening

- Agregó una política de **trusted workspaces**: por defecto el workspace debe estar explícitamente confiado, ser un repositorio Git y estar fuera de rutas sensibles o del home completo.
- Agregó terminación de árboles de procesos para timeouts, cancelación, señales y cierre del runtime, tanto en Python como en el bootstrap Node/VS Code.
- Agregó códigos de error estables para Codex: binario ausente, login ausente, timeout, aborto por señal, salida no-cero y structured output inválido.
- Reforzó las guardas anti-reentry con profundidad máxima y detección de recursión sobre el mismo provider.
- Agregó redacción recursiva de secretos en logs, telemetría, diagnósticos, cache de Context7 y salida de la extensión.
- Hizo que la extensión de VS Code requiera Workspace Trust antes de ejecutar providers o confiar un repositorio.

### Runtime y distribución

- El runtime privado de VS Code ahora es versionado, verifica SHA-256 del wheel, se instala de forma transaccional con rollback y conserva un número acotado de versiones anteriores.
- El bootstrap compartido mantiene el comportamiento host-first con fallback automático a WSL y resolución directa en Remote WSL/Linux.
- La extensión sigue ofreciendo **Instalación A UN SOLO clic con bootstrap automático**, sin `mcp.json`, launcher global ni instalación manual del core.
- Los wheels del core y del adapter de Kiro se validan en un entorno Python aislado durante el build.

### Validación

- Agregó pruebas de conformidad semántica entre Python facade, CLI facade, MCP prompts/tools y el contrato congelado `setup/status/run`.
- Agregó una matriz sintética de diez tareas sobre dos repositorios Git temporales.
- Agregó E2E sintético del onboarding idempotente del adapter de Kiro.
- Agregó runbooks para VS Code Windows+WSL, VS Code Remote WSL, Kiro Power, cancelación, upgrades y Context7 SecretStorage.
- Agregó documentación de instalación desde una máquina limpia y `dist/RC_VALIDATION.json` en cada release build.

## 0.12.0 — Unified Facades & One-Click VS Code

- Agregó el contrato versionado `facade-v1` con exactamente `setup`, `status` y `run`.
- Agregó MCP prompts y comandos CLI respaldados por la misma implementación del core.
- Agregó una extensión nativa de VS Code con registro programático del MCP.
- Agregó runtime privado administrado, detección automática Windows/WSL y almacenamiento seguro de Context7.
- Redujo la superficie visible de VS Code a `Baldr: Open` y `@baldr /setup|/status|/run`.
- Agregó fachadas finas para Kiro, VS Code Agent Plugin y clientes MCP genéricos.

## 0.11.0 — Architecture Boundary & Feature Freeze

- Separó hooks/onboarding de Kiro en `baldr-kiro-adapter`.
- Agregó provider registry y contrato explícito de capacidades.
- Reemplazó la superficie core `delegate_spec_task` por `delegate_task`.
- Agregó descubrimiento de extensiones mediante Python entry points.
- Congeló providers, roles, workflows, tools y prompts para estabilización.

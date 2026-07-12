# Changelog

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

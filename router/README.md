# baldr-router core

Runtime MCP en Python, agnóstico al cliente, que orquesta providers mediante roles y un workflow controlado.

## Contrato público

El core empaqueta el contrato versionado `facade-v1` y expone únicamente tres intenciones compartidas:

```text
MCP prompts: setup, status, run
CLI: baldr-router facade setup|status|run
```

Las fachadas pueden renderizar esas intenciones de manera diferente, pero no deben reimplementar selección de providers, roles, workflows, Context7, telemetría o verificación.

## Responsabilidades del core

- provider registry y capability checks;
- roles y workflow congelado `architect-implement-review`;
- providers Codex y Kiro CLI opcional;
- Context7/cache;
- structured reports y verificación;
- telemetría local con redacción de secretos;
- state machine durable en SQLite, journal y recovery;
- perfiles de ejecución abstractos 1-for-all o n/m/l por fase;
- Git worktrees/checkpoints y evidence durable;
- trusted workspace policy;
- guardas anti-reentry y profundidad máxima;
- terminación de árboles de procesos;
- carga genérica de adapters mediante entry points.

Los hooks de Kiro viven en `../facades/kiro/adapter` y la UI de VS Code en `../facades/vscode-extension`.

## Trusted workspaces

Por defecto, un provider solo puede acceder a una ruta que:

- haya sido confiada de forma persistente o provista por una fachada confiable mediante `BALDR_TRUSTED_WORKSPACE_ROOTS_JSON`;
- exista y sea un directorio;
- sea un repositorio Git;
- no sea el home completo ni una ruta sensible/sistémica.

Comandos útiles:

```bash
baldr-router workspace-status /path/to/repo
baldr-router trust-workspace /path/to/repo
baldr-router untrust-workspace /path/to/repo
```

## Durable orchestration

El workflow usa SQLite como control plane, Git/worktrees como code plane y artifacts content-addressed como evidence plane. Cada fase puede compartir un perfil o tener una lista independiente de perfiles, modelos/agents, efforts, runners y scopes de sesión.

```bash
# El mismo contrato público permanece congelado
baldr-router facade setup
baldr-router facade status
baldr-router facade run --workspace /path/to/repo --task "..."
```

`status` incluye schema, runs no terminales y recovery. `run` acepta idempotency/resume mediante las fachadas y tools existentes. Ver `../docs/durable-orchestration.md`.

## Baldr Probe, Verify y Lab

La línea v0.16 agrega hardening sin ampliar la superficie MCP congelada:

```bash
# Fingerprint de entorno redactado
baldr-router env-report

# Perfil estático y acotado de un workspace confiable
baldr-router trust-workspace /path/to/repo
baldr-router probe-workspace /path/to/repo --refresh

# Self-test determinístico del ciclo de vida
baldr-router verify /path/to/repo --mode quick
baldr-router verify /path/to/repo --mode full

# Tres ejecuciones consecutivas para demostrar repetibilidad
baldr-router lab /path/to/repo --mode full --repeat 3

# Evidencia redactada
baldr-router evidence --latest
```

`verify` usa un repositorio temporal y un worker fixture interno para probar ejecución, progreso, cancelación de árboles de procesos, handshake/reinicio MCP, actualización/rollback y redacción de secretos. No consume créditos de providers salvo que se use explícitamente `--include-provider-smoke`.

El profile del workspace respeta trust, Git ignore, límites de tamaño y exclusiones de archivos sensibles; no ejecuta scripts ni hace un crawl irrestricto del código fuente.

## Errores estables de Codex

El runner `exec-json` clasifica binario/login ausente, timeout, aborto, salida no-cero e invalid structured output mediante códigos machine-readable. Ver `docs/release-candidate-hardening.md`.

## Desarrollo

```bash
make router-test
make router-lint
```

La línea v0.16 está bajo feature freeze. Ver `../FEATURE_FREEZE.md`.

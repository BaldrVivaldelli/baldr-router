# Baldr Router

**Baldr Router es un runtime local MCP para orquestar agentes con roles.** El core no depende de Kiro, VS Code ni otro cliente: las integraciones son fachadas finas que traducen la UX de cada producto al mismo contrato versionado.

```text
VS Code / Kiro / Codex / Claude Desktop / otro cliente MCP
  -> facade opcional
  -> baldr-router MCP
       -> architect
       -> implementer
       -> reviewer
       -> providers: Codex, Kiro CLI opcional
       -> Context7 opcional
       -> structured output + telemetry + verification
```

> **v0.18.0 — Protección automática del workspace.** La superficie de orquestación sigue congelada: las raíces Git limpias usan worktrees y los estados Git sucios/unborn o demás carpetas usan una copia sombra durable administrada por BALDR, con publicación verificada del diff al finalizar.


## Real Environment Qualification

Los tests sintéticos prueban el runtime, pero no pueden certificar una instalación real de VS Code, Kiro, WSL, autenticación o repositorios privados. El gate introducido en v0.16.1 continúa vigente:

```text
synthetic validation -> provisional -> qualified
```

Comandos operativos, sin cambiar las intenciones MCP públicas `setup/status/run`:

```bash
baldr-router qualification definitions
baldr-router qualification template --profile vscode-windows-wsl --output-dir ./qualification-input
baldr-router qualification run \
  --profile vscode-windows-wsl \
  --workspace-root /path/to/repo \
  --client-assertions ./qualification-input/client-assertions.json \
  --canary-results ./qualification-input/canary-results.json \
  --repeat 3
baldr-router qualification status --latest
```

`qualified` exige el entorno exacto, todas las assertions del cliente, tres pases del Lab y diez tareas con evidence sobre dos repositorios distintos. Un build de CI siempre queda como máximo `provisional`.

Guía: [`docs/real-environment-qualification.md`](docs/real-environment-qualification.md)

## Una implementación, varias fachadas

La implementación de dominio vive únicamente en `router/`:

- selección y ejecución de providers;
- roles y workflow `architect-implement-review`;
- Context7/cache;
- structured reports y verification gates;
- telemetría local;
- durable SQLite state, event journal y evidence;
- perfiles de ejecución reutilizables por fase;
- trusted workspace policy;
- hardening anti-reentry y limpieza de procesos.

Las fachadas solo resuelven UX, instalación y configuración nativa:

```text
facades/
  kiro/
    adapter/                 # tools/hooks específicos de Kiro
    baldr-orchestrator/      # Kiro Power

  vscode-extension/          # extensión nativa
  vscode-agent-plugin/       # fachada Preview con slash commands
  generic-mcp/               # ejemplos declarativos
```

Todas consumen el contrato:

```text
contracts/facade-v1.json
  setup
  status
  run
```

Sincronización y verificación:

```bash
python scripts/generate_facades.py
python scripts/generate_facades.py --check
```

## VS Code: Instalación A UN SOLO clic con bootstrap automático

La extensión nativa incluye el wheel de `baldr-router`, registra el MCP programáticamente y prepara un runtime Python privado y versionado. No requiere editar `mcp.json`, instalar globalmente el launcher ni ejecutar `uv tool install`.

Instalación local:

```text
Extensions
  -> …
  -> Install from VSIX
  -> baldr-router-vscode-0.18.0.vsix
```

Superficie diaria:

```text
Activity Bar:
  Baldr
    -> tasks durables
    -> timeline
    -> composer
    -> + menu
    -> /new /run /status /profile /git /context /roles /cancel /resume /archive /setup /help

Command Palette:
  Baldr: Open

Chat opcional:
  @baldr /setup
  @baldr /status
  @baldr /run <task>
  @baldr <task>
```

No hay un formulario obligatorio. Escribir una tarea en el composer crea y ejecuta un item durable con la configuración activa. Protección de cambios, nivel de detalle, equipo y ayuda adicional se ajustan mediante chips, `+` o slash commands.

Guía de la consola: [`docs/baldr-console.md`](docs/baldr-console.md)

La extensión:

- exige VS Code Workspace Trust antes de ejecutar providers;
- detecta host, Windows/WSL y Remote WSL automáticamente;
- verifica versión y SHA-256 del wheel;
- instala el runtime de forma transaccional con rollback;
- conserva una versión anterior para rollback operativo;
- cancela el árbol de procesos cuando se aborta una ejecución;
- almacena la key opcional de Context7 en VS Code SecretStorage.

> El primer inicio requiere Python 3.11+ en el entorno seleccionado y puede necesitar red para instalar dependencias del wheel. VS Code y los providers mantienen sus propios diálogos de confianza/login.

Guías:

- [`docs/clean-machine-install.md`](docs/clean-machine-install.md)
- [`e2e/vscode-windows-wsl.md`](e2e/vscode-windows-wsl.md)
- [`e2e/vscode-remote-wsl.md`](e2e/vscode-remote-wsl.md)

## Kiro

Kiro usa dos piezas opcionales:

```text
facades/kiro/adapter/
  paquete Python que registra tools de workspace/hook

facades/kiro/baldr-orchestrator/
  Power con onboarding y steering de Kiro
```

Instalación del core y adapter en el mismo environment:

```bash
uv tool install --force --editable ./router \
  --with-editable ./facades/kiro/adapter \
  --with-executables-from baldr-kiro-adapter
```

Después instalá el Power desde:

```text
facades/kiro/baldr-orchestrator/
```

Frase sugerida:

```text
I just installed the power baldr-orchestrator and want to use it.
```

El adapter confía el repositorio seleccionado y crea/actualiza hooks de manera idempotente, sin pisar modificaciones manuales por defecto.

Guía: [`e2e/kiro-power.md`](e2e/kiro-power.md)


## Orquestación durable y perfiles por fase

Baldr controla el workflow; los providers participan en fases acotadas. La configuración no presupone modelos concretos:

```text
architecture:    1 o n perfiles
implementation:  1 o m perfiles
review:          1 o l perfiles
```

Un solo perfil puede respaldar las tres fases o cada fase puede tener su lista ordenada, estrategia `first-success`/`all`, modelo, effort, runner y scope de sesión propios. Las fases con escritura no permiten múltiples escritores concurrentes.

```text
Baldr control plane
  -> SQLite state machine + event journal
  -> architecture participants
  -> implementación en worktree o workspace sombra aislado
  -> review participants
  -> bounded fixes
  -> durable evidence
```

Los workflows guardan un snapshot inmutable de su configuración. Tras un crash, los pasos read-only pueden reintentarse; un write attempt con efectos inciertos queda `unknown` y exige reconciliación. Las sesiones se separan por workspace/run, rol, provider, modelo/agente y perfil.

Detalle y ejemplos TOML: [`docs/durable-orchestration.md`](docs/durable-orchestration.md)

## Consistencia y control operativo

Baldr v0.16 cierra las carreras más importantes entre procesos y efectos externos:

```text
lease owner + lease epoch
  -> cada takeover incrementa el fencing token
  -> un worker viejo ya no puede confirmar resultados

idempotency key + request fingerprint
  -> misma solicitud: reanuda el mismo run
  -> solicitud distinta: idempotency_conflict

write attempt incierto
  -> unknown
  -> awaiting_reconciliation
  -> inspeccionar | continuar | aplicar | descartar, sólo cuando sea seguro
```

La cancelación se persiste antes de terminar procesos; el resume está ligado a la ruta y a la identidad Git o manifest original; los worktrees borrados se reconstruyen desde checkpoints verificables y los shadows sobreviven reinicios en el estado durable de Baldr. Las sesiones expiran o se invalidan ante cambios de repositorio, provider o límites de turnos. `status` presenta runs no terminales, reconciliación, schema, maintenance y perfiles resueltos sin agregar nuevas intenciones públicas.

Detalle: [`docs/consistency-operator-control.md`](docs/consistency-operator-control.md)

## Protección automática del workspace

La opción recomendada y predeterminada es **Protección automática**:

```text
raíz exacta de un repositorio Git limpio  -> worktree administrado por Baldr
Git sucio, sin primer commit o sin Git    -> workspace sombra durable
subcarpeta elegida dentro de otro repo    -> workspace sombra durable
```

Baldr no amplía una subcarpeta hasta el repositorio padre ni exige guardar cambios Git preexistentes. En un shadow copia el estado inicial bajo su directorio local (`shadow-workspaces/<run-id>/tree`), inicializa allí un Git privado auxiliar y ejecuta los agentes únicamente sobre esa copia. Los manifests y blobs SHA-256 —no el Git privado— son la fuente de verdad portable. Protección automática falla de forma cerrada si un provider, runner o sandbox no puede demostrar ese límite; los modos directos quedan como elección explícita de garantía reducida.

La carpeta original permanece intacta durante planificación, implementación y revisión. Si review aprueba, Baldr recalcula los hashes, registra un plan durable y aplica sólo archivos nuevos, modificados o eliminados, cambios de tipo y modos. Cada operación tiene un cursor antes/después, por lo que reintentar tras un crash omite efectos ya aplicados. Si el original cambió o la publicación quedó parcial, se conserva la copia y sólo se muestran acciones seguras para inspeccionar, continuar, aplicar o descartar; **Descartar** no aparece si pudiera abandonar efectos en el original.

La copia excluye `.git`, `.hg`, `.svn`, un piso no reemplazable de secretos, patrones sensibles adicionales y artefactos generados, con límites visibles de entradas, tamaño, profundidad y symlinks. Sólo admite enlaces relativos internos; los modos POSIX se preservan donde la plataforma lo permite y Windows informa explícitamente enlaces/reparse points no soportados. Un shadow publicado y verificado se elimina por defecto; los fallos y conflictos usan retención configurable.

Las opciones avanzadas son **Trabajar directamente** (`current`) y **Sin protección** (`non-git`, con confirmación). Los valores legados guardados como `worktree`, `current` o `non-git` conservan su semántica; las preferencias nuevas usan `automatic`.

Detalle: [`docs/durable-orchestration.md`](docs/durable-orchestration.md)

## Baldr Lab + Probe + Verify

La v0.16 conserva tres capas de hardening:

```text
Baldr Lab
  entornos descartables + tres ejecuciones consecutivas

Baldr Probe
  fingerprint del entorno + perfil acotado del workspace confiado

Baldr Verify / Evidence
  self-test determinístico de install/execute/cancel/restart/update/rollback
```

Comandos de diagnóstico (no amplían la superficie MCP congelada):

```bash
baldr-router env-report
baldr-router probe-workspace /path/to/repo
baldr-router verify --mode full
baldr-router lab --mode full --repeat 3
baldr-router evidence --latest
```

VS Code ejecuta/cachea una verificación rápida al preparar el runtime. El setup de VS Code y el adapter de Kiro generan el mismo perfil de workspace desde el core. Los bundles de evidencia viven fuera del repo, están redactados y contienen hashes de sus artefactos.

Detalle: [`docs/validation-lab-workspace-probe.md`](docs/validation-lab-workspace-probe.md)

## Trusted workspaces

Antes de que un provider lea o escriba un workspace, Baldr exige por defecto que:

- la ruta haya sido confiada explícitamente o provista por una fachada confiable;
- exista y sea un directorio;
- no sea el home completo ni una ruta sensible/sistémica.

Un worktree requiere Git. Una carpeta no-Git puede ejecutarse con Protección automática porque los providers escriben en el shadow administrado; esta excepción por operación no concede permiso para editar el original directamente. **Sin protección** sigue exigiendo consentimiento explícito y guarda esa excepción únicamente para la carpeta elegida.

```bash
baldr-router workspace-status /path/to/repo
baldr-router trust-workspace /path/to/repo
baldr-router untrust-workspace /path/to/repo
```

Las fachadas nativas pueden pasar roots confiables mediante `BALDR_TRUSTED_WORKSPACE_ROOTS_JSON`; eso no desactiva el bloqueo de rutas sensibles, las reglas de copia del shadow ni la confirmación requerida para escribir sin protección.

## Error handling y cancelación

Codex `exec-json` entrega códigos machine-readable para:

```text
codex_not_found
codex_not_authenticated
codex_timeout
codex_process_aborted
codex_process_failed
codex_invalid_structured_output
```

Los subprocesses se ejecutan en grupos propios y Baldr termina sus descendientes ante timeout, cancelación, señal o shutdown. Los runners `app-server` y `sdk` permanecen experimentales; `exec-json` es el default estable.

## Context7 y secretos

Context7 sigue siendo opcional. Las keys pueden venir de SecretStorage, una variable de entorno aprobada o el secret store local. Baldr aplica redacción a:

- output y diagnósticos;
- telemetría JSONL;
- errores;
- cache de Context7;
- logs de la extensión.

La query cruda de Context7 no se persiste en la metadata del cache.

## Providers, roles y workflow congelados

```text
Providers:
  codex
  kiro-cli (opcional)

Roles:
  architect
  implementer
  reviewer

Workflow:
  architect-implement-review
```

El diálogo no ocurre provider-a-provider:

```text
architect -> Baldr -> implementer -> Baldr -> reviewer -> Baldr -> client
```

Esto permite límites de rondas, telemetría, permisos por rol y protección contra loops agenticos.

## Release Candidate Hardening

La matriz base v0.16 conserva los diez escenarios de entorno real y suma crash/restart/upgrade en cada transición durable:

1. VS Code Windows + fallback automático a WSL.
2. VS Code Remote WSL.
3. Kiro Power + adapter.
4. Trusted workspace roots.
5. Cancelación y cleanup de procesos hijos.
6. Upgrades de runtime versionados con rollback.
7. Taxonomía de errores de Codex.
8. Conformidad CLI facade ↔ MCP.
9. Context7 SecretStorage y redacción.
10. Instalación desde una máquina limpia.

Detalles:

- [`docs/release-candidate-hardening.md`](docs/release-candidate-hardening.md)
- [`docs/validation-lab-workspace-probe.md`](docs/validation-lab-workspace-probe.md)
- [`lab/README.md`](lab/README.md)

Los tests automáticos usan providers sintéticos y simulación de plataforma. Los smoke tests reales deben registrarse en [`e2e/REAL_ENVIRONMENT_MATRIX.md`](e2e/REAL_ENVIRONMENT_MATRIX.md); no se considera que un escenario real pasó solo por tests sintéticos.

## Desarrollo y release build

```bash
# Core
cd router
uv run --extra dev pytest -q
uv run --extra dev ruff check src tests

# Kiro adapter
cd ../facades/kiro/adapter
uv run --extra dev pytest -q
uv run --extra dev ruff check src tests

# Contract/facades
cd ../../..
python scripts/generate_facades.py --check

# Launcher
cd launcher && npm test

# VS Code extension
cd ../facades/vscode-extension
npm ci
npm run check
npm test

# Release completa
cd ../..
python scripts/build_release.py
```

El build genera wheels, VSIX, ZIPs de fachadas, checksums y `dist/RC_VALIDATION.json`.

## Documentación

- [`docs/architecture.md`](docs/architecture.md)
- [`docs/durable-orchestration.md`](docs/durable-orchestration.md)
- [`docs/release-candidate-hardening.md`](docs/release-candidate-hardening.md)
- [`docs/validation-lab-workspace-probe.md`](docs/validation-lab-workspace-probe.md)
- [`lab/README.md`](lab/README.md)
- [`docs/clean-machine-install.md`](docs/clean-machine-install.md)
- [`docs/vscode.md`](docs/vscode.md)
- [`docs/kiro.md`](docs/kiro.md)
- [`e2e/README.md`](e2e/README.md)
- [`FEATURE_FREEZE.md`](FEATURE_FREEZE.md)
- [`CHANGELOG.md`](CHANGELOG.md)

## Release reproducible

La release se divide en source, artifacts y validation evidence. No se publica la SQLite de build ni caches internas dentro del source ZIP.

```bash
python scripts/dev.py test
python scripts/dev.py lint
python scripts/dev.py build
python scripts/dev.py verify-release
```

Detalle: [`docs/release-packaging.md`](docs/release-packaging.md)

# Baldr Router

**Baldr es un control plane local para coordinar trabajo con agentes.** Puede
usar Codex o Kiro directamente, como hasta ahora, y también descubrir agentes
externos escritos en Python o TypeScript sin incorporar su código al Router.
El core se expone por MCP y no depende de VS Code, Kiro ni de otro cliente en
particular.

Este repositorio es el monorepo de la infraestructura de Baldr: Router, Agent
Manager, Runner, Agent Builder, SDKs y fachadas de producto. Los agentes de cada
equipo continúan viviendo, versionándose y publicándose desde sus propios
repositorios.

```text
VS Code / Kiro / Codex / otro cliente MCP
                     |
                     v
                Baldr Router
                     |
          +----------+-----------+
          |                      |
          v                      v
 Codex / Kiro normal       Agent Manager
                                  |
                         AgentRef + digest
                                  |
                                  v
                           Agent Runner
                                  |
                                  v
                       artefacto Python / TS

repositorio externo -> SDK -> Agent Builder -> driver -> artefacto + manifiesto
```

## Qué incluye el monorepo

| Pieza | Responsabilidad | Ubicación |
| --- | --- | --- |
| Router | MCP, workflows durables, políticas y coordinación | [`router/`](router/) |
| Agent Manager | Catálogo, resolución de equipos e identidades inmutables | [`router/src/baldr_router/agent_manager.py`](router/src/baldr_router/agent_manager.py) |
| Agent Runner | Ejecutar artefactos externos fuera del proceso del Router | [`runtimes/agent-runner/`](runtimes/agent-runner/) |
| Agent Builder | Crear, probar, construir, publicar y hacer rollback | [`tooling/agent-builder/`](tooling/agent-builder/) |
| SDK Python | API de autoría para agentes Python | [`sdks/python/`](sdks/python/) |
| SDK TypeScript | API de autoría para agentes TypeScript | [`sdks/typescript/`](sdks/typescript/) |
| Driver TypeScript | Implementación externa de Builder Protocol para Node | [`tooling/agent-builder-typescript/`](tooling/agent-builder-typescript/) |
| Fachadas | Integraciones de VS Code, Kiro y otros clientes | [`facades/`](facades/) |

Los SDK son APIs de autoría: el agente importa solamente el SDK de su lenguaje.
Agent Builder es la toolchain de desarrollo y no forma parte del código del
agente. Los drivers traducen el protocolo neutral de Builder al toolchain de un
lenguaje. Baldr almacena la identidad y ubicación del release; no se convierte
en dueño del código fuente del agente.

> **¿Querés usar Baldr y no desarrollar el monorepo?** Empezá por el
> [`golden path`](docs/golden-path.md): instalación, primera tarea,
> recuperación, agentes externos, actualización y rollback sin editar
> configuración manual.

## Dos formas de usar Baldr

### 1. Codex o Kiro directamente

La experiencia existente no cambia. La extensión de VS Code, el Power de Kiro,
Codex y las integraciones MCP pueden seguir usando los perfiles normales sin
instalar Agent Builder, los SDKs ni drivers externos.

### 2. Agentes externos coordinados por Baldr

Un equipo desarrolla el agente con el SDK de su lenguaje. Agent Builder
descubre un driver compatible, produce un artefacto reproducible y publica
manifiestos con `AgentRef + digest`. Agent Manager resuelve las identidades y el
Runner ejecuta el artefacto durante las fases de planificación, implementación
o revisión.

La arquitectura completa está en
[`docs/external-agent-runtime.md`](docs/external-agent-runtime.md). La frontera
políglota está definida por
[`Builder Protocol v1`](docs/builder-protocol.md).

## Preparar un checkout limpio

Requisitos para desarrollar todo el monorepo:

- Python 3.11 o posterior;
- [`uv`](https://docs.astral.sh/uv/);
- Node.js 20 o posterior para el SDK y driver TypeScript;
- Git.

Desde la raíz del repositorio:

```bash
make deps
make install-agent-runtime
npm run build:agents

baldr-agent --help
baldr-agent-runner health
```

`make deps` instala las dependencias de desarrollo de los paquetes Python y
Node. `make install-agent-runtime` expone `baldr-agent` y
`baldr-agent-runner`. El último comando construye el SDK y el driver TypeScript
antes de registrarlo.

Para instalar también la CLI del Router desde este checkout:

```bash
uv tool install --force --editable ./router
baldr-router --help
```

La extensión de VS Code incluye su propio runtime privado del Router. No exige
esta instalación para continuar usando los providers normales. Para ejecutar
agentes externos de proceso local sí necesita encontrar `baldr-agent-runner` en
`PATH`, o recibir su ruta mediante `BALDR_AGENT_RUNNER_COMMAND`.

## Quickstart: agente TypeScript

Durante desarrollo, construí el driver y exponé su binario desde la raíz del
monorepo:

```bash
npm run build:agents
export PATH="$PWD/tooling/agent-builder-typescript/bin:$PATH"
baldr-agent driver doctor baldr.typescript
```

Una release se instala sin conservar el checkout:

```bash
node_package_dir=/ruta/a/release/artifacts/node
npm install --global \
  "$node_package_dir/baldr-agent-sdk-0.20.0.tgz" \
  "$node_package_dir/baldr-agent-builder-typescript-0.20.0.tgz"
baldr-agent driver doctor baldr.typescript
```

Después de publicar los paquetes en el registry, la instalación equivalente es
`npm install --global @baldr/agent-builder-typescript`; npm instala su
dependencia exacta de `@baldr/agent-sdk`. El ejecutable
`baldr-builder-driver-typescript` queda en `PATH`, por lo que no hace falta
registrar una ruta al checkout. `baldr-agent driver register` continúa
disponible para drivers privados distribuidos mediante un manifiesto.

Después creá el agente en el repositorio que será propiedad de tu equipo:

```bash
baldr-agent init ./my-typescript-agent \
  --name my-typescript-agent \
  --owner my-team \
  --namespace product \
  --language typescript

cd my-typescript-agent
baldr-agent test
baldr-agent driver conformance baldr.typescript
baldr-agent build
baldr-agent publish
baldr-agent doctor

baldr-agent run \
  --role implementer \
  --workspace /ruta/al/workspace \
  --request "Generá el resultado"
```

El proyecto importa `@baldr/agent-sdk`; el driver `baldr.typescript` genera un
artefacto Node `.cjs` autocontenido y determinístico. Al instalar el paquete
globalmente, cambiar al directorio del agente no rompe su descubrimiento.

## Quickstart: agente Python

Python dispone de un driver incorporado y no necesita registro adicional:

```bash
baldr-agent init ./my-python-agent \
  --name my-python-agent \
  --owner my-team \
  --namespace product \
  --language python

cd my-python-agent
baldr-agent test
baldr-agent driver conformance baldr.python
baldr-agent build
baldr-agent publish
baldr-agent doctor
```

Los proyectos Python anteriores con `schema_version = 1` y `entry_module`
siguen siendo compatibles. Los proyectos nuevos usan la configuración neutral
v2 con `language`, `entrypoint` y `driver` opcional.

## Verificar el vertical políglota

La prueba oficial crea un agente TypeScript temporal, descubre el driver, lo
prueba, genera dos builds byte a byte idénticos, instala y publica el release y
ejecuta un workflow real con planner, writer y reviewer mediante Baldr:

```bash
npm run build:agents
uv run python scripts/test_typescript_agent_vertical.py
```

Un segundo piloto usa un agente real mantenido fuera de este monorepo, lo
publica en un Agent Manager temporal y ejecuta las tres fases mediante Runner:

```bash
uv run python scripts/test_typescript_external_pilot.py \
  --project /ruta/a/baldr-agents-pilot/typescript-repository-report
```

Validaciones más amplias:

```bash
npm run check:agents
npm run test:agents
make check
```

## Estado del vertical políglota

- Python y TypeScript comparten configuración y contratos neutrales.
- El driver Python está incorporado; el driver TypeScript se descubre como un
  proceso externo JSONL.
- La selección de drivers fija exactamente `id + version + digest`.
- Los artefactos publicados son inmutables por versión y digest.
- El primer artefacto TypeScript es CommonJS autocontenido y requiere Node 20+.
- Agregar otro lenguaje requiere un SDK de autoría y un driver compatible; no
  requiere modificar Router, Agent Manager ni Runner.

> **v0.20.0 — Agentes externos políglotas.** La consola explica en lenguaje cotidiano
> qué entendió Baldr, qué está haciendo, qué produjo Planificación, Ejecución y
> Revisión, qué comprobó y cuándo necesita una decisión. El recorrido normal
> trabaja directamente sobre el workspace confiado; la autorización por tarea
> de v0.18 continúa disponible como una opción explícita.


## Real Environment Qualification

Los tests sintéticos prueban el runtime, pero no pueden certificar una instalación real de VS Code, Kiro, WSL, autenticación o repositorios privados. El gate introducido en v0.16.1 continúa vigente:

```text
synthetic validation -> provisional -> qualified
```

Comandos operativos, sin cambiar las intenciones MCP públicas `setup/status/run`:

```bash
baldr-router qualification definitions
baldr-router qualification template --profile vscode-remote-wsl --output-dir ./qualification-input
baldr-router qualification run \
  --profile vscode-remote-wsl \
  --workspace-root /path/to/repo \
  --client-assertions ./qualification-input/client-assertions.json \
  --canary-results ./qualification-input/canary-results.json \
  --repeat 3
baldr-router qualification status --latest
baldr-router qualification promotion-status --receipt ./qualification-output --release-version 0.20.0
```

`qualified` exige el entorno exacto, todas las assertions del cliente, tres pases del Lab y diez tareas con evidence sobre dos repositorios distintos. Un build de CI siempre queda como máximo `provisional`.

El gate de promoción de esta iteración es el recorrido VS Code Remote WSL +
Codex. Kiro mantiene su implementación, paquetes y pruebas, pero su
qualification real no bloquea v0.20 y se retomará en una iteración posterior.

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
  -> baldr-router-vscode-0.20.0.vsix
```

Superficie diaria:

```text
Activity Bar:
  Baldr
    -> tasks durables
    -> progreso narrativo durable
    -> composer
    -> + menu
    -> /new /run /status /profile /git /context /roles /cancel /resume /archive /restore /delete /setup /help

Command Palette:
  Baldr: Open

Chat opcional:
  @baldr /setup
  @baldr /status
  @baldr /run <task>
  @baldr <task>
```

No hay un formulario obligatorio. Escribir una tarea en el composer crea y ejecuta un item durable con la configuración activa. Protección de cambios, nivel de detalle, equipo y ayuda adicional se ajustan mediante chips, `+` o slash commands.

Después de terminar, el siguiente pedido en el mismo Chat o en la sesión
seleccionada de la consola continúa el mismo work item como un turno durable.
`/resume` sigue siendo recuperación de ejecuciones interrumpidas. La extensión
resuelve la carpeta mediante referencias, editor activo o elección explícita en
multi-root, y adjunta contexto acotado del archivo/selección/dirty buffer y sus
diagnósticos. El resultado estructurado final queda visible antes que los
detalles técnicos.

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

La distribución ejecutable también incluye
`artifacts/node/baldr-router-launcher-0.20.0.tgz`. Instalalo en el host que
inicia Kiro antes de cargar el Power; el launcher encuentra primero un Router
local y usa WSL como fallback automático. No hace falta editar `mcp.json` ni
depender de un checkout del monorepo:

```bash
npm install --global \
  ./artifacts/node/baldr-router-launcher-0.20.0.tgz
baldr-router-launcher detect
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
  -> permisos efectivos = fase ∩ capacidades del agente
  -> implementación directa en el workspace elegido
  -> review participants
  -> bounded fixes
  -> durable evidence
```

Los workflows guardan un snapshot inmutable de su configuración. Tras un crash, los pasos read-only pueden reintentarse; un write attempt con efectos inciertos queda `unknown` y exige reconciliación. Las sesiones se separan por workspace/run, rol, provider, modelo/agente y perfil.

Detalle y ejemplos TOML: [`docs/durable-orchestration.md`](docs/durable-orchestration.md)

Los perfiles también pueden apuntar a identidades exactas de agentes externos.
El registry local resuelve manifests versionados y el gateway invoca el
transporte declarado sin cargar código de agentes dentro de Baldr. Codex y Kiro
continúan funcionando por la ruta anterior cuando el perfil no declara
`agent_ref`. Detalle y piloto Kiro:
[`docs/external-agent-registry.md`](docs/external-agent-registry.md).
El transporte HTTP independiente y la frontera de Agent Manager están
documentados en [`docs/external-agent-http.md`](docs/external-agent-http.md).
La publicación por equipos, RBAC/tenancy, auditoría, backups y operación del
servicio están en
[`docs/agent-manager-operations.md`](docs/agent-manager-operations.md).

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

## Autorización de cambios en el workspace

La opción recomendada y predeterminada es **Trabajar directamente** (`current`):

```text
arquitectura   -> solo lectura
implementación -> escritura directa en la carpeta seleccionada
revisión       -> comprobación del diff y de las verificaciones
```

Este es el mismo modelo operativo visible de Codex/Kiro: el provider recibe únicamente el workspace elegido y su sandbox, y los cambios aparecen directamente en esa carpeta sin una pausa de autorización por tarea. Baldr no publica una copia completa al terminar ni exige que el resto del repositorio permanezca congelado. Git y los checkpoints durables registran el resultado; si una escritura se interrumpe con efectos inciertos, la sesión pide reconciliación en vez de repetirla silenciosamente.

**Pedir autorización** (`automatic`) queda disponible como elección explícita para quien prefiera una pausa antes de la primera escritura. **Sin protección** (`non-git`) permite el mismo modelo directo sin exigir Git y mantiene su confirmación modal. Los worktrees y workspaces sombra siguen soportados para configuraciones y sesiones aisladas existentes, incluidas sus acciones de recuperación, pero ya no son el comportamiento predeterminado de un work item nuevo.

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

Un worktree requiere Git. **Pedir autorización** admite carpetas confiables sin Git porque cada tarea solicita permiso antes de su primera escritura directa. **Sin protección** sigue exigiendo consentimiento explícito y guarda esa excepción únicamente para la carpeta elegida.

```bash
baldr-router workspace-status /path/to/repo
baldr-router trust-workspace /path/to/repo
baldr-router untrust-workspace /path/to/repo
```

Las fachadas nativas pueden pasar roots confiables mediante `BALDR_TRUSTED_WORKSPACE_ROOTS_JSON`; eso no desactiva el bloqueo de rutas sensibles, la intersección de capacidades de escritura ni la confirmación requerida para trabajar sin Git.

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

El mismo contrato se usa desde Router y VS Code: cancelar persiste primero
`cancelling`, termina el árbol y materializa `cancelled`; cerrar el cliente
termina los árboles capturados y el siguiente arranque resuelve únicamente los
estados recuperables sin decisiones alternativas. Cada cierre automático
incluye `process_validation.orphan_processes`, y cada error separa el código y
mensaje técnicos de `error.summary` y `error.action`. La definición normativa
está en [`docs/consistency-operator-control.md`](docs/consistency-operator-control.md#contrato-único-de-cierre-cancelación-y-recuperación).

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

- [`docs/golden-path.md`](docs/golden-path.md) — recorrido único de instalación, primera tarea, recuperación y agentes externos
- [`docs/product-readiness.md`](docs/product-readiness.md) — evidencia automática y qualification real pendiente
- [`docs/agentes-externos-necesitan-fronteras.md`](docs/agentes-externos-necesitan-fronteras.md) — artículo de diseño sobre la plataforma de agentes externos
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

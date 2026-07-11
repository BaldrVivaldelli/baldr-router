# Durable Baldr-led Orchestration

Baldr v0.16 convierte el workflow congelado `architect-implement-review` en una máquina de estados durable. Los modelos o agentes aportan razonamiento; Baldr conserva el control operativo.

```text
cliente MCP
  -> Baldr durable workflow engine
      -> architecture phase
      -> implementation phase
      -> review phase
      -> bounded fix/review rounds
```

Los participantes no se invocan directamente. Baldr selecciona perfiles, persiste cada transición, transmite artifacts estructurados y decide cuándo reintentar, pausar, reconciliar o completar.

## Perfiles de ejecución abstractos

Baldr no codifica nombres concretos de modelos. Un perfil describe cómo ejecutar una participación:

```toml
[execution_profiles.shared]
provider = "codex"
model = ""
reasoning_effort = ""
session_scope = "workspace"
```

Los campos vacíos heredan los defaults del provider. El mismo perfil puede respaldar las tres fases:

```toml
[roles.architect]
profiles = ["shared"]

[roles.implementer]
profiles = ["shared"]

[roles.reviewer]
profiles = ["shared"]
```

También se admite una cantidad independiente de perfiles por fase: `n` para arquitectura, `m` para implementación y `l` para revisión.

```toml
[execution_profiles.architecture-primary]
provider = "codex"
model = "architecture-model"
reasoning_effort = "high"
session_scope = "workspace"

[execution_profiles.architecture-fallback]
provider = "kiro-cli"
agent = "architecture-agent"
effort = "high"

[execution_profiles.implementation]
provider = "codex"
model = "implementation-model"
reasoning_effort = "medium"
session_scope = "workspace"

[execution_profiles.review-a]
provider = "codex"
model = "review-model-a"
reasoning_effort = "high"

[execution_profiles.review-b]
provider = "kiro-cli"
agent = "review-agent-b"
effort = "high"

[roles.architect]
profiles = ["architecture-primary", "architecture-fallback"]
strategy = "first-success"
min_successes = 1
can_write = false
sandbox = "read-only"

[roles.implementer]
profiles = ["implementation"]
strategy = "first-success"
min_successes = 1
can_write = true
sandbox = "workspace-write"

[roles.reviewer]
profiles = ["review-a", "review-b"]
strategy = "all"
min_successes = 2
can_write = false
sandbox = "read-only"
```

Reglas:

- `first-success` prueba perfiles en orden hasta obtener un resultado válido;
- `all` ejecuta todos los perfiles y consolida sus resultados;
- una fase con escritura no puede usar `all` con múltiples participantes, evitando escritores concurrentes;
- la precedencia es override de la ejecución, perfil del rol y finalmente default del provider;
- el snapshot resuelto se congela al crear el workflow, por lo que un upgrade de configuración no cambia una ejecución ya iniciada.

## Tres planos de durabilidad

```text
Control plane
  SQLite

Code plane
  Git worktree + checkpoints + patch

Artifact plane
  outputs/evidence content-addressed
```

### SQLite

Ruta por defecto:

```text
Linux/WSL:
  ~/.local/state/baldr-router/baldr.sqlite3

Windows nativo:
  directorio de estado local de Baldr
```

La base debe vivir en el filesystem local del runtime, no en una ruta de red ni en `/mnt/c` cuando Baldr corre dentro de WSL.

Configuración:

```toml
[durability]
enabled = true
journal_mode = "WAL"
synchronous = "FULL"
busy_timeout_ms = 5000
lease_seconds = 45
heartbeat_seconds = 5
recovery_on_start = true
artifact_inline_limit_bytes = 32768
retain_terminal_days = 90
```

SQLite mantiene:

- estado materializado de runs, steps, participants y attempts;
- journal append-only de eventos;
- leases y heartbeats;
- sesiones de provider;
- checkpoints de workspace;
- referencias a artifacts y evidence;
- snapshot inmutable de la configuración resuelta.

Las migraciones son monotónicas y llevan checksum. Modificar una migración ya aplicada hace fallar el arranque en vez de alterar silenciosamente el historial.

## Máquina de estados

Estados relevantes del workflow:

```text
pending
running
recovering
interrupted
unknown
awaiting_reconciliation
approved
needs_changes
blocked
failed
cancelled
```

Cada transición actualiza el estado materializado y agrega un evento dentro de la misma transacción SQLite.

Los efectos externos no pueden formar parte de esa transacción. Por eso Baldr no promete `exactly once`; implementa:

```text
at-least-once controlado
+ idempotency keys
+ reconciliation
+ Git checkpoints
```

`unknown` significa que un proceso externo pudo haber producido efectos antes de perderse la confirmación durable.

## Leases, heartbeat y recovery

Cada workflow activo tiene un owner, una expiración y un `lease_epoch` monotónico. Mientras un provider corre, Baldr renueva el lease del workflow y del intento. Cada takeover incrementa el epoch; toda mutación posterior exige el mismo owner/epoch dentro de la transacción, por lo que un worker obsoleto no puede confirmar un resultado después de perder el lease.

Al arrancar:

1. busca leases vencidos;
2. clasifica los pasos activos;
3. un paso read-only pasa a `interrupted` y puede reintentarse;
4. un paso con escritura pasa a `unknown`;
5. el workflow con efectos de escritura inciertos pasa a `awaiting_reconciliation` y no se reintenta ciegamente.

Una ejecución reanudada usa el snapshot original de perfiles, límites, sandbox y versión del workflow, incluso si la configuración actual cambió. El resume también queda ligado a la ruta y a la identidad Git original; mover el run o reemplazar el repo en la misma carpeta se rechaza.

## Sesiones persistentes por perfil

Las sesiones no se comparten indiscriminadamente. La key incluye:

```text
scope + workspace/run + role + provider + model/agent + profile
```

Por ejemplo, arquitectura e implementación pueden usar el mismo provider pero conservar threads distintos. En scope `workspace`, un workflow posterior puede reanudar el thread correspondiente al mismo rol/modelo/perfil.

Los resultados estructurados, no la memoria implícita del thread, son el contrato entre fases:

```text
architecture artifact
implementation report
review report
```

## Git worktrees y checkpoints

Para un repositorio Git limpio y `write_isolation = "auto"`, Baldr crea un worktree detached bajo su directorio de estado.

```text
original repo
  <- patch publication
Baldr worktree
  <- checkpoint commit después de cada write step
```

Cada checkpoint registra:

- base commit;
- checkpoint commit;
- hash del diff;
- patch binario;
- step que produjo el cambio;
- estado `prepared`, `checkpointed` o `published`.

La publicación es idempotente: si Baldr se cae después de aplicar el patch pero antes de persistir `published`, el siguiente intento detecta que el mismo patch ya está presente y reconcilia sin aplicarlo dos veces.

Un workspace sucio, sin primer commit o no-Git usa modo `in-place` cuando la política lo permite. Ese modo conserva fingerprints, pero no ofrece el mismo aislamiento transaccional.

## Idempotencia

Un caller puede enviar `idempotency_key` a `run`. La key queda ligada a un request fingerprint compuesto por workspace/repository identity, workflow version, hashes de task/contexto y snapshot de configuración. La misma key con el mismo fingerprint recupera el workflow; una solicitud distinta devuelve `idempotency_conflict`.

Cada provider attempt también tiene una key derivada de:

```text
run + step + profile + attempt number
```

Esto impide duplicar un intento ya confirmado. Un write attempt `unknown` nunca se repite automáticamente.

## Cancelación durable y reconciliación

La cancelación se materializa antes de terminar procesos:

```text
running -> cancelling -> cancelled
```

Baldr persiste timestamp/reason, termina el process tree del run y marca attempts, participants y steps como `cancelled`. Una solicitud repetida es idempotente y recovery puede completarla si el cliente desaparece.

Un write attempt con efectos inciertos queda `unknown` y el workflow pasa a `awaiting_reconciliation`. El operador puede continuar usando la misma intención `run` con una acción explícita:

```text
resume_from_checkpoint
accept_existing_changes
discard_worktree
mark_failed
```

Baldr inspecciona identidad, HEAD, patch y worktree antes de ofrecer acciones; ninguna escritura incierta se reintenta automáticamente.

## Maintenance, sessions y reducers

El control plane ejecuta integrity/foreign-key checks, backups pre-migration, GC de runs/artifacts, expiración de sesiones y WAL checkpoints. Las sesiones se invalidan por TTL, turn count, identidad de repositorio o versión de provider.

Cuando una fase tiene múltiples participants, Baldr usa reducers determinísticos sobre structured reports. Arquitectura soporta `primary-with-advisors`, `unanimous` y `conflict-blocks`; review soporta `any-blocker`, `all-approved`, `quorum` y `conflict-blocks`. No se invoca otro modelo para consolidar.

## Evidence desde SQLite

Cuando un workflow termina, Baldr genera evidence a partir del journal y el estado durable, no desde memoria efímera.

```text
~/.local/state/baldr-router/evidence/workflow-<run-id>/
  summary.md
  workflow-state.json
  workflow-events.json
  schema.json
  artifact-hashes.json
  manifest.json
```

El bundle no contiene API keys, raw prompts ni código del workspace. Los artifacts privados quedan referenciados y redactados según su nivel.

## Superficie pública congelada

La durabilidad no agrega intenciones de producto:

```text
setup
status
run
```

- `setup` prepara configuración, trust, probe y verificación;
- `status` muestra schema, runs no terminales, recovery y perfiles resueltos;
- `run` crea o reanuda el workflow durable.

Las tools MCP existentes aceptan opcionalmente `idempotency_key` y `resume_run_id`, sin cambiar el workflow, los roles ni los providers congelados.

## Pruebas de crash y upgrade

La suite cubre:

- crash antes y después de cada boundary durable del workflow;
- read-only retry seguro;
- write side effects convertidos a `unknown`;
- snapshot de configuración preservado después de un upgrade;
- sesiones persistentes separadas por role/model/profile;
- migraciones SQLite y checksum;
- journal y estado materializado consistentes;
- publicación Git idempotente;
- evidence reconstruido desde SQLite.

Los tests sintéticos no reemplazan la matriz E2E real de VS Code, WSL y Kiro, pero permiten reproducir fallos de proceso de manera determinística.

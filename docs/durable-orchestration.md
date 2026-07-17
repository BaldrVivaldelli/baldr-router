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
  Git worktree o workspace sombra + checkpoints + publicación idempotente

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
+ checkpoints verificables
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

Una ejecución reanudada usa el snapshot original de perfiles, límites, sandbox y versión del workflow, incluso si la configuración actual cambió. El resume también queda ligado a la ruta original y, según el modo, a la identidad Git o al manifest y la política del workspace sombra. Mover el run, reemplazar el repo o alterar el estado durable se rechaza.

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

## Escritura autorizada y aislamiento compatible

`Pedir autorización` (`automatic`) es el modo recomendado y predeterminado. Baldr conserva exactamente el alcance que eligió la persona:

```text
arquitectura en solo lectura sobre la carpeta seleccionada
  -> si el plan necesita escribir, persiste una decisión del operador
  -> autorizar: implementación directa con workspace-write
  -> no autorizar: cierre durable sin modificar archivos
```

No hay una publicación posterior desde una copia completa. Una vez concedido el permiso, cada cambio aparece directamente en el workspace activo, como en Codex/Kiro. Los cambios independientes de la persona pueden convivir con la ejecución; Git, los checkpoints y el journal registran los efectos observados. Si un intento de escritura se interrumpe antes de confirmar su resultado, Baldr lo deja `unknown` y exige reconciliación en vez de repetirlo.

`current` ofrece el mismo camino directo con consentimiento persistente, sin una pausa por tarea. `non-git` permite escritura directa en una carpeta confiable sin exigir Git y conserva su confirmación explícita. Arquitectura continúa con sandbox `read-only`; implementación y fixes reciben `workspace-write` únicamente cuando el modo ya lo autoriza.

### Worktrees y workspaces sombra compatibles

Las sesiones aisladas creadas con versiones anteriores conservan su snapshot y se reanudan con la semántica original:

```text
raíz exacta de un repositorio Git limpio y con commit
  -> worktree detached administrado por Baldr

Git sucio/sin commit, carpeta sin Git o subcarpeta dentro de otro repo
  -> workspace sombra durable administrado por Baldr
```

Baldr no amplía una subcarpeta seleccionada hasta la raíz Git de un directorio padre. En estos runs aislados, una raíz Git debe tener un commit y estar limpia para obtener el aislamiento por worktree; si está sucia o todavía no tiene commit, el snapshot legado `auto` captura ese estado como baseline de un shadow en vez de bloquear, hacer stash o escribir directamente.

### Raíz Git exacta

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

### Carpeta sin Git o alcance parcial

Para una carpeta sin Git —o una subcarpeta elegida dentro de un repositorio padre— un snapshot aislado legado crea:

```text
<estado-local-de-baldr>/shadow-workspaces/<run-id>/
  tree/                 copia sobre la que trabajan los agentes
    .git/               Git privado auxiliar
  control/
    state.json          estado durable y manifests seleccionados
    journal/            eventos de preparación, checkpoint y publicación
    manifests/          manifests inmutables por contenido
    blobs/              contenido inmutable identificado por SHA-256
```

Esta ubicación pertenece al estado durable de Baldr; no usa `/tmp` y puede abrirse nuevamente después de reiniciar VS Code, el router o la máquina. El Git privado de `tree/` facilita inspección y checkpoints locales, pero no es la fuente de verdad: la autoridad portable son los manifests y blobs identificados por hash.

Baldr toma un manifest inicial, materializa la copia, vuelve a escanear el origen y comprueba que ambos hashes coincidan antes de ejecutar un provider. Arquitectura, implementación, revisión y correcciones reciben `tree/` como workspace. Un adapter `advisory`, un Codex SDK que no demuestre cwd o un sandbox irrestricto se bloquean antes de invocarse; el usuario puede elegir explícitamente un modo directo si acepta esa garantía reducida. La carpeta original queda intacta hasta que la revisión aprueba el resultado o la persona decide aplicar el checkpoint verificado.

Después de cada fase con escritura se crea otro manifest. El delta incluye archivos nuevos, modificados y eliminados, cambios de tipo y modos. Antes de tocar el original, Baldr vuelve a calcular sus hashes. Cada ruta que Baldr modificó debe seguir igual al manifest inicial o coincidir ya con el resultado esperado. Las rutas ajenas al delta se conservan con su estado actual, por lo que el trabajo independiente puede continuar mientras Baldr opera en la copia protegida. Si una misma ruta cambió de ambos lados, la publicación se detiene con un conflicto sin empezar a aplicar el plan.

La publicación guarda primero su plan y su cursor en SQLite y en el journal del workspace sombra. Cada operación de borrar, crear, reemplazar o cambiar modo se registra antes y después del efecto junto con un guard del contenido/identidad del target y sus padres. El guard se vuelve a validar después de registrar el intent y justo antes del efecto, cerrando cambios posteriores al preflight. Por eso un reintento puede omitir rutas ya aplicadas y continuar las restantes sin duplicar cambios. Si un crash ocurre durante una operación, el estado se clasifica como publicación parcial: se conserva la copia y no se ofrece un descarte que pueda dejar efectos sin reconciliar.

Las acciones visibles se calculan desde el estado durable; no todas están disponibles en todos los estados:

```text
Ver la copia protegida              inspect_shadow
Continuar desde la copia protegida  continue_from_shadow
Aplicar los cambios protegidos      apply_shadow_changes
Descartar la copia protegida        discard_shadow
```

`Descartar` sólo se ofrece si la publicación todavía no pudo modificar el original (o existe un rollback verificado). Las mismas opciones aparecen ante un fallo de fase o un review con cambios pendientes. `Aplicar` publica el último checkpoint verificado por decisión explícita; sólo recupera `approved` cuando review ya había aprobado. Ante un conflicto o una publicación parcial, la opción segura es inspeccionar y, cuando el preflight lo permita, reintentar la aplicación idempotente. También se puede marcar el run como fallido conservando su journal y evidencia.

### Exclusiones, límites y portabilidad

El snapshot nunca sigue enlaces simbólicos. Sólo admite enlaces relativos cuyo destino permanezca dentro del workspace; rechaza enlaces absolutos, destinos externos, archivos especiales y reparse points de Windows que no sean enlaces soportados. Los manifests registran el destino de cada enlace y los modos de archivos y directorios. En Linux y macOS se restauran los modos POSIX; en Windows la ejecutabilidad/read-only es de mejor esfuerzo y un sistema que no permita crear el enlace devuelve un error explícito. También se rechazan nombres no portables, colisiones por mayúsculas/minúsculas y rutas reservadas de Windows antes de ejecutar agentes.

Las reglas duras excluyen metadatos `.git`, `.hg` y `.svn` en cualquier profundidad. Así, un repositorio anidado se copia como contenido normal, pero nunca se expone su metadata de control. Por defecto también se excluyen directorios generados como `node_modules`, `.venv`, caches, `dist`, `build` y `target`, además de patrones de binarios intermedios. Existe un piso no reemplazable para `.ssh`, `.aws`, `.gnupg`, `.npmrc`, `.netrc`, claves privadas, `.env` y credenciales; la configuración sólo agrega patrones, salvo una inclusión explícita. Si un agente intenta crear una ruta sensible equivalente, el checkpoint falla de forma visible. Las plantillas como `.env.example` están permitidas. Contenido excluido nunca se infiere como borrado y bloquea la eliminación de un padre si pudiera perderse.

La política aislada queda congelada en esos runs y conserva estos campos de `[workspace]`:

```toml
write_isolation = "auto"
shadow_max_files = 100000
shadow_max_total_bytes = 5368709120
shadow_max_single_file_bytes = 536870912
shadow_max_depth = 64
shadow_max_symlinks = 10000
shadow_exclude_generated = true
shadow_generated_directories = ["node_modules", ".venv", "venv", "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache", ".tox", ".gradle", "target", "dist", "build"]
shadow_secret_patterns = [".env", ".env.*", "*.pem", "*.key", "*.p12", "*.pfx", "credentials.json", "secrets.toml"]
shadow_exclude_patterns = []
shadow_include_patterns = []
```

`shadow_max_files` limita todas las entradas administradas, incluidos directorios. Los patrones generados internos también excluyen extensiones intermedias como `*.pyc`, `*.class` y `*.obj`. `.git`, `.hg` y `.svn` no pueden reincorporarse con `shadow_include_patterns`. Si se supera un límite, Baldr falla antes de iniciar el provider y muestra cuál fue; nunca cae silenciosamente a escritura directa.

### Limpieza, retención y compatibilidad

Tras una publicación verificada, el default elimina el workspace sombra inmediatamente. Si la limpieza falla, el checkpoint queda `cleanup_pending` para que maintenance la reintente sin cambiar el resultado aprobado. La retención es configurable:

```toml
cleanup_successful_shadow_workspaces = true
retain_failed_shadow_workspaces = true
shadow_success_retention_hours = 0
shadow_failed_retention_days = 30
shadow_conflict_retention_days = 90
```

Maintenance valida ownership y estado terminal antes de eliminar una copia; nunca borra un shadow todavía recuperable por inferencia de edad solamente. `cleanup_successful_shadow_workspaces=false` conserva también los aprobados, independientemente de la retención de éxito.

Los work items nuevos usan `automatic`, que se resuelve a escritura `in-place` sólo después de la autorización por tarea. Los valores guardados existentes conservan su semántica: `worktree` sigue siendo el modo Git aislado legado; un snapshot histórico con `write_isolation = "auto"` puede resolver a worktree o shadow; `current` trabaja directamente sobre un repositorio Git y `non-git` es la opción explícita **Sin protección**, con confirmación y sin rollback automático.

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

Un write attempt con efectos inciertos queda `unknown` y el workflow pasa a `awaiting_reconciliation`. El operador puede continuar usando la misma intención `run` con una acción que el core haya habilitado para ese estado:

```text
authorization: authorize_changes | decline_changes
shadow:   inspect_shadow | continue_from_shadow | apply_shadow_changes | discard_shadow
worktree: resume_from_checkpoint | accept_existing_changes | discard_worktree
mark_failed
```

Baldr inspecciona ownership, manifests, cursor y conflictos del shadow, o identidad, HEAD, patch y worktree, antes de ofrecer acciones. Ninguna escritura incierta se reintenta automáticamente y `discard_shadow` se omite si una publicación parcial pudo modificar el original.

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
- copia, checkpoint y publicación sombra idempotente;
- conflicto por hash y crash durante una publicación parcial;
- límites, exclusiones, modos, symlinks y repositorios anidados;
- evidence reconstruido desde SQLite.

Los tests sintéticos no reemplazan la matriz E2E real de VS Code, WSL y Kiro, pero permiten reproducir fallos de proceso de manera determinística.

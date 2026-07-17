# Consistency & Operator Control

Baldr Router v0.16 mantiene el contrato público congelado en `setup`, `status` y `run`, pero endurece el control plane durable para que crashes, takeovers, cancelaciones y reintentos no produzcan estados contradictorios.

## 1. Fencing tokens

Cada lease tiene owner, expiración y un `lease_epoch` monotónico.

```text
worker A -> epoch 7
lease expira
worker B -> epoch 8
worker A intenta persistir -> lease_fence_rejected
```

Todas las mutaciones operativas de runs, steps, participants, attempts, sesiones y checkpoints validan el token dentro de la misma transacción SQLite. El fencing no impide que un efecto externo ocurra justo antes de perder el lease; impide que un worker obsoleto lo confirme como estado vigente. Un owner nuevo debe reconciliarlo.

## 2. Idempotencia ligada a la solicitud

Una `idempotency_key` no identifica solamente un nombre: queda ligada a un fingerprint estable de:

- identidad del workspace y repositorio;
- workflow y versión;
- hash de task y contexto;
- librerías Context7 solicitadas;
- snapshot de configuración y perfiles.

```text
misma key + mismo fingerprint     -> mismo durable run
misma key + fingerprint distinto  -> idempotency_conflict
```

El input privado y el run se crean en una única transacción, evitando artifacts huérfanos cuando una key ya existe.

## 3. Resume estricto

Un run durable solo puede reanudarse contra:

- la ruta original normalizada;
- en modo Git, el mismo common directory y fingerprint de roots/remotes;
- en modo sombra, el mismo ownership, manifest, política congelada y ubicación durable;
- un workspace todavía confiable.

Baldr rechaza el resume si el repo fue reemplazado en la misma carpeta, si el estado del shadow fue manipulado o si un cliente intenta mover el run a otra ruta.

## 4. Cancelación durable

La cancelación sigue esta secuencia:

```text
running -> cancelling
persist cancel_requested_at + reason
terminate provider/process tree
attempts/participants/steps -> cancelled
workflow -> cancelled
```

La solicitud es idempotente. Si el cliente desaparece después de pedirla, recovery puede completar la transición desde SQLite.

## 5. Reconciliación operable

Un write attempt que pierde confirmación durable pasa a `unknown`; el run pasa a `awaiting_reconciliation`. Baldr nunca lo reintenta ciegamente.

Las acciones se calculan desde el checkpoint, el manifest y el cursor de publicación. Para un workspace sombra pueden ser:

```text
inspect_shadow
  devuelve ruta, manifests, delta, conflictos y estado de publicación sin modificar archivos

continue_from_shadow
  restaura el último manifest confirmado y vuelve a ejecutar el paso incierto

apply_shadow_changes
  retoma la publicación idempotente desde el cursor durable después de un nuevo preflight

discard_shadow
  elimina la copia sólo si la publicación no pudo dejar efectos en el original

mark_failed
  termina el workflow preservando journal y evidence
```

Una publicación sombra persiste el plan y marca cada operación como inflight antes de modificar el filesystem; después avanza el cursor. Si el proceso cae entre ambas marcas, recovery trata el original como posiblemente modificado. En ese estado no ofrece `discard_shadow`: sólo permite inspeccionar o reintentar `apply_shadow_changes`, que reconoce operaciones ya terminadas y continúa las restantes. Un conflicto de hashes también conserva el shadow; ninguna escritura incierta se reintenta sin una decisión del operador.

Para worktrees existentes continúan `resume_from_checkpoint`, `accept_existing_changes` y `discard_worktree`. Para el modo legado `non-git` sólo se ofrecen las acciones compatibles con una ejecución directa sin checkpoint restaurable. `status` incluye el diagnóstico y la lista exacta de acciones seguras; la UI no inventa capacidades a partir del nombre del modo.

## 6. Worktrees y workspaces sombra

Si un worktree desaparece, Baldr verifica el repo original y lo recrea desde `checkpoint_commit` o `base_commit`. Si existe pero HEAD, identidad o artifacts no coinciden, bloquea la ejecución y exige reconciliación.

Un workspace sombra se guarda en el directorio de estado local de Baldr, bajo `shadow-workspaces/<run-id>`, nunca en `/tmp`. `tree/` contiene la copia y un Git privado auxiliar; `control/` guarda ownership, estado, manifests SHA-256, blobs y un journal por evento. Los manifests son la autoridad de recuperación: el Git privado no reemplaza la verificación por contenido.

En runs aislados creados con la semántica anterior, una raíz Git exacta y limpia usa worktree. Una raíz Git sucia o sin commit, una carpeta sin Git y una subcarpeta seleccionada dentro de un repositorio padre usan shadow; Baldr no expande el alcance al padre ni obliga a hacer stash. El original permanece sin cambios hasta que review aprueba o la persona elige aplicar el checkpoint verificado.

La política siguiente permanece para flujos aislados explícitos de worktree/shadow:

```toml
[workspace]
dirty_workspace_policy = "reject"
```

`automatic` es el flujo predeterminado de escritura directa con autorización previa por tarea. `current` e `in-place` conservan el consentimiento directo persistente y exactamente la subcarpeta elegida como cwd, aunque Git se encuentre en un padre. `non-git` corresponde a **Sin protección** y exige consentimiento explícito. `worktree`, `auto` de bajo nivel y los shadows se conservan para runs aislados existentes y configuración avanzada.

Antes de crear un shadow, Baldr excluye `.git`, `.hg`, `.svn`, un denylist mínimo no reemplazable de credenciales, patrones sensibles configurados y artefactos generados. Aplica límites visibles de entradas (incluidos directorios), bytes totales, tamaño individual, profundidad y enlaces. Sólo acepta symlinks relativos que permanezcan dentro del alcance; rechaza rutas no portables y reparse points no soportados. Los modos POSIX se conservan donde el filesystem los soporte; Windows usa semántica de permisos de mejor esfuerzo y falla explícitamente si no puede recrear un enlace.

Un cwd no se considera una frontera. En modos protegidos, adapters `advisory`, Codex SDK sin cwd demostrable y sandboxes irrestrictos se bloquean antes de ejecutar; sólo `read-only` o `workspace-write` con enforcement real son aceptados.

## 7. Salud y mantenimiento SQLite

Baldr ejecuta:

```text
PRAGMA quick_check / integrity_check
PRAGMA foreign_key_check
backup transaccional antes de migraciones
retención de terminal runs
retención y limpieza segura de workspaces sombra
GC de artifacts no referenciados
expiración de provider sessions
WAL checkpoint
```

En un maintenance full también crea un backup verificable. La base y los shadows deben vivir en el filesystem local del runtime, especialmente dentro de WSL.

Por defecto, un shadow publicado y verificado se elimina inmediatamente; uno fallido se retiene 30 días y uno con conflicto terminal 90 días. `cleanup_successful_shadow_workspaces`, `retain_failed_shadow_workspaces`, `shadow_success_retention_hours`, `shadow_failed_retention_days` y `shadow_conflict_retention_days` permiten ajustar ese comportamiento. Si `cleanup_successful_shadow_workspaces=false`, maintenance tampoco elimina automáticamente los aprobados. Un conflicto no terminal sigue retenido hasta una decisión segura. Maintenance valida ownership, estado terminal y recuperabilidad antes de borrar.

## 8. Lifecycle de sesiones

Las session keys incluyen scope, workspace/run, role, provider, profile y model/agent. Además se invalidan por:

- TTL;
- máximo de turnos;
- cambio de identidad del repositorio;
- cambio de versión del provider;
- cambio de modelo, que produce una key distinta.

Los artifacts estructurados siguen siendo el contrato entre fases; la memoria de una sesión es una optimización, no la fuente de verdad.

## 9. Reducers determinísticos

Una fase puede tener N/M/L participants. Baldr consolida outputs estructurados sin invocar otro modelo.

Arquitectura:

```text
primary-with-advisors
unanimous
conflict-blocks
```

Revisión:

```text
any-blocker
all-approved
quorum
conflict-blocks
```

Los conflictos quedan explícitos en `resolution.conflicts`; los write roles continúan prohibiendo múltiples escritores concurrentes.

## 10. Pruebas de consistencia

La suite cubre:

- dos owners compitiendo por un lease;
- worker obsoleto intentando escribir después de un takeover;
- conflicto de idempotency fingerprint;
- resume contra ruta o repo distinto;
- cancelación idempotente y materializada;
- acciones seguras de reconciliación calculadas por modo y estado;
- reconstrucción de worktree;
- creación y checkpoint durable de workspaces sombra;
- preflight por hashes y publicación sombra idempotente;
- crash durante una operación y recuperación desde cursor durable;
- conflicto del original y descarte bloqueado después de publicación parcial;
- límites, exclusiones, permisos, symlinks y repositorios anidados;
- integrity, backup, WAL, GC y session expiry;
- reducers con conflicto y quorum;
- random walks de la state machine;
- crash/restart en los boundaries durables de architecture, implementation y review.

Las pruebas sintéticas no sustituyen los E2E reales de Windows/WSL, Remote WSL, Kiro y providers autenticados. Los runbooks permanecen bajo `e2e/` y deben ejecutarse tres veces consecutivas desde estados limpios.

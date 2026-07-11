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
- el mismo Git common directory;
- el mismo fingerprint de roots/remotes del repositorio;
- un workspace todavía confiable.

Baldr rechaza el resume si el repo fue reemplazado en la misma carpeta o si un cliente intenta mover el run a otra ruta.

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

Acciones aceptadas mediante `run`:

```text
resume_from_checkpoint
  descarta efectos no confirmados y continúa desde el último checkpoint durable

accept_existing_changes
  checkpointa el estado existente, registra la decisión del operador y continúa a review

discard_worktree
  elimina el worktree ambiguo, lo reconstruye y vuelve a ejecutar el write step

mark_failed
  termina el workflow preservando journal y evidence
```

`status` incluye el diagnóstico del workspace y las acciones seguras para ese estado.

## 6. Reconstrucción de worktrees

Si un worktree desaparece, Baldr verifica el repo original y lo recrea desde `checkpoint_commit` o `base_commit`. Si existe pero HEAD, identidad o artifacts no coinciden, bloquea la ejecución y exige reconciliación.

El default para workspaces sucios es:

```toml
[workspace]
dirty_workspace_policy = "reject"
```

El modo in-place debe habilitarse explícitamente cuando el aislamiento por worktree no sea posible.

## 7. Salud y mantenimiento SQLite

Baldr ejecuta:

```text
PRAGMA quick_check / integrity_check
PRAGMA foreign_key_check
backup transaccional antes de migraciones
retención de terminal runs
GC de artifacts no referenciados
expiración de provider sessions
WAL checkpoint
```

En un maintenance full también crea un backup verificable. La base debe vivir en el filesystem local del runtime, especialmente dentro de WSL.

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
- las cuatro acciones de reconciliación;
- reconstrucción de worktree;
- integrity, backup, WAL, GC y session expiry;
- reducers con conflicto y quorum;
- random walks de la state machine;
- crash/restart en los boundaries durables de architecture, implementation y review.

Las pruebas sintéticas no sustituyen los E2E reales de Windows/WSL, Remote WSL, Kiro y providers autenticados. Los runbooks permanecen bajo `e2e/` y deben ejecutarse tres veces consecutivas desde estados limpios.

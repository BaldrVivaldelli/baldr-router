# Baldr Console y progreso narrativo — v0.19

Baldr Console es la interfaz principal de Baldr Router en VS Code. Vive en una sección propia de la Activity Bar y evita usar Copilot Chat como panel operativo.

```text
Activity Bar
  -> Baldr
       -> lista de tasks durables
       -> progreso narrativo del item seleccionado
       -> composer fijo
       -> menú +
       -> chips de protección, nivel de detalle, equipo y ayuda adicional
```

La consola es una fachada fina. No abre SQLite ni implementa routing: consume las mismas intenciones versionadas del core:

```text
setup
status
run
```

## Uso diario

Escribí directamente en el composer:

```text
Corregí el refresh de tokens y agregá tests
```

Baldr crea un work item durable y lo inicia con la configuración del workspace. El item sigue disponible después de cerrar VS Code, reiniciar el MCP o actualizar la extensión.

El chat `@baldr` sigue disponible como shortcut opcional, pero Baldr Console es la experiencia principal.

## Menú `+`

El botón `+` usa Quick Picks pequeños, no un formulario:

```text
Nueva tarea para después
Archivo abierto
Texto seleccionado
Protección de cambios
Nivel de detalle
Equipo de Baldr
Crear una opción de equipo
Ayuda adicional
Actualizar estado
Abrir registros
```

## Slash commands

Al escribir `/` aparece autocomplete dentro del composer:

```text
/new <task>
/run [task]
/status
/profile <fast|balanced|deep|custom>
/git <automatic|current|off>
/context <auto|on|off>
/roles
/cancel
/resume
/archive
/restore
/delete
/setup
/help
```

Son aliases de `setup`, `status` y `run`; no amplían el contrato público.

## Chips

Los chips debajo del composer reflejan la configuración persistida del workspace:

```text
Protección automática
Estándar
Equipo estándar
Ayuda automática
```

Cada chip abre un Quick Pick y guarda la selección a través del core.

## Protección de cambios

```text
Protección automática
  recomendada y predeterminada; trabaja sobre una copia protegida y recuperable.

Trabajar directamente
  modifica la carpeta elegida y usa su repositorio Git para revisar los cambios.

Sin protección
  modifica la carpeta directamente, sin exigir Git ni ofrecer recuperación automática.
```

Con **Protección automática**, una raíz Git exacta y limpia usa un worktree; una raíz sucia o sin primer commit, una carpeta sin Git y una subcarpeta seleccionada dentro de otro repositorio usan un workspace sombra durable. Los agentes trabajan únicamente en esa copia. La carpeta original no cambia mientras Baldr planifica, implementa y revisa; después de una revisión aprobada, Baldr vuelve a comprobar los hashes y publica solamente el diff calculado. Si el equipo elegido no ofrece un límite de workspace verificable, Baldr lo informa sin ejecutar ese provider.

**Sin protección** requiere confirmación modal explícita. Después se recuerda para ese workspace como `intentional_non_git`; la excepción se aplica sólo a esa carpeta y no desactiva la política global. El texto de la tarea se conserva si la persona cancela la confirmación.

Las preferencias nuevas usan `automatic`. Las tareas antiguas guardadas como `worktree`, `current` o `non-git` mantienen su comportamiento y pueden seguir reanudándose sin una migración silenciosa.

## Perfiles por fase

`Fast`, `Balanced` y `Deep` son presets. `Custom` permite elegir perfiles para:

```text
architecture:    1..N
implementation:  1..M
review:          1..L
```

La creación de un perfil reutilizable se hace paso a paso mediante Quick Picks e inputs pequeños: nombre, provider, modelo o agente y effort. No existe una página de formulario separada.

## Progreso narrativo

La vista principal evita estados internos y responde cuatro preguntas: qué está haciendo Baldr, qué ya terminó, cuál fue el resultado y si la persona necesita actuar.

```text
Ahora
  Organizando el trabajo | Haciendo los cambios | Comprobando el resultado

Planificación
  resumen, decisiones, supuestos, riesgos y criterios

Ejecución
  resumen, archivos informados, comprobaciones y pendientes

Revisión
  conclusión, hallazgos, correcciones y nueva revisión

Resultado
  cambios, comprobaciones, pendientes y próximos pasos
```

Las tres etapas son acordeones canónicos. Las rondas de corrección permanecen dentro de Ejecución y Revisión en lugar de duplicar tarjetas. La etapa activa se abre automáticamente; la selección de paneles, el foco y el scroll sobreviven a las actualizaciones. Las duraciones reflejan timestamps observados y nunca se muestran porcentajes o estimaciones inventadas.

Los reportes de agentes se proyectan mediante el contrato público `baldr-work-item-progress` v1. La proyección usa una allowlist, límites y redacción: no cruza prompts, razonamiento, stdout/stderr, sesiones, roots privados ni eventos crudos. Proveedor, modelo, intentos, comandos sanitizados y códigos quedan cerrados bajo **Detalles técnicos**; los logs son el nivel final de diagnóstico.

Cuando el runner ofrece actividad en vivo, Baldr conserva sólo categorías seguras (`working`, `analyzing`, `researching`, `changing`, `verifying`) y timestamps. `working` es el estado honesto para el inicio de un rol o un comando genérico; completar un turno o recibir la respuesta del SDK no se presenta como verificación. El contenido del razonamiento, comandos y archivos nunca forma parte de esas observaciones. Al recargar VS Code, la historia se reconstruye desde SQLite y artefactos durables.

Cada reporte puede incluir qué entendió Baldr, alcance, enfoque, pasos,
trabajo completado/siguiente, hallazgos, correcciones y evidencia de
verificación. Esas secciones se muestran sólo después de quedar guardadas en
un reporte estructurado; mientras una ronda está activa no se reutiliza el
resumen de la ronda anterior como si fuera un avance nuevo.

Cada etapa terminada conserva además una **Entrega**. El resumen queda visible
en la tarjeta y **Ver entrega completa** abre un visor protegido y paginado. El
visor no muestra logs ni salida cruda: presenta únicamente interpretación,
alcance, plan, trabajo, hallazgos, correcciones y evidencia ya reducidos por el
core. Las tareas históricas indican con honestidad si sólo existe un resumen o
si el detalle ya no está disponible.

Los 256 descriptores más recientes viajan en el status liviano. Cuando hay más,
aparece **Ver entregas anteriores** y la consola pagina el índice bajo demanda;
por eso ningún intento desaparece y el polling no hidrata documentos grandes.
El visor bloquea el fondo mientras está abierto, admite teclado y `Escape`,
preserva foco/scroll, y descarta respuestas tardías pertenecientes a otra tarea
o entrega.

La consola consulta `facade status --workbench-only`: evita repetir doctor, login, qualification y probes en cada actualización. El polling usa backoff de 2,5 a 10 segundos cuando no cambia la revisión, se detiene con la vista oculta y no reconstruye el contenido sin cambios.

La especificación completa está en [narrative-progress.md](narrative-progress.md). Los contratos canónicos están en `contracts/work-item-progress-v1.schema.json`, `contracts/phase-deliverable-page-v1.schema.json` y `contracts/phase-deliverable-index-page-v1.schema.json`.

## Acciones

Las acciones dependen del estado durable:

```text
Run
Cancel
Archive
Restore
Delete permanently
Resolve
Open logs
```

El historial permite filtrar sesiones activas, finalizadas y archivadas, y buscarlas por título. Archivar es reversible; una sesión archivada se puede restaurar o eliminar permanentemente tras confirmación. El borrado elimina el historial durable y las entregas de Baldr, sin modificar archivos del workspace.

Cuando una escritura queda incierta o la publicación encuentra un conflicto, **Revisar opciones** ofrece únicamente las acciones que el core demostró seguras para ese estado. En un workspace sombra pueden aparecer:

```text
Ver la copia protegida              inspect_shadow
Continuar desde la copia protegida  continue_from_shadow
Aplicar los cambios protegidos      apply_shadow_changes
Descartar la copia protegida        discard_shadow
```

No siempre aparecen las cuatro. También se ofrecen cuando una fase falla o review pide cambios, siempre sobre el último checkpoint verificado. **Aplicar** publica ese checkpoint por decisión explícita y cierra la tarea como aprobada sólo si review ya había aprobado; en otro caso queda como `needs_changes`. Si la publicación pudo aplicar una parte del plan, **Descartar** se oculta para no abandonar cambios en el original; se puede inspeccionar y reintentar **Aplicar**, que continúa desde el journal idempotente. Si otra persona o proceso cambió el original, Baldr muestra el conflicto y conserva la copia. Las acciones legadas de worktree (`resume_from_checkpoint`, `accept_existing_changes` y `discard_worktree`) siguen disponibles para tareas existentes cuando corresponda. **Marcar como fallida** conserva el journal y la evidencia.

## Persistencia

El core agrega estas tablas mediante migraciones SQLite:

```text
workspace_preferences
work_items
work_item_runs
work_item_events
phase_deliverables
```

El texto completo de la task y el contexto privado se guardan como artifacts privados; la fila materializada conserva metadata, estado, referencias y configuración. La extensión nunca accede directamente a la base.

## Seguridad

- VS Code Workspace Trust sigue siendo obligatorio.
- Protección automática admite carpetas sin Git sin autorizar escrituras directas sobre ellas.
- Sin protección requiere consentimiento explícito.
- El workspace sombra vive en el estado durable local de Baldr, no en `/tmp`, y se elimina sólo después de publicar y verificar correctamente o de un descarte seguro.
- `.git`, secretos configurados y artefactos generados se excluyen de la copia; los límites de archivos, bytes, profundidad y enlaces fallan de manera visible.
- Context7 usa SecretStorage de VS Code cuando la key se configura desde la consola.
- La UI escapa contenido antes de renderizarlo.
- Los providers solo reciben workspaces autorizados por el core.
- Cancelación y reconciliación pasan por la state machine durable.

## Superficie visible

```text
Activity Bar:
  Baldr

Command Palette:
  Baldr: Open

Chat opcional:
  @baldr /setup
  @baldr /status
  @baldr /run <task>
```

No se agregan comandos separados para cada operación.

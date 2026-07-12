# Baldr Console y protección automática — v0.18

Baldr Console es la interfaz principal de Baldr Router en VS Code. Vive en una sección propia de la Activity Bar y evita usar Copilot Chat como panel operativo.

```text
Activity Bar
  -> Baldr
       -> lista de tasks durables
       -> detalle y timeline del item seleccionado
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

## Timeline y acciones

Cada item muestra:

```text
Architecture
Implementation
Review
Fix rounds, si corresponden
```

Las acciones disponibles dependen del estado durable:

```text
Run
Cancel
Archive
Resolve
Open logs
```

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

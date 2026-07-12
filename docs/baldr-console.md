# Baldr Console y work items durables — v0.17

Baldr Console es la interfaz principal de Baldr Router en VS Code. Vive en una sección propia de la Activity Bar y evita usar Copilot Chat como panel operativo.

```text
Activity Bar
  -> Baldr
       -> lista de tasks durables
       -> detalle y timeline del item seleccionado
       -> composer fijo
       -> menú +
       -> chips de Git, preset, roles y Context7
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
New draft
Attach current file
Attach selection
Git mode
Execution preset
Role profiles
Create execution profile
Context7
Refresh status
Open logs
```

## Slash commands

Al escribir `/` aparece autocomplete dentro del composer:

```text
/new <task>
/run [task]
/status
/profile <fast|balanced|deep|custom>
/git <worktree|current|off>
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
Git: Worktree
Balanced
A/I/R: default
Context: Auto
```

Cada chip abre un Quick Pick y guarda la selección a través del core.

## Modos de workspace

```text
Git worktree
  recomendado; aislamiento, checkpoints y publicación idempotente.

Current Git workspace
  ejecución in-place; exige un repositorio limpio.

Non-Git workspace
  edición directa con garantías reducidas.
```

El modo non-Git requiere confirmación modal explícita. Después se recuerda para ese workspace como `intentional_non_git`; Baldr no desactiva silenciosamente la protección Git.

Si una tarea se envía desde una carpeta no-Git, Baldr conserva el texto como draft y muestra una elección guiada: abrir una carpeta que sí sea repositorio o confirmar **Non-Git and Run**. No se pierde la tarea ni se muestra el JSON crudo de la policy.

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

Cuando un write step queda `unknown` o `awaiting_reconciliation`, `Resolve` ofrece únicamente las acciones seguras calculadas por el core:

```text
resume_from_checkpoint
accept_existing_changes
discard_worktree
mark_failed
```

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
- Non-Git requiere consentimiento explícito.
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

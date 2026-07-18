schema_version = 2
name = "{{NAME}}"
owner = {{OWNER_LITERAL}}
registry = "{{REGISTRY}}"
namespace = "{{NAMESPACE}}"
version = "1.0.0"
language = "python"
entrypoint = "agent.py"
driver = "baldr.python"
sources = ["agent.py"]
output_dir = "dist"
timeout_seconds = 300
test_command = ["{python}", "-m", "unittest", "discover", "-s", "tests", "-v"]
source_id = "{{NAMESPACE}}.{{NAME}}"

[roles.planner]
agent_name = "{{NAME}}-planner"
capabilities = ["workspace.read", "role.architect"]
effect_mode = "read-only"
label = "{{NAME}} — planificación (solo lectura)"
description = "Prepara el plan sin modificar el workspace."

[roles.writer]
agent_name = "{{NAME}}-writer"
capabilities = ["workspace.read", "workspace.write", "role.implementer"]
effect_mode = "workspace-write"
label = "{{NAME}} — ejecución (escritura)"
description = "Ejecuta la tarea dentro del workspace explícitamente entregado."

[roles.reviewer]
agent_name = "{{NAME}}-reviewer"
capabilities = ["workspace.read", "role.reviewer"]
effect_mode = "read-only"
label = "{{NAME}} — revisión (solo lectura)"
description = "Comprueba el resultado en un snapshot descartable."

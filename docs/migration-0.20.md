# Migración de Baldr 0.19 a 0.20

Baldr 0.20 conserva las intenciones públicas `setup`, `status` y `run`, los
perfiles normales de Codex/Kiro y los proyectos de agentes Python con
`schema_version = 1`. La migración coordina las versiones de Router, Kiro,
Runner, Agent Builder y los SDKs; no requiere mover el código de los agentes al
monorepo.

## Actualizar el runtime

Instalá juntos los wheels `0.20.0` de Router, Agent Builder, Agent Runner y el
SDK Python. La extensión VS Code instala su wheel privado en un runtime
versionado separado, por lo que puede volver a `0.19.0` si la actualización no
completa.

Para TypeScript, el driver requiere exactamente la misma versión del SDK:

```bash
npm install --global \
  ./baldr-agent-sdk-0.20.0.tgz \
  ./baldr-agent-builder-typescript-0.20.0.tgz

baldr-agent driver doctor baldr.typescript
```

No reutilices un driver `0.19.x` con el SDK `0.20.x`. El descubrimiento fija
`id + version + digest` y falla si hay más de una identidad compatible sin una
selección explícita.

## Comprobar un repositorio de agentes

Desde el repositorio externo:

```bash
baldr-agent test
baldr-agent driver conformance baldr.typescript --driver-version 0.20.0
baldr-agent build
baldr-agent publish
baldr-agent doctor
```

Para Python, reemplazá el id por `baldr.python`. Durante una actualización,
`--driver-version` permite comprobar 0.20 aunque el ejecutable 0.19 continúe en
otra entrada de `PATH`; también puede fijarse `--driver-digest`. La conformidad
usa el proyecto real como fixture y no modifica su versión publicada.

## Probar antes de publicar

`baldr-agent run` crea una release efímera de desarrollo, ejecuta el rol exacto
mediante Agent Runner y descarta la instalación temporal al terminar:

```bash
baldr-agent run \
  --role implementer \
  --workspace /ruta/al/workspace \
  --request "Generá el informe" \
  --driver-version 0.20.0
```

El workspace es obligatorio. Planner y reviewer reciben snapshots de solo
lectura; implementer recibe únicamente la raíz explícita y requiere las
capacidades `workspace.read` y `workspace.write` en el manifiesto.

## Rollback

Las referencias publicadas siguen siendo inmutables. Una actualización debe
cambiar la versión del agente; modificar contenido bajo un `AgentRef` existente
es rechazado. Para reactivar una versión anterior:

```bash
baldr-agent rollback 1.0.0
```

Antes de promover 0.20, ejecutá `python scripts/dev.py build` y
`python scripts/dev.py verify-release`. La evidencia de distribución
TypeScript registra conformidad, ejecución directa, ejecución por la fachada
compartida de VS Code/Kiro, actualización y rollback. La publicación a PyPI o
npm sólo ocurre mediante `workflow_dispatch` y el selector explícito del
registry correspondiente.

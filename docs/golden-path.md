# Golden path de Baldr

Este es el recorrido recomendado para usar Baldr como producto. No requiere
editar `mcp.json`, JSON ni TOML. Las opciones avanzadas continúan disponibles,
pero no forman parte del primer uso.

## 1. Completar una primera tarea en VS Code

Requisitos:

- Python 3.11 o posterior en Windows, WSL, Linux o macOS;
- Codex instalado y autenticado en el entorno elegido por Baldr;
- un repositorio Git abierto en VS Code.

Recorrido:

1. Instalá `baldr-router-vscode-0.20.0.vsix` y recargá la ventana si VS Code lo
   solicita.
2. Abrí el repositorio y aceptá **Workspace Trust**. Esa decisión autoriza el
   workspace; Baldr no vuelve a pedir permiso antes de cada tarea.
3. Esperá a que VS Code termine de iniciar. Baldr prepara el runtime en segundo
   plano y ofrece abrir la consola; también podés abrir **Baldr** desde la
   Activity Bar.
4. Si el estado indica que Codex no está autenticado, ejecutá `codex login` en
   el entorno informado por Baldr y actualizá el estado.
5. Escribí la tarea en el compositor y presioná Enter.

El recorrido predeterminado queda fijado por el core:

```text
planificación  -> solo lectura
implementación -> escritura directa en el workspace confiado
revisión       -> comprobación del diff y del resultado
```

No hace falta elegir un modo Git, un preset, un modelo ni un equipo para la
primera tarea. Al terminar, el resultado separa archivos agregados,
modificados y eliminados. Cada archivo existente se puede abrir desde su fila.

## 2. Cancelar, reiniciar y continuar

- **Cancelar:** usá la acción visible de la sesión. Baldr termina el proceso y
  sus descendientes y persiste el estado terminal.
- **Reiniciar:** cerrá o recargá VS Code y volvé a abrir Baldr. La sesión se
  recupera desde SQLite; el webview no es la fuente de verdad.
- **Continuar:** escribí un nuevo pedido sobre una sesión terminada para crear
  otro turno durable.
- **Reintentar:** Baldr lo ofrece únicamente cuando la evidencia persistida
  dice que el intento es repetible.
- **Reconciliar:** si una interrupción pudo dejar escrituras parciales, Baldr
  no repite el paso a ciegas. Muestra solamente las acciones demostradas como
  seguras por el estado durable.

## 3. Integración opcional de Kiro (fuera del gate v0.20)

Kiro conserva el mismo contrato y sus paquetes siguen verificados, pero no es
un segundo golden path de esta iteración. Su cliente real no participa de la
qualification ni puede satisfacer el gate de promoción v0.20; estas
instrucciones preservan la integración disponible para una qualification
posterior.

Descargá y extraé `baldr-router-0.20.0-artifacts.zip`. Desde el directorio
extraído, instalá Router y el adapter en el mismo entorno donde se ejecutará
el core (el host o la distribución WSL elegida):

```bash
cd artifacts/python
uv tool install --force ./baldr_router-0.20.0-py3-none-any.whl \
  --with ./baldr_kiro_adapter-0.20.0-py3-none-any.whl \
  --with-executables-from baldr-kiro-adapter
cd ../..
```

En el host que inicia Kiro y sus servidores MCP —normalmente Windows
PowerShell— instalá el launcher incluido en el mismo release:

```bash
npm install --global \
  ./artifacts/node/baldr-router-launcher-0.20.0.tgz
baldr-router-launcher detect
```

`detect` debe informar Router `0.20.0` en el host o en WSL. Después:

1. extraé `artifacts/baldr-orchestrator-kiro-0.20.0.zip` e instalá el Power
   local desde el directorio `baldr-orchestrator/` resultante usando
   **Powers → Add Custom Power → Import power from a folder → Install**. No
   copies el contenido directamente dentro de `~/.kiro/powers/installed`:
   Kiro registra el MCP namespaced en su configuración únicamente durante la
   instalación;
2. iniciá el setup compartido desde Kiro;
3. aceptá la confianza del workspace;
4. ejecutá la tarea normalmente.

Kiro usa las mismas intenciones `setup`, `status` y `run`, los mismos defaults
y el mismo estado durable que VS Code. El adapter crea hooks administrados de
forma idempotente; no hace falta mantener una configuración MCP a mano.

Si **Kiro - MCP Logs** informa `excluded by registry-only access mode`, la
cuenta está bajo una política organizacional que admite únicamente servidores
del MCP Registry. Baldr no intenta evadir esa política. La organización debe
permitir Powers locales o publicar/autorizar Baldr en su registry; como
alternativa de desarrollo, usá un perfil de Kiro sin esa restricción. Este
estado no cuenta como qualification aprobada.

## 4. Crear y publicar un agente externo

La extensión de VS Code no necesita la toolchain de autoría para usar Codex o
Kiro. Instalá esta parte solamente en el entorno donde vas a desarrollar
agentes. Desde el directorio donde extrajiste el ZIP de artefactos:

```bash
cd artifacts/python
uv tool install --force ./baldr_agent_runner-0.20.0-py3-none-any.whl \
  --with ./baldr_agent_sdk-0.20.0-py3-none-any.whl \
  --with ./baldr_agent_builder-0.20.0-py3-none-any.whl \
  --with ./baldr_router-0.20.0-py3-none-any.whl \
  --with-executables-from baldr-agent-builder \
  --with-executables-from baldr-router
cd ../..
```

Para TypeScript, instalá además el driver publicado:

```bash
npm install --global \
  ./artifacts/node/baldr-agent-sdk-0.20.0.tgz \
  ./artifacts/node/baldr-agent-builder-typescript-0.20.0.tgz
```

Creá el proyecto sin escribir configuración manual:

```bash
baldr-agent init ./my-agent \
  --name my-agent \
  --owner my-team \
  --namespace product \
  --language python

cd my-agent
baldr-agent test
baldr-agent driver conformance baldr.python
baldr-agent run \
  --role implementer \
  --workspace ../demo-workspace \
  --request "Create the requested result"
baldr-agent build
baldr-agent publish
baldr-agent doctor
```

Para TypeScript cambiá únicamente `--language python` por
`--language typescript` y la conformidad por `baldr.typescript`.

Después de publicar, el modo de equipo predeterminado descubre agentes
compatibles automáticamente. VS Code también permite fijarlos por etapa desde
**Equipo → Agentes disponibles**. El resultado de la sesión muestra la
identidad y versión exactas utilizadas.

## 5. Actualizar y volver atrás

Una versión publicada es inmutable. Cambiá el código, fijá la nueva versión sin
editar TOML, publicá nuevamente y verificá el release:

```bash
baldr-agent version 1.1.0
baldr-agent test
baldr-agent build
baldr-agent publish
baldr-agent doctor
```

Para reactivar una versión anterior sin borrar la nueva:

```bash
baldr-agent rollback 1.0.0
```

Publicar contenido diferente con la misma versión falla y pide incrementar la
versión; nunca reemplaza silenciosamente el agente ya utilizado por sesiones
durables.

## 6. Cuando algo falla

Empezá por la superficie donde trabajás:

- VS Code: actualizá Baldr y abrí **Detalles técnicos** sólo si la acción
  principal no alcanza;
- Kiro: ejecutá el status compartido y comprobá el adapter;
- agente externo: ejecutá `baldr-agent doctor` dentro de su proyecto;
- runtime: ejecutá `baldr-router verify --mode full`.

La validación automática demuestra contratos y lifecycle, pero no certifica
por sí sola una instalación real. En esta iteración, la promoción requiere un
receipt `qualified` del recorrido VS Code Remote WSL + Codex descrito en
[`real-environment-qualification.md`](real-environment-qualification.md).
Kiro continúa soportado y documentado, pero su qualification real queda para
la siguiente iteración.

Para ejecutar el gate desde el cliente real, abrí **Baldr → + → Calificar VS
Code + Codex**. La extensión selecciona el perfil Remote WSL, registra el
client receipt, ejecuta tres pasadas con provider smoke y abre los archivos de
assertions y canarios que todavía necesiten evidencia observada.

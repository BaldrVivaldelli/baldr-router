# Baldr Router for VS Code

Fachada nativa de VS Code para el runtime MCP agnóstico **Baldr Router**.

## Instalación A UN SOLO clic con bootstrap automático

Instalá la extensión desde Marketplace o el `.vsix` local. La extensión:

1. registra Baldr Router programáticamente como MCP;
2. prepara un runtime Python privado y versionado desde el wheel incluido;
3. verifica versión y SHA-256 antes de reutilizar el runtime;
4. detecta Windows, WSL, Remote WSL, Linux y macOS;
5. usa WSL solo cuando el host no puede ejecutar Baldr;
6. conserva el runtime actual y una versión anterior para upgrades seguros;
7. expone una sola entrada de Command Palette: **Baldr: Open**;
8. permite configurar provider, modelo y esfuerzo de `architect`, `implementer` y `reviewer`;
9. expone `@baldr` con `/setup`, `/status` y `/run`.
10. ejecuta y cachea un lifecycle self-test rápido durante el warm-up;
11. perfila únicamente workspaces confiables mediante una inspección estática y acotada;
12. genera evidence bundles redactados para instalación, ejecución, cancelación, reinicio y upgrades.

No requiere editar `mcp.json`, instalar un launcher global ni ejecutar `uv tool install`. Debe existir Python 3.11+ en el host de ejecución o dentro de WSL. La autenticación de providers, como `codex login`, sigue siendo un consentimiento explícito.

## Workspace Trust

La extensión declara que no soporta workspaces no confiables. Antes de setup o run sobre una carpeta, VS Code debe otorgar **Workspace Trust**. La extensión pasa únicamente las carpetas abiertas y confiables como roots temporales al core; el router mantiene además su propia política de trusted workspaces.

## Uso diario

```text
Baldr: Open

@baldr /setup
@baldr /status
@baldr /run Implement the requested feature
@baldr Implement the requested feature
```

`Baldr: Open` concentra setup, status y run. Recuperación y diagnóstico se muestran en el canal de salida **Baldr Router**, sin agregar más comandos públicos.

## Perfiles de ejecución

Abrí **Baldr: Open** y elegí **Configure execution profiles**. El asistente
guía la selección de provider para `architect`, `implementer` y `reviewer`.
Para Codex permite conservar el modelo por defecto o escribir un override de
modelo, y seleccionar el razonamiento por fase. Para Kiro CLI permite elegir
agent y effort. La configuración se guarda como perfiles inline del router y
se aplica a las ejecuciones posteriores.

`@baldr /status` muestra el perfil efectivo de cada fase y ofrece el botón
**Configure architect, implementer, and reviewer**, que abre el mismo
asistente.

## Orquestación durable

La extensión no implementa estado o routing propio. `@baldr /run` usa el core durable, que persiste workflows en SQLite, separa sesiones por role/model/profile y usa worktrees/checkpoints para fases de escritura. `@baldr /status` muestra schema, runs activos, recovery y perfiles resueltos mediante el contrato compartido.

La superficie visible no cambia:

```text
setup
status
run
```

## Probe, verificación y evidencia

La extensión no implementa un scanner ni un self-test paralelo. Reutiliza el mismo core que usan Kiro y los clientes MCP genéricos:

```text
Baldr Probe   -> entorno + perfil bounded del workspace confiable
Baldr Verify  -> lifecycle fixture determinístico
Baldr Lab     -> repetición consecutiva en entornos reales
Baldr Evidence-> bundle redactado y verificable
```

`@baldr /status` y **Baldr: Open → Status** muestran el último estado de verificación, el perfil del workspace y el evidence ID. Los tests reales de VS Code Windows/WSL y Remote WSL se documentan bajo `e2e/`.

## Context7 opcional

El setup pregunta primero si se desea habilitar Context7. La key se guarda mediante VS Code `SecretStorage` y se inyecta solo en el environment del proceso. No se escribe en workspace, settings, `mcp.json`, chat, telemetría ni metadata del cache. La salida de extensión y runtime aplica redacción defensiva.

## Cancelación y cleanup

Cuando se cancela una ejecución desde VS Code, la extensión termina el grupo/árbol de procesos del facade. El core aplica la misma política a Codex, Kiro CLI y app-server para timeouts, señales y cierre del runtime.

## Arquitectura

La extensión es una fachada fina. Selección de providers, asignación de roles, workflow, Context7, telemetría y verification gates permanecen en el core Python y el contrato `facade-v1`.

La extensión empaqueta:

```text
resources/runtime/baldr_router-0.16.1-py3-none-any.whl
runtime/runtime-bootstrap.mjs
runtime/baldr-bootstrap.mjs
```

El bootstrap es exactamente el mismo que usa el launcher standalone.

## Build

Desde la raíz del repositorio:

```bash
make build
```

O solo la extensión, luego de copiar el wheel del core a `resources/runtime/`:

```bash
make extension-install
make extension-check
make extension-test
make extension-package
```

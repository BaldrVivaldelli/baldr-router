# Baldr product-readiness audit

Esta matriz conecta el golden path con evidencia verificable. Una prueba
sintética demuestra el contrato indicado, pero nunca reemplaza la qualification
del cliente real.

| Resultado de producto | Evidencia automatizada | Evidencia real requerida |
| --- | --- | --- |
| Bootstrap e instalación limpia | release bootstrap, wheels aislados y VSIX verificado | perfil obligatorio VS Code Remote WSL + Codex en `e2e/REAL_ENVIRONMENT_MATRIX.md` |
| Default directo sin autorización por tarea | tests de workflow, work items, fachada y consola | tarea real desde VS Code con Codex |
| Contrato común VS Code/Kiro/CLI/MCP | facade conformance y ejecuciones instaladas de ambas fachadas | VS Code obligatorio; Kiro conservado y diferido |
| Cancelación sin procesos huérfanos | process-control, launcher y lifecycle verification | cancelación iniciada desde la UI |
| Reinicio y recuperación durable | contrato SQLite descartable ejecutado tres veces por el Lab, crash/restart y deliverables | recarga del cliente y reinicio del runtime |
| Escritura incierta reconciliable | Lab real verifica `unknown` sin reintento ciego; durable recovery y operator-control cubren acciones | canary real de interrupción durante escritura |
| Resumen agregado/modificado/eliminado y navegación | progress contract, presentation y webview behavior | inspección del diff generado por una tarea real |
| Lifecycle externo Python | distribución instalada, selección por ambas fachadas, update y rollback en `dist/validation/python-distribution.json` | selección y ejecución desde VS Code |
| Lifecycle externo TypeScript | distribución instalada, selección por ambas fachadas, update y rollback en `dist/validation/typescript-distribution.json` | selección y ejecución desde VS Code |
| Qualification honesta | synthetic qualification exige `provisional` y el release verifica el digest | receipt `qualified` de VS Code Remote WSL + Codex |

## Estado de v0.20

La cobertura automática demuestra el recorrido instalable, los contratos, la
recuperación y los dos lenguajes. La qualification real de promoción continúa
pendiente hasta obtener el receipt `qualified` de VS Code Remote WSL + Codex.
Por lo tanto, un build local o de CI puede declararse `provisional`, nunca
`qualified`. Kiro conserva su compatibilidad existente, pero su validación UI
real pertenece a una iteración posterior y no bloquea v0.20.

### Evidencia real parcial disponible

- VS Code Remote WSL activó la fachada 0.20 mediante `onStartupFinished`, creó
  el runtime privado instalado y registró un client receipt 0.20 sin abrir la
  vista de Baldr. La qualification provisional más reciente,
  `br-qualification-vscode-remote-wsl-20260719T173455Z-2690fd65`, verificó el
  environment match, tres pasadas consecutivas del lifecycle lab y tres
  provider smokes reales; su receipt SHA-256 es
  `6f89e16f114bf04ad248becfcfbc37d4c326eb8f1ba501e72cd46a4f4fd25f46`.
- Los diez canaries reales pasaron sobre dos repositorios, con run ID, evidence
  ID, pruebas reproducibles, cero procesos huérfanos y las seis invariantes en
  cada tarea. El Lab `br-lab-20260719T173455Z-e57c5a77d6` también ejercitó las
  diez acciones de reconciliación sobre runs durables independientes. Después
  de observar instalación, recarga, progreso, privacidad, la acción **Indicar
  correcciones** y la navegación desde la tarjeta de cambios al archivo exacto,
  el receipt registró 30 de 35 assertions aprobadas. La evidencia sigue siendo
  `provisional`: faltan cinco observaciones reales y un nuevo receipt antes de
  aprobar la fila de promoción.
- El launcher Windows 0.20 completó el handshake MCP hacia el Router en WSL con
  27 tools y 3 prompts. El cliente Kiro de este entorno no puede consumirlo
  porque una política empresarial `registry-only` excluye Powers locales; el
  transporte real funciona, pero la qualification de la UI continúa pendiente.

# Feature Freeze — línea de estabilización v0.19

Baldr Router permanece bajo **congelación funcional**. v0.19 agrega una proyección narrativa y una presentación accesible del progreso durable sin ampliar providers, roles, workflows ni las intenciones públicas `setup/status/run`; la protección automática de v0.18 continúa vigente.

## Superficie congelada

- MCP server sobre stdio;
- launcher opcional host-first con fallback automático a WSL;
- providers: Codex CLI y Kiro CLI opcional;
- roles: architect, implementer y reviewer;
- workflow: `architect-implement-review`;
- tools directas: `delegate_task` y `review_current_diff`;
- Context7/cache opcional;
- structured reports y telemetría local;
- SQLite durable state, event journal, lease fencing, cancelación y recovery;
- execution profiles abstractos 1-for-all o n/m/l por fase;
- Git worktrees/checkpoints e evidence durable;
- runners de Codex: `exec-json` estable; `app-server` y `sdk` experimentales;
- facade intents versionadas: `setup`, `status`, `run`;
- fachadas finas para Kiro, VS Code nativo, VS Code Agent Plugin y MCP genérico.

La lista exacta de tools, prompts, providers, roles y workflows está declarada en `baldr_router.release_policy`. `router/tests/test_frozen_surface.py` falla si esa superficie cambia accidentalmente.

## Cambios permitidos durante el freeze

- correcciones de bugs y seguridad;
- trusted workspace enforcement;
- manejo de timeouts, cancelación y procesos hijos;
- validación E2E, Baldr Lab, workspace probing seguro y evidence bundles;
- mejoras de rendimiento que preserven contratos;
- packaging, upgrades, Windows/WSL y compatibilidad de rutas;
- fachadas nativas que traduzcan UX al contrato congelado;
- documentación, diagnósticos y migraciones SQLite;
- state machine, request fingerprints, reconciliation, recovery y tests de concurrencia/crash/upgrade;
- refactors internos sin cambio de comportamiento.
- read models y proyecciones de presentación aditivas, acotadas y redactadas, sin cambiar la superficie de orquestación congelada.
- turnos conversacionales durables y acciones internas de `run` para continuar un work item, sin agregar intenciones públicas.

## Cambios postergados

- nuevos providers o familias de modelos;
- nuevos roles o workflows;
- delegación autónoma provider-to-provider;
- dashboard o GUI standalone;
- nuevos transports del core;
- rondas agenticas ilimitadas;
- lógica de dominio específica de clientes dentro del core.

## Criterio de salida del Release Candidate

1. Build reproducible de wheel, adapter, launcher, VSIX, Power y Agent Plugin.
2. Pruebas automáticas de trusted workspaces, recursión, errores de Codex, structured output, redacción de secretos y limpieza de procesos.
3. Conformidad semántica de `setup/status/run` entre CLI, MCP y fachadas.
4. Validación real de VS Code Windows + WSL automático.
5. Validación real de VS Code Remote WSL.
6. Validación real de Kiro Power + adapter.
7. Upgrade v0.18 → v0.19 sin destruir el runtime anterior si falla la instalación nueva.
8. Cancelación sin procesos huérfanos.
9. Context7 sin secretos en logs, configuración, telemetría ni cache.
10. Diez tareas representativas completadas en al menos dos repositorios Git reales sin edición técnica manual de la integración.
11. Crash/restart determinístico en cada boundary durable, con read-only retry seguro y write attempts inciertos en `unknown`.
12. Snapshot de perfiles preservado durante upgrade, sesiones separadas por role/model/profile y publicación Git idempotente.
13. Worker obsoleto bloqueado por lease epoch después de takeover.
14. Cancelación durable y las cuatro acciones de reconciliación verificadas.
15. SQLite integrity/backup/GC, session invalidation y worktree reconstruction verificados.

Los tests sintéticos son necesarios pero no reemplazan los runbooks de entorno real bajo `e2e/`. Cada perfil obligatorio debe pasar tres veces consecutivas desde un estado limpio.


## Permitido en v0.19.x Real Environment Qualification

- qualification profiles, operator assertions and canary receipts;
- CI, SBOM, provenance, checksums and release hygiene;
- split source/artifact/evidence packaging;
- deterministic structured decision conflict detection;
- E2E corrections that do not add providers, roles, workflows, MCP tools or facade intents.

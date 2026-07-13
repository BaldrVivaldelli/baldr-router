# Progreso narrativo de BALDR

La consola debe permitir que una persona, sin conocer agentes, modelos ni estados internos, responda rápidamente cuatro preguntas:

1. ¿Qué está haciendo BALDR ahora?
2. ¿Qué ya terminó?
3. ¿Cuál fue el resultado?
4. ¿Necesita algo de mí?

La experiencia no representa un porcentaje ni una estimación ficticia. Se reconstruye exclusivamente desde pasos, reportes y eventos durables.

## Jerarquía de información

La vista usa divulgación progresiva:

```text
Qué sucede ahora
  -> título y explicación en lenguaje cotidiano
  -> última actividad durable

Etapas
  -> Planificación: qué entendió y qué decidió
  -> Ejecución: qué hizo y cómo lo comprobó
  -> Revisión: qué verificó y qué encontró

Resultado
  -> cambios, comprobaciones, pendientes y próximos pasos

Detalles técnicos
  -> perfiles, intentos, comandos, códigos y registros
```

La etapa activa se abre automáticamente. Las etapas finalizadas conservan su resumen y sus rondas de corrección; las futuras explican qué ocurrirá. La expansión elegida por la persona, el foco y el scroll deben sobrevivir a las actualizaciones de estado.

## Wording visible

| Actividad | Título | Explicación |
| --- | --- | --- |
| Borrador | Todavía no empezó | La tarea está guardada y lista para comenzar. |
| Trabajo en curso | Trabajando en la etapa | BALDR todavía no tiene un avance confirmado para mostrar. |
| Planificación | Organizando el trabajo | BALDR está entendiendo tu pedido y armando un plan. |
| Ejecución | Haciendo los cambios | BALDR está trabajando según el plan acordado. |
| Corrección | Ajustando el resultado | BALDR está corrigiendo lo que encontró la revisión. |
| Revisión | Comprobando el resultado | BALDR verifica que los cambios cumplan tu pedido y funcionen correctamente. |
| Publicación | Guardando el resultado | El trabajo terminó y se está aplicando de forma segura. |
| Finalizado | Trabajo listo | Los cambios fueron realizados y revisados. |
| Intervención | Necesitamos que elijas cómo continuar | El trabajo está preservado; revisá las opciones disponibles. |
| Cancelado | Trabajo cancelado | BALDR dejó de trabajar en esta tarea. |

Estados operativos como `dispatching`, `running`, `succeeded`, `unknown` o `awaiting_reconciliation` no son copy de producto. El core los proyecta a estados semánticos y la extensión los presenta en español.

## Contrato público

`WorkItemService.get()` agrega `progress` sin quitar `phases`, `workflow` ni `timeline` de la superficie compatible. La proyección tiene contrato y versión propios:

```json
{
  "contract": "baldr-work-item-progress",
  "version": 1,
  "revision": 1783866960000001,
  "overall_state": "running",
  "activity": {
    "kind": "reviewing",
    "message": "...",
    "since": "..."
  },
  "active_stage": "review",
  "last_event_at": "...",
  "stages": [],
  "final_report": null,
  "attention": null,
  "milestones": [],
  "technical": {}
}
```

Las tres etapas canónicas son `planning`, `execution` y `review`. Las rondas adicionales viven en `history`; no crean tarjetas duplicadas. El estado de ejecución de una fase y su resultado semántico son diferentes: una revisión puede ejecutarse correctamente y, aun así, pedir cambios.

La proyección se construye por allowlist y aplica límites de texto/listas. Nunca incluye prompts, razonamiento, stdout/stderr, sesiones, leases, tokens de reanudación, rutas privadas del estado ni eventos crudos. Los archivos visibles deben ser rutas relativas contenidas en el workspace.

Los reportes actuales pueden aportar secciones narrativas explícitas, todas
acotadas y redactadas antes de cruzar el límite público:

```text
interpretation          qué entendió BALDR
scope / approach        alcance y enfoque elegido
plan_steps              pasos acordados
work_completed          trabajo terminado
work_next               trabajo que todavía falta
findings                hallazgos de revisión
corrections             correcciones realizadas
verification_evidence   comprobaciones observables y sus resultados informados
```

Son aditivas: una tarea histórica sin estas secciones conserva su `summary` y
las listas anteriores. Ninguna sección representa razonamiento interno ni una
transcripción del análisis.

## Entregas durables por etapa

Cada Planificación, Ejecución y Revisión terminal materializa una entrega
estructurada, redactada y ligada a la tarea. No se obtiene parseando logs ni
expone prompts, razonamiento, respuestas crudas, participantes, rutas de estado
o identificadores de artefactos. La entrega sobrevive reinicios y la limpieza
del run que la produjo.

La vista de estado sólo transporta descriptores recientes y un
`deliverable_index` con `total`, `returned`, `truncated`, cursor opaco y acción
de lectura. Si existe más historia, la consola muestra **Ver entregas
anteriores** y pagina el índice únicamente bajo demanda. Así todos los intentos
y rondas siguen siendo inspeccionables sin convertir cada polling en una carga
de documentos completos.

Los contratos públicos tienen responsabilidades separadas:

- `baldr-work-item-progress` describe el estado narrativo y los selectores;
- `baldr-phase-deliverable-index-page` pagina descriptores históricos;
- `baldr-phase-deliverable-page` pagina el contenido seguro de una entrega;
- `baldr-phase-deliverable` representa el documento durable interno y no es la
  respuesta del façade.

Una entrega histórica inválida, ausente o demasiado extensa se declara como
`summary_only` o `unavailable`; la interfaz nunca inventa detalles. Si el
resumen no fue conservado, el wording lo dice expresamente. Cursores y
respuestas están ligados a workspace, tarea, digest y solicitud para impedir
que una respuesta tardía aparezca sobre otra tarea.

## Hitos en vivo

Todos los providers producen una observación genérica al iniciar una fase. Cuando el runner ofrece eventos tipados durante la ejecución, BALDR los reduce a categorías públicas:

```text
working
analyzing
researching
changing
verifying
```

`working` significa únicamente que el rol o un comando está activo. Un evento
de archivo puede elevarlo a `changing`; sólo un evento de prueba o comprobación
tipado puede elevarlo a `verifying`. Iniciar un implementador, iniciar un
revisor, ejecutar un comando genérico, recibir la respuesta final del SDK o
observar `turn.completed` no demuestra por sí solo un cambio ni una
verificación.

Sólo se persisten categoría, estado, etapa, timestamp y nivel de evidencia. No se conserva texto de razonamiento, comandos, contenido de archivos ni argumentos de herramientas. Las observaciones se deduplican y limitan para evitar que el journal crezca sin control, y no se convierten en hitos completados.

## Evidencia honesta

La UI diferencia implícitamente tres fuentes:

- `reported`: el agente lo informó en su reporte estructurado;
- `observed`: BALDR observó una transición o actividad del runner;
- `verified`: BALDR obtuvo evidencia determinista, por ejemplo una publicación o checkpoint validado.

Una entrada textual en `tests_run` significa “el agente informó esta comprobación”; no debe convertirse en “pasó” si el contrato no conserva un resultado verificable.

## Actualización eficiente

El polling visible usa `facade status --workbench-only`. Esta ruta consulta el estado durable y evita diagnóstico de providers, login, qualification, probes y lifecycle checks. La revisión de progreso permite evitar reconstruir el DOM cuando nada cambió. La vista oculta no consulta y una implementación puede aplicar backoff mientras la revisión permanezca estable.

El status completo sigue siendo el default para CLI y clientes existentes.

## Accesibilidad

- La actividad actual usa `aria-live="polite"`.
- Una decisión urgente usa `role="alert"`; los cambios normales no.
- Los acordeones exponen `aria-expanded` y funcionan con teclado.
- El visor de entregas es modal: inmoviliza el fondo, encierra el foco, cierra
  con `Escape` y devuelve el foco al control que lo abrió.
- Estado e importancia nunca dependen sólo del color.
- Debe funcionar sin scroll horizontal desde 240 px, con zoom del 200 % y temas de alto contraste.
- `prefers-reduced-motion` desactiva pulsos y transiciones decorativas.

## Escenarios obligatorios

La matriz de pruebas cubre: borrador, ejecución larga, finalización correcta, revisión con hallazgos, corrección y segunda revisión, intervención humana, conflicto de publicación, error reintentable/no reintentable, cancelación, archivado, recuperación después de reinicio y datos legacy incompletos. También inyecta HTML, secretos, rutas absolutas y reportes sobredimensionados para verificar escape, redacción y límites; fuerza más de 256 entregas, páginas fuera de orden y cambios de tarea para comprobar historia completa y aislamiento.

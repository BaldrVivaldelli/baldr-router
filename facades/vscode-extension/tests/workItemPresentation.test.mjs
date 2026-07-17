import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

import { buildWorkItemPresentation, formatStageDuration } from '../dist/workItemPresentation.js';

const repositoryRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..', '..', '..');

const emptyReport = (overrides = {}) => ({
  status: 'planned',
  summary: '',
  interpretation: '',
  scope: [],
  approach: [],
  plan_steps: [],
  work_completed: [],
  work_next: [],
  findings: [],
  corrections: [],
  verification_evidence: [],
  changes_added: [],
  changes_modified: [],
  changes_removed: [],
  files_added: [],
  files_modified: [],
  files_deleted: [],
  file_changes: [],
  commands_run: [],
  tests_run: [],
  verification_needed: [],
  risks: [],
  follow_up: [],
  decisions: {},
  constraints: [],
  assumptions: [],
  alternatives_rejected: [],
  acceptance_criteria: [],
  blockers: [],
  review_decision: 'not_applicable',
  ...overrides,
});

test('Python progress-v1 fixture maps durable deliverable selectors into their canonical stages', () => {
  const progress = JSON.parse(fs.readFileSync(
    path.join(repositoryRoot, 'contracts', 'fixtures', 'work-item-progress-v1-deliverables.json'),
    'utf8',
  ));

  const presentation = buildWorkItemPresentation({
    id: 'wi-cross-language-fixture',
    status: 'running',
    progress,
  });

  assert.deepEqual(presentation.stages[0].deliverables, [{
    stage: 'planning',
    round: 0,
    runOrdinal: 1,
    itemRevision: 4,
    availability: 'available',
    reason: '',
    digest: 'a'.repeat(64),
    createdAt: '2026-07-12T10:02:00Z',
    preview: 'Baldr entendió el pedido y preparó un plan claro.',
    entryCount: 5,
  }]);
  assert.equal(presentation.stages[1].deliverables[0].availability, 'summary_only');
  assert.equal(presentation.stages[1].deliverables[0].runOrdinal, 2);
  assert.equal(
    presentation.stages[1].deliverables[0].preview,
    'Se conservaron los cambios informados por una ejecución anterior.',
  );
  assert.deepEqual(presentation.stages[2].deliverables, []);
});

test('presented camelCase deliverable descriptors can pass safely through local UI state', () => {
  const presentation = buildWorkItemPresentation({
    status: 'running',
    progress: {
      contract: 'baldr-work-item-progress',
      version: 1,
      overall_state: 'running',
      stages: [],
      deliverables: [{
        stage: 'review',
        round: 12,
        runOrdinal: 3,
        itemRevision: 9,
        availability: 'unavailable',
        reason: 'retention_expired',
        createdAt: '2026-07-12T10:05:00Z',
        preview: 'Resumen local seguro.',
        entryCount: 0,
      }],
    },
  });

  assert.deepEqual(presentation.stages[2].deliverables[0], {
    stage: 'review',
    round: 12,
    runOrdinal: 3,
    itemRevision: 9,
    availability: 'unavailable',
    reason: 'La entrega completa no está disponible.',
    digest: '',
    createdAt: '2026-07-12T10:05:00Z',
    preview: 'Resumen local seguro.',
    entryCount: 0,
  });
});

test('progress exposes only the safe pagination hint for older deliverable descriptors', () => {
  const presentation = buildWorkItemPresentation({
    status: 'completed',
    progress: {
      contract: 'baldr-work-item-progress', version: 1, overall_state: 'complete', stages: [],
      deliverable_index: {
        total: 320, returned: 256, truncated: true,
        next_cursor: 'opaque-public-cursor', action: 'list-item-deliverables',
      },
    },
  });

  assert.deepEqual(presentation.deliverableIndex, {
    total: 320,
    returned: 256,
    truncated: true,
    nextCursor: 'opaque-public-cursor',
  });
});

test('v1 progress becomes a three-stage non-technical presentation', () => {
  const presentation = buildWorkItemPresentation({
    id: 'wi-1',
    status: 'running',
    progress: {
      contract: 'baldr-work-item-progress',
      version: 1,
      revision: 'rev-7',
      overall_state: 'working',
      activity: {
        kind: 'verifying',
        message: 'provider-internal arbitrary text must not become UX copy',
        since: '2026-07-12T14:35:00Z',
      },
      active_stage: 'review',
      stages: [
        {
          id: 'planning',
          state: 'complete',
          outcome: 'planned',
          round_count: 1,
          report: emptyReport({
            summary: 'El plan prioriza una mejora acotada.',
            interpretation: 'La persona necesita entender qué hace Baldr en cada etapa.',
            scope: ['La experiencia de progreso.'],
            approach: ['Usar resultados estructurados y durables.'],
            plan_steps: ['Entender el pedido.', 'Realizar el trabajo.', 'Revisar el resultado.'],
            decisions: { alcance: 'Cambiar solamente la consola.' },
            acceptance_criteria: ['La persona entiende qué sucede.'],
            assumptions: ['La consola se usa desde una barra lateral estrecha.'],
          }),
        },
        {
          id: 'execution',
          state: 'complete',
          outcome: 'implemented',
          round_count: 1,
          report: emptyReport({
            status: 'implemented',
            summary: 'La experiencia fue actualizada.',
            work_completed: ['Se agregaron tarjetas narrativas.'],
            verification_evidence: ['La prueba de presentación pasó.'],
            files_modified: ['src/console.ts'],
            tests_run: ['npm test'],
          }),
          technical: {
            participants: [{
              profile: 'implementer-inline',
              provider: 'codex',
              model_or_agent: 'gpt-internal',
              state: 'succeeded',
              attempt_count: 1,
            }],
          },
        },
        {
          id: 'review',
          state: 'active',
          outcome: '',
          round_count: 1,
          report: emptyReport({ status: 'reviewed' }),
        },
      ],
      final_report: null,
      attention: null,
      milestones: [
        { kind: 'review_started', stage: 'review', state: 'active', message: 'raw', at: '2026-07-12T14:36:00Z' },
      ],
    },
  });

  assert.equal(presentation.revision, 'rev-7');
  assert.equal(presentation.headline, 'Comprobando el resultado');
  assert.equal(presentation.explanation, 'Baldr está verificando que el trabajo funcione como esperabas.');
  assert.doesNotMatch(presentation.explanation, /provider-internal/);
  assert.equal(presentation.activeStage, 'review');
  assert.deepEqual(presentation.stages.map((stage) => stage.id), ['planning', 'execution', 'review']);
  assert.equal(presentation.stages[0].statusLabel, 'Plan listo');
  assert.equal(
    presentation.stages[0].sections.some((section) => section.title === 'Supuestos que tomó'),
    true,
  );
  assert.deepEqual(
    presentation.stages[0].sections.slice(0, 4).map((section) => section.title),
    ['Lo que entendió Baldr', 'Qué incluye', 'Cómo lo va a encarar', 'Pasos acordados'],
  );
  assert.equal(presentation.stages[1].statusLabel, 'Cambios realizados');
  assert.equal(presentation.stages[2].statusLabel, 'Revisando ahora');
  assert.deepEqual(presentation.stages[1].facts.map((fact) => fact.label), ['1 archivo', '1 comprobación informada']);
  assert.equal(presentation.stages[1].facts.find((fact) => fact.id === 'tests').tone, 'neutral');
  const participant = presentation.stages[1].technicalRows.find((row) => row.label === 'implementer-inline');
  assert.match(participant.value, /Modelo o agente: gpt-internal/);
  assert.match(participant.value, /Estado: completado/);
  assert.doesNotMatch(participant.value, /\{|\}/);
  assert.equal(presentation.milestones[0].label, 'Comenzó a comprobar el resultado');
});

test('fallback phases translate internal statuses and never expose them as labels', () => {
  const presentation = buildWorkItemPresentation({
    id: 'wi-old',
    status: 'running',
    phases: [
      { phase: 'architect', status: 'succeeded', round: 0 },
      { phase: 'implementer', status: 'running', round: 0 },
    ],
  });

  assert.equal(presentation.activeStage, 'execution');
  assert.equal(presentation.headline, 'Trabajando en la ejecución');
  assert.deepEqual(
    presentation.stages.map((stage) => stage.statusLabel),
    ['Plan listo', 'Trabajando ahora', 'Todavía no empezó'],
  );
  assert.doesNotMatch(
    presentation.stages.map((stage) => `${stage.statusLabel} ${stage.purpose}`).join(' '),
    /succeeded|running/i,
  );
});

test('fallback workflow groups review and correction rounds into canonical stages', () => {
  const presentation = buildWorkItemPresentation({
    id: 'wi-rounds',
    status: 'running',
    workflow: {
      run: { status: 'running', id: 'run-1' },
      steps: [
        {
          step_key: 'architect.plan', phase: 'architect', status: 'succeeded', round_number: 0,
          output: { final_report: emptyReport({ status: 'planned', summary: 'Plan inicial.' }) },
        },
        {
          step_key: 'implementer.implement', phase: 'implementer', status: 'succeeded', round_number: 0,
          output: { final_report: emptyReport({ status: 'implemented', summary: 'Primer cambio.' }) },
        },
        {
          step_key: 'reviewer.review', phase: 'reviewer', status: 'succeeded', round_number: 0,
          output: { final_report: emptyReport({
            status: 'needs_changes', summary: 'Falta una corrección.', blockers: ['Corregir el caso vacío.'], review_decision: 'changes_required',
          }) },
        },
        {
          step_key: 'implementer.fix_round_1', phase: 'implementer', status: 'running', round_number: 1,
          output: null,
        },
      ],
    },
  });

  assert.equal(presentation.stages.length, 3);
  const execution = presentation.stages.find((stage) => stage.id === 'execution');
  const review = presentation.stages.find((stage) => stage.id === 'review');
  assert.equal(execution.state, 'active');
  assert.equal(execution.roundCount, 2);
  assert.equal(execution.statusLabel, 'Aplicando correcciones');
  assert.match(execution.purpose, /ejecución está en curso/i);
  assert.equal(execution.history.length, 1);
  assert.equal(review.state, 'attention');
  assert.equal(review.statusLabel, 'Hay correcciones pendientes');
});

test('an active correction keeps the previous report in history instead of presenting it as current', () => {
  const previous = emptyReport({ status: 'implemented', summary: 'Primera versión completada.' });
  const presentation = buildWorkItemPresentation({
    status: 'running',
    progress: {
      contract: 'baldr-work-item-progress',
      version: 1,
      overall_state: 'running',
      active_stage: 'execution',
      activity: { kind: 'changing' },
      stages: [{
        id: 'execution',
        state: 'running',
        round_count: 2,
        report: previous,
        history: [
          { round: 0, state: 'complete', outcome: 'implemented', report: previous },
        ],
      }],
    },
  });
  const execution = presentation.stages[1];
  assert.equal(execution.summary, '');
  assert.equal(execution.history.length, 1);
  assert.equal(execution.history[0].summary, 'Primera versión completada.');
});

test('v1 history keeps every previous round supplied by the backend', () => {
  const presentation = buildWorkItemPresentation({
    status: 'running',
    progress: {
      contract: 'baldr-work-item-progress',
      version: 1,
      overall_state: 'working',
      active_stage: 'execution',
      activity: { kind: 'changing' },
      stages: [{
        id: 'execution',
        state: 'running',
        round_count: 2,
        report: emptyReport({ status: 'implemented', summary: 'Resultado anterior.' }),
        history: [{
          round: 0,
          state: 'complete',
          outcome: 'implemented',
          report: emptyReport({ status: 'implemented', summary: 'Primera implementación.' }),
        }],
      }],
    },
  });

  const execution = presentation.stages.find((stage) => stage.id === 'execution');
  assert.equal(execution.history.length, 1);
  assert.equal(execution.history[0].summary, 'Primera implementación.');
});

test('completed work exposes a concise result with pluralized facts', () => {
  const presentation = buildWorkItemPresentation({
    id: 'wi-complete',
    status: 'completed',
    workflow: {
      run: {
        status: 'approved',
        id: 'run-complete',
        final: emptyReport({
          status: 'approved',
          summary: 'La mejora quedó terminada y revisada.',
          files_modified: ['a.ts', 'b.ts'],
          tests_run: ['npm test'],
          follow_up: ['Reiniciar VS Code para verla.'],
          review_decision: 'approved',
        }),
      },
      steps: [],
    },
  });

  assert.equal(presentation.overallState, 'complete');
  assert.equal(presentation.headline, 'Trabajo listo');
  assert.equal(presentation.outcome.title, 'Trabajo listo');
  assert.equal(presentation.outcome.tone, 'positive');
  assert.equal(presentation.outcome.summary, 'La mejora quedó terminada y revisada.');
  assert.deepEqual(presentation.outcome.facts.map((fact) => fact.label), [
    '2 archivos',
    '1 comprobación informada',
    'Veredicto: revisión aprobada',
  ]);
  assert.deepEqual(presentation.outcome.fileChanges, [
    { path: 'a.ts', kind: 'modified', additions: null, deletions: null, evidence: 'reported' },
    { path: 'b.ts', kind: 'modified', additions: null, deletions: null, evidence: 'reported' },
  ]);
  assert.equal(presentation.outcome.sections.some((section) => section.title === 'Próximos pasos'), true);
});

test('completed work separates added, modified, and deleted files', () => {
  const presentation = buildWorkItemPresentation({
    id: 'wi-file-summary',
    status: 'completed',
    workflow: {
      run: {
        status: 'approved',
        id: 'run-file-summary',
        final: emptyReport({
          status: 'approved',
          summary: 'Los cambios quedaron listos.',
          changes_added: ['Un resumen final separado por tipo de cambio.'],
          changes_modified: ['La presentación del resultado de la sesión.'],
          changes_removed: ['La lista genérica que mezclaba todos los cambios.'],
          files_added: ['src/new.ts'],
          files_modified: ['src/current.ts'],
          files_deleted: ['src/old.ts'],
          file_changes: [
            { path: 'src/new.ts', kind: 'added', additions: 12, deletions: 0, evidence: 'observed' },
            { path: 'src/current.ts', kind: 'modified', additions: 4, deletions: 2, evidence: 'observed' },
            { path: 'src/old.ts', kind: 'deleted', additions: 0, deletions: 7, evidence: 'observed' },
          ],
          review_decision: 'approved',
        }),
      },
      steps: [],
    },
  });

  assert.deepEqual(
    presentation.outcome.sections
      .filter((section) => section.id.startsWith('changes-'))
      .map((section) => [section.title, section.items]),
    [
      ['Qué agregó', ['Un resumen final separado por tipo de cambio.']],
      ['Qué modificó', ['La presentación del resultado de la sesión.']],
      ['Qué quitó', ['La lista genérica que mezclaba todos los cambios.']],
    ],
  );
  assert.deepEqual(
    presentation.outcome.fileChanges,
    [
      { path: 'src/new.ts', kind: 'added', additions: 12, deletions: 0, evidence: 'observed' },
      { path: 'src/current.ts', kind: 'modified', additions: 4, deletions: 2, evidence: 'observed' },
      { path: 'src/old.ts', kind: 'deleted', additions: 0, deletions: 7, evidence: 'observed' },
    ],
  );
  assert.equal(presentation.outcome.facts[0].label, '3 archivos');
});

test('a provisional final report never claims the work is ready', () => {
  const presentation = buildWorkItemPresentation({
    status: 'needs_attention',
    progress: {
      contract: 'baldr-work-item-progress',
      version: 1,
      overall_state: 'attention',
      activity: { kind: 'waiting_for_choice' },
      stages: [],
      final_report: emptyReport({
        status: 'needs_changes',
        summary: 'La revisión encontró una corrección pendiente.',
        blockers: ['Resolver la validación pendiente.'],
        review_decision: 'changes_required',
      }),
    },
  });

  assert.equal(presentation.outcome.title, 'Hay cambios pendientes');
  assert.equal(presentation.outcome.tone, 'warning');
  assert.notEqual(presentation.outcome.title, 'Trabajo listo');
});

test('attention has safe default wording and a single clear action', () => {
  const presentation = buildWorkItemPresentation({
    id: 'wi-attention',
    status: 'needs_attention',
    progress: {
      contract: 'baldr-work-item-progress',
      version: 1,
      revision: 'attention-1',
      overall_state: 'attention',
      activity: { kind: 'waiting_for_choice', message: 'internal provider status' },
      active_stage: null,
      stages: [],
      attention: { blockers: ['Elegir cómo conservar los cambios.'] },
    },
  });

  assert.equal(presentation.headline, 'Necesitamos que elijas cómo continuar');
  assert.equal(presentation.attention.title, 'Necesitamos que elijas cómo continuar');
  assert.equal(presentation.attention.actionLabel, 'Elegir cómo continuar');
  assert.deepEqual(presentation.attention.blockers, ['Elegir cómo conservar los cambios.']);
  assert.doesNotMatch(presentation.explanation, /internal provider status/);
});

test('legacy phase failures explain the stopped stage instead of repeating generic recovery copy', () => {
  const presentation = buildWorkItemPresentation({
    status: 'needs_attention',
    allowed_actions: ['mark_failed'],
    progress: {
      contract: 'baldr-work-item-progress',
      version: 1,
      overall_state: 'attention',
      activity: { kind: 'waiting_for_choice' },
      active_stage: 'planning',
      stages: [{
        id: 'planning', state: 'attention', outcome: 'blocked',
        report: emptyReport({
          status: 'blocked',
          blockers: ['La fase de planificación no puede crear archivos.'],
        }),
      }],
      attention: {
        kind: 'reconciliation',
        stage: 'planning',
        summary: 'Los cambios están protegidos y Baldr necesita que elijas cómo continuar.',
        blockers: ['La fase de planificación no puede crear archivos.'],
      },
      technical: { error_codes: ['workflow_phase_failed'] },
    },
  });

  assert.equal(presentation.attention.title, 'La planificación se detuvo');
  assert.equal(
    presentation.attention.message,
    'La planificación se detuvo por el motivo que aparece abajo. '
      + 'No se llegó a modificar ningún archivo.',
  );
  assert.equal(presentation.attention.actionLabel, 'Cerrar esta sesión');
});

test('write authorization is presented as a decision instead of a failure', () => {
  const presentation = buildWorkItemPresentation({
    status: 'needs_attention',
    allowed_actions: ['authorize_changes', 'decline_changes', 'archive'],
    progress: {
      contract: 'baldr-work-item-progress',
      version: 1,
      overall_state: 'attention',
      activity: { kind: 'waiting_for_choice' },
      active_stage: 'planning',
      stages: [{ id: 'planning', state: 'complete', outcome: 'planned' }],
      attention: {
        kind: 'authorization',
        summary: 'El plan está listo. Elegí si Baldr puede modificar archivos.',
        blockers: [],
      },
      technical: { error_codes: ['write_authorization_required'] },
    },
  });

  assert.equal(presentation.attention.kind, 'authorization');
  assert.equal(presentation.attention.title, 'Baldr necesita permiso para modificar archivos');
  assert.match(presentation.attention.message, /Elegí si Baldr puede modificar archivos/);
  assert.equal(presentation.attention.actionLabel, 'Elegir autorización');
  assert.deepEqual(presentation.attention.blockers, []);
});

test('live activity kinds use local Spanish copy', () => {
  const expected = {
    working: 'Trabajando en la sesión',
    analyzing: 'Analizando el pedido',
    researching: 'Buscando información útil',
    changing: 'Preparando los cambios',
    verifying: 'Comprobando el resultado',
  };
  for (const [kind, headline] of Object.entries(expected)) {
    const presentation = buildWorkItemPresentation({
      status: 'running',
      progress: {
        contract: 'baldr-work-item-progress',
        version: 1,
        overall_state: 'working',
        activity: { kind, message: '<script>texto arbitrario</script>' },
        stages: [],
      },
    });
    assert.equal(presentation.headline, headline);
    assert.doesNotMatch(presentation.explanation, /script|arbitrario/);
  }
});

test('completed stage reports present explicit narrative fields without technical logs', () => {
  const presentation = buildWorkItemPresentation({
    status: 'completed',
    progress: {
      contract: 'baldr-work-item-progress',
      version: 1,
      overall_state: 'complete',
      activity: { kind: 'completed' },
      stages: [
        {
          id: 'planning',
          state: 'complete',
          report: emptyReport({
            interpretation: 'La persona quiere una explicación clara.',
            scope: ['La consola de tareas.'],
            approach: ['Mostrar una historia por etapas.'],
            plan_steps: ['Comprender', 'Hacer', 'Comprobar'],
          }),
        },
        {
          id: 'execution',
          state: 'complete',
          report: emptyReport({
            status: 'implemented',
            work_completed: ['Se agregó el seguimiento narrativo.'],
            work_next: ['Reiniciar VS Code.'],
            corrections: ['Se quitó una afirmación sin evidencia.'],
            verification_evidence: ['La prueba de UI terminó correctamente.'],
            commands_run: ['internal-test-command'],
          }),
        },
        {
          id: 'review',
          state: 'complete',
          report: emptyReport({
            status: 'approved',
            findings: ['No quedan problemas importantes.'],
            verification_evidence: ['El contrato público fue validado.'],
            review_decision: 'approved',
          }),
        },
      ],
    },
  });

  const planning = presentation.stages[0];
  const execution = presentation.stages[1];
  const review = presentation.stages[2];
  assert.deepEqual(
    planning.sections.slice(0, 4).map((entry) => entry.id),
    ['interpretation', 'scope', 'approach', 'plan-steps'],
  );
  assert.equal(execution.sections.find((entry) => entry.id === 'work-completed').items[0], 'Se agregó el seguimiento narrativo.');
  assert.equal(execution.sections.find((entry) => entry.id === 'verification-evidence').items[0], 'La prueba de UI terminó correctamente.');
  assert.equal(review.sections.find((entry) => entry.id === 'findings').items[0], 'No quedan problemas importantes.');
  assert.equal(execution.sections.some((entry) => entry.items.includes('internal-test-command')), false);
  assert.equal(execution.technicalSections.some((entry) => entry.items.includes('internal-test-command')), true);
});

test('live analyzing copy follows the active stage', () => {
  const cases = {
    planning: 'Analizando el pedido',
    execution: 'Analizando cómo hacer los cambios',
    review: 'Analizando el resultado',
  };
  for (const [activeStage, headline] of Object.entries(cases)) {
    const presentation = buildWorkItemPresentation({
      status: 'running',
      progress: {
        contract: 'baldr-work-item-progress',
        version: 1,
        overall_state: 'running',
        active_stage: activeStage,
        activity: { kind: 'analyzing' },
        stages: [{ id: activeStage, state: 'running', report: null }],
      },
    });
    assert.equal(presentation.headline, headline);
  }
});

test('skipped terminal stages are neutral instead of looking unfinished', () => {
  const presentation = buildWorkItemPresentation({
    status: 'completed',
    progress: {
      contract: 'baldr-work-item-progress',
      version: 1,
      overall_state: 'complete',
      activity: { kind: 'completed' },
      stages: [
        { id: 'planning', state: 'complete', report: emptyReport() },
        { id: 'execution', state: 'complete', report: emptyReport({ status: 'no_changes_needed' }) },
        { id: 'review', state: 'skipped', outcome: 'no_changes_needed', report: null },
      ],
    },
  });
  const review = presentation.stages.find((stage) => stage.id === 'review');
  assert.equal(review.state, 'skipped');
  assert.equal(review.statusLabel, 'No fue necesaria');
  assert.doesNotMatch(review.purpose, /no empezó/i);
});

test('report arrays are bounded for a narrow durable view', () => {
  const presentation = buildWorkItemPresentation({
    status: 'running',
    progress: {
      contract: 'baldr-work-item-progress',
      version: 1,
      overall_state: 'working',
      active_stage: 'review',
      stages: [{
        id: 'execution',
        state: 'complete',
        report: emptyReport({ files_modified: Array.from({ length: 30 }, (_, index) => `file-${index}.ts`) }),
      }, { id: 'review', state: 'running', report: null }],
    },
  });
  const files = presentation.stages[1].sections.find((section) => section.id === 'files');
  assert.equal(files.items.length, 20);
});

test('v1 technical lists use readable disclosure sections instead of JSON rows', () => {
  const presentation = buildWorkItemPresentation({
    status: 'completed',
    progress: {
      contract: 'baldr-work-item-progress',
      version: 1,
      overall_state: 'complete',
      stages: [{
        id: 'execution',
        state: 'complete',
        outcome: 'implemented',
        report: emptyReport({ status: 'implemented', summary: 'Listo.' }),
        technical: {
          commands_run: ['npm test'],
          constraints: ['Mantener compatibilidad.'],
          alternatives_rejected: ['Mostrar eventos crudos.'],
        },
      }],
    },
  });
  const execution = presentation.stages.find((stage) => stage.id === 'execution');

  assert.deepEqual(
    execution.technicalSections.map((section) => section.title),
    ['Comandos ejecutados', 'Límites considerados', 'Opciones descartadas'],
  );
  assert.equal(execution.technicalRows.some((row) => row.value.includes('npm test')), false);
});

test('stage duration is honest, bounded, and tolerant of invalid timestamps', () => {
  const now = Date.parse('2026-07-12T14:00:40Z');
  assert.equal(
    formatStageDuration('2026-07-12T14:00:00Z', null, 'active', now),
    'En curso hace 40 s',
  );
  assert.equal(
    formatStageDuration('2026-07-12T13:58:00Z', '2026-07-12T14:00:05Z', 'complete', now),
    'Duró 2 min',
  );
  assert.equal(formatStageDuration('not-a-date', null, 'active', now), '');
  assert.equal(formatStageDuration('2026-07-12T14:00:00Z', null, 'complete', now), '');

  const presentation = buildWorkItemPresentation({
    status: 'running',
    progress: {
      contract: 'baldr-work-item-progress',
      version: 1,
      overall_state: 'running',
      active_stage: 'planning',
      stages: [{ id: 'planning', state: 'running', started_at: '2026-07-12T14:00:00Z' }],
    },
  }, now);
  assert.equal(presentation.stages[0].durationLabel, 'En curso hace 40 s');
});

test('unknown future progress versions degrade safely instead of being parsed as v1', () => {
  const presentation = buildWorkItemPresentation({
    status: 'running',
    progress: {
      contract: 'baldr-work-item-progress',
      version: 2,
      overall_state: 'complete',
      activity: { kind: 'completed' },
      active_stage: 'review',
      stages: [{ id: 'review', state: 'complete', report: { summary: 'Private future shape' } }],
      final_report: { status: 'approved', summary: 'Must not claim success' },
    },
  });

  assert.equal(presentation.outcome, null);
  assert.equal(presentation.activeStage, '');
  assert.deepEqual(presentation.stages.map((stage) => stage.state), ['pending', 'pending', 'pending']);
  assert.notEqual(presentation.headline, 'Trabajo listo');
});

test('live provider observations are not presented as completed milestones', () => {
  const presentation = buildWorkItemPresentation({
    status: 'running',
    progress: {
      contract: 'baldr-work-item-progress',
      version: 1,
      overall_state: 'running',
      activity: { kind: 'analyzing' },
      active_stage: 'planning',
      stages: [{ id: 'planning', state: 'running' }],
      milestones: [
        { kind: 'analyzing', stage: 'planning', state: 'running', evidence: 'observed', at: '2026-07-12T14:00:00Z' },
        { kind: 'stage_started', stage: 'planning', state: 'running', evidence: 'observed', at: '2026-07-12T14:00:01Z' },
      ],
    },
  });

  assert.equal(presentation.milestones.some((entry) => entry.id === 'milestone-0'), false);
  assert.deepEqual(presentation.milestones.map((entry) => entry.evidence), ['observed']);
});

test('an approved review remains approved when publication needs a global decision', () => {
  const presentation = buildWorkItemPresentation({
    status: 'needs_attention',
    allowed_actions: ['inspect_shadow', 'apply_shadow_changes'],
    progress: {
      contract: 'baldr-work-item-progress',
      version: 1,
      overall_state: 'attention',
      activity: { kind: 'waiting_for_choice' },
      active_stage: null,
      stages: [{
        id: 'review',
        state: 'complete',
        outcome: 'approved',
        report: emptyReport({ status: 'approved', review_decision: 'approved', summary: 'La revisión terminó bien.' }),
      }],
      attention: {
        kind: 'reconciliation',
        stage: null,
        summary: 'Tus archivos cambiaron; no los sobrescribimos.',
        retryable: null,
      },
    },
  });

  const review = presentation.stages.find((stage) => stage.id === 'review');
  assert.equal(presentation.activeStage, '');
  assert.equal(review.state, 'complete');
  assert.equal(review.statusLabel, 'Todo revisado');
  assert.equal(review.facts.find((fact) => fact.id === 'review-verdict').label, 'Veredicto: revisión aprobada');
  assert.doesNotMatch(`${review.statusLabel} ${review.purpose} ${presentation.attention.message}`, /correcci/i);
});

test('recovery and unknown review state never pretend that corrections were requested', () => {
  const presentation = buildWorkItemPresentation({
    status: 'running',
    progress: {
      contract: 'baldr-work-item-progress',
      version: 1,
      overall_state: 'running',
      activity: { kind: 'recovering' },
      active_stage: 'review',
      stages: [{ id: 'review', state: 'running', outcome: null, report: null, round_count: 2 }],
    },
  });

  const review = presentation.stages.find((stage) => stage.id === 'review');
  assert.equal(presentation.headline, 'Retomando el trabajo');
  assert.equal(review.state, 'active');
  assert.doesNotMatch(`${review.statusLabel} ${review.purpose} ${presentation.explanation}`, /correcci/i);
});

test('retry action copy is exposed only when the durable error says retryable', () => {
  for (const [retryable, expected] of [[true, 'Volver a intentar'], [false, 'Elegir cómo continuar'], [null, 'Elegir cómo continuar']]) {
    const presentation = buildWorkItemPresentation({
      status: 'failed',
      progress: {
        contract: 'baldr-work-item-progress',
        version: 1,
        overall_state: 'attention',
        activity: { kind: 'attention' },
        stages: [],
        attention: { summary: 'No se pudo completar la tarea.', retryable },
      },
    });
    assert.equal(presentation.attention.retryable, retryable);
    assert.equal(presentation.attention.actionLabel, expected);
  }
});

test('a durable pending run is queued while a saved draft remains not started', () => {
  const queued = buildWorkItemPresentation({
    status: 'ready',
    progress: {
      contract: 'baldr-work-item-progress', version: 1, overall_state: 'pending',
      activity: { kind: 'waiting' }, stages: [], technical: { run_state: 'pending' },
    },
  });
  const draft = buildWorkItemPresentation({
    status: 'draft',
    progress: {
      contract: 'baldr-work-item-progress', version: 1, overall_state: 'pending',
      activity: { kind: 'waiting' }, stages: [], technical: { run_state: null },
    },
  });

  assert.equal(queued.overallState, 'queued');
  assert.equal(queued.headline, 'En espera');
  assert.equal(draft.overallState, 'draft');
  assert.equal(draft.headline, 'Todavía no empezó');
});

test('cancelling and cancelled work never promise that future stages will run', () => {
  const cancelling = buildWorkItemPresentation({
    status: 'cancelling',
    progress: {
      contract: 'baldr-work-item-progress', version: 1, overall_state: 'running',
      activity: { kind: 'cancelling' }, active_stage: 'planning',
      stages: [{ id: 'planning', state: 'running' }],
    },
  });
  const cancelled = buildWorkItemPresentation({
    status: 'cancelled',
    progress: {
      contract: 'baldr-work-item-progress', version: 1, overall_state: 'cancelled',
      activity: { kind: 'cancelled' }, active_stage: null,
      stages: [{ id: 'planning', state: 'cancelled' }],
    },
  });

  assert.deepEqual(cancelling.stages.map((stage) => stage.statusLabel), [
    'Deteniendo esta etapa', 'No se iniciará', 'No se iniciará',
  ]);
  assert.doesNotMatch(cancelling.stages.slice(1).map((stage) => stage.purpose).join(' '), /hará|comprobará|va a/i);
  assert.equal(cancelled.activeStage, '');
  assert.equal(cancelled.stages.every((stage) => stage.state === 'cancelled'), true);
  assert.doesNotMatch(cancelled.stages.map((stage) => stage.purpose).join(' '), /hará|comprobará|va a/i);
});

test('review verdicts use human wording and inconclusive never means corrections', () => {
  const expected = {
    approved: 'Veredicto: revisión aprobada',
    changes_required: 'Veredicto: hay cambios pendientes',
    inconclusive: 'Veredicto: revisión inconclusa',
    not_applicable: 'Veredicto: no aplica',
  };
  for (const [decision, label] of Object.entries(expected)) {
    const status = decision === 'inconclusive' ? 'inconclusive' : decision === 'changes_required' ? 'needs_changes' : 'reviewed';
    const presentation = buildWorkItemPresentation({
      progress: {
        contract: 'baldr-work-item-progress', version: 1,
        overall_state: decision === 'approved' || decision === 'not_applicable' ? 'complete' : 'attention',
        activity: { kind: decision === 'approved' ? 'completed' : 'attention' },
        stages: [{ id: 'review', state: decision === 'approved' || decision === 'not_applicable' ? 'complete' : 'attention', outcome: status,
          report: emptyReport({ status, review_decision: decision }) }],
      },
    });
    const review = presentation.stages[2];
    assert.equal(review.facts.find((fact) => fact.id === 'review-verdict').label, label);
    if (decision === 'inconclusive') {
      assert.equal(review.statusLabel, 'Revisión inconclusa');
      assert.doesNotMatch(`${review.statusLabel} ${review.purpose}`, /correcci/i);
    }
  }
});

test('failed and inconclusive final reports are warning results', () => {
  for (const [status, title] of [['failed', 'El trabajo necesita atención'], ['inconclusive', 'No se pudo confirmar el resultado']]) {
    const presentation = buildWorkItemPresentation({
      status: 'needs_attention',
      progress: {
        contract: 'baldr-work-item-progress', version: 1, overall_state: 'attention',
        activity: { kind: 'attention' }, stages: [],
        final_report: emptyReport({ status, review_decision: status === 'inconclusive' ? 'inconclusive' : 'not_applicable' }),
      },
    });
    assert.equal(presentation.outcome.tone, 'warning');
    assert.equal(presentation.outcome.title, title);
  }
});

test('archived draft and failed work remain historical and never claim to be ready', () => {
  const archivedDraft = buildWorkItemPresentation({
    status: 'archived',
    progress: {
      contract: 'baldr-work-item-progress', version: 1, overall_state: 'archived',
      activity: { kind: 'archived' }, active_stage: null, stages: [],
      final_report: null, technical: { run_state: null },
    },
  });
  const archivedFailure = buildWorkItemPresentation({
    status: 'archived',
    progress: {
      contract: 'baldr-work-item-progress', version: 1, overall_state: 'archived',
      activity: { kind: 'archived' }, active_stage: 'execution',
      stages: [{ id: 'execution', state: 'attention', outcome: 'failed',
        report: emptyReport({ status: 'failed', summary: 'La ejecución no pudo completarse.' }) }],
      final_report: emptyReport({ status: 'failed', summary: 'La ejecución no pudo completarse.' }),
      technical: { run_state: 'failed' },
    },
  });

  for (const presentation of [archivedDraft, archivedFailure]) {
    assert.equal(presentation.overallState, 'archived');
    assert.equal(presentation.activity, 'archived');
    assert.equal(presentation.activeStage, '');
    assert.equal(presentation.headline, 'Sesión archivada');
    assert.doesNotMatch(JSON.stringify(presentation), /Trabajo listo|Baldr completó y revisó el trabajo/);
  }
  assert.equal(archivedDraft.stages.every((stage) => stage.state === 'skipped'), true);
  assert.equal(archivedFailure.outcome.tone, 'warning');
  assert.equal(archivedFailure.outcome.title, 'El trabajo necesita atención');
  assert.equal(archivedFailure.stages[0].state, 'skipped');
  assert.equal(archivedFailure.stages[2].state, 'skipped');
});

test('long activity streams do not push lifecycle milestones out of presentation', () => {
  const activity = Array.from({ length: 100 }, (_, index) => ({
    kind: index % 2 ? 'analyzing' : 'verifying', stage: 'planning', state: 'running', at: `2026-07-12T10:01:${String(index % 60).padStart(2, '0')}Z`,
  }));
  const presentation = buildWorkItemPresentation({
    status: 'running',
    progress: {
      contract: 'baldr-work-item-progress', version: 1, overall_state: 'running',
      activity: { kind: 'analyzing' }, active_stage: 'planning',
      stages: [{ id: 'planning', state: 'running' }],
      milestones: [
        { kind: 'created', state: 'pending', at: '2026-07-12T10:00:00Z', evidence: 'observed' },
        { kind: 'started', state: 'running', at: '2026-07-12T10:00:01Z', evidence: 'observed' },
        ...activity,
      ],
    },
  });

  assert.deepEqual(presentation.milestones.map((entry) => entry.label), [
    'La sesión quedó preparada', 'La sesión comenzó',
  ]);
});

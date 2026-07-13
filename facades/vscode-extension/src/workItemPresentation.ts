import type {
  CanonicalStageId,
  WorkItemActivity,
  WorkItemOverallState,
  WorkItemStageState,
} from './consoleProtocol.js';

type JsonRecord = Record<string, unknown>;

export interface PresentationSection {
  id: string;
  title: string;
  items: string[];
  tone: 'neutral' | 'positive' | 'warning' | 'danger';
}

export interface PresentationFact {
  id: string;
  label: string;
  tone: 'neutral' | 'positive' | 'warning' | 'danger';
}

export interface TechnicalRow {
  label: string;
  value: string;
}

export interface StageHistoryPresentation {
  round: number;
  label: string;
  stateLabel: string;
  summary: string;
}

export interface PhaseDeliverablePresentation {
  stage: CanonicalStageId;
  round: number;
  runOrdinal: number;
  itemRevision: number;
  availability: 'available' | 'summary_only' | 'unavailable';
  reason: string;
  digest: string;
  createdAt: string;
  preview: string;
  entryCount: number;
}

export interface DeliverableIndexPresentation {
  total: number;
  returned: number;
  truncated: boolean;
  nextCursor: string;
}

export interface StagePresentation {
  id: CanonicalStageId;
  title: string;
  subtitle: string;
  purpose: string;
  state: WorkItemStageState;
  statusLabel: string;
  outcome: string;
  summary: string;
  roundCount: number;
  startedAt: string;
  completedAt: string;
  durationLabel: string;
  facts: PresentationFact[];
  sections: PresentationSection[];
  milestones: MilestonePresentation[];
  deliverables: PhaseDeliverablePresentation[];
  history: StageHistoryPresentation[];
  technicalRows: TechnicalRow[];
  technicalSections: PresentationSection[];
}

export interface ReportPresentation {
  title: string;
  tone: 'neutral' | 'positive' | 'warning';
  status: string;
  reviewDecision: string;
  summary: string;
  facts: PresentationFact[];
  sections: PresentationSection[];
  technicalSections: PresentationSection[];
}

export interface AttentionPresentation {
  title: string;
  message: string;
  actionLabel: string;
  blockers: string[];
  retryable: boolean | null;
}

export interface MilestonePresentation {
  id: string;
  label: string;
  occurredAt: string;
  state: string;
  evidence: string;
  stage: CanonicalStageId | '';
}

export interface WorkItemPresentation {
  version: 1;
  revision: string;
  overallState: WorkItemOverallState;
  activity: WorkItemActivity;
  activeStage: CanonicalStageId | '';
  headline: string;
  explanation: string;
  lastEventAt: string;
  stages: StagePresentation[];
  deliverableIndex: DeliverableIndexPresentation;
  outcome: ReportPresentation | null;
  attention: AttentionPresentation | null;
  milestones: MilestonePresentation[];
  technicalRows: TechnicalRow[];
}

const STAGE_ORDER: CanonicalStageId[] = ['planning', 'execution', 'review'];
const MAX_ITEMS = 20;
const MAX_DELIVERABLES = 256;
const MAX_ITEM_LENGTH = 1_000;
const MAX_SUMMARY_LENGTH = 2_400;

const STAGE_COPY: Record<CanonicalStageId, { title: string; subtitle: string; pending: string; active: string }> = {
  planning: {
    title: 'Planificación',
    subtitle: 'Entender y organizar',
    pending: 'Baldr va a entender tu pedido y decidir los pasos.',
    active: 'La planificación está en curso. El resultado aparecerá cuando termine esta etapa.',
  },
  execution: {
    title: 'Ejecución',
    subtitle: 'Hacer el trabajo',
    pending: 'Baldr hará el trabajo siguiendo el plan acordado.',
    active: 'La ejecución está en curso. Los cambios informados aparecerán cuando termine esta etapa.',
  },
  review: {
    title: 'Revisión',
    subtitle: 'Comprobar el resultado',
    pending: 'Baldr comprobará que el resultado cumpla tu pedido.',
    active: 'La revisión está en curso. Los hallazgos aparecerán cuando termine esta etapa.',
  },
};

function record(value: unknown): JsonRecord {
  return value && typeof value === 'object' && !Array.isArray(value) ? value as JsonRecord : {};
}

function records(value: unknown): JsonRecord[] {
  return Array.isArray(value) ? value.map(record).filter((item) => Object.keys(item).length > 0) : [];
}

function cleanText(value: unknown, limit = MAX_ITEM_LENGTH): string {
  if (value === undefined || value === null) return '';
  const raw = typeof value === 'string' ? value : typeof value === 'number' || typeof value === 'boolean' ? String(value) : '';
  const normalized = raw.trim();
  return normalized.length <= limit ? normalized : `${normalized.slice(0, limit)}…`;
}

function stringList(value: unknown): string[] {
  if (!Array.isArray(value)) return [];
  return value
    .map((item) => cleanText(item))
    .filter(Boolean)
    .slice(0, MAX_ITEMS);
}

function positiveInteger(value: unknown, fallback = 0): number {
  const parsed = Number(value);
  return Number.isInteger(parsed) && parsed >= 0 ? parsed : fallback;
}

function deliverableReason(
  value: unknown,
  availability: PhaseDeliverablePresentation['availability'],
  hasPreview: boolean,
): string {
  if (availability === 'available') return '';
  const reason = cleanText(value, 120).toLowerCase();
  if (reason === 'report_too_large') {
    return hasPreview
      ? 'La entrega completa es muy extensa; mostramos el resumen disponible.'
      : 'La entrega completa es muy extensa y no se conservaron más detalles.';
  }
  if (reason === 'legacy_output_too_large') {
    return 'El contenido histórico era demasiado extenso y no se pudo recuperar completo.';
  }
  if (['report_invalid'].includes(reason)) {
    return 'Esta sesión conserva un resumen, pero no todos los detalles.';
  }
  if (['legacy_output_missing', 'legacy_output_corrupt', 'legacy_report_missing', 'report_missing', 'stored_deliverable_corrupt'].includes(reason)) {
    return 'La entrega completa no está disponible para esta sesión anterior.';
  }
  return availability === 'summary_only'
    ? 'Está disponible el resumen de esta entrega.'
    : 'La entrega completa no está disponible.';
}

export function buildPhaseDeliverablePresentations(value: unknown): PhaseDeliverablePresentation[] {
  return records(value).map((entry) => {
    const stage = canonicalStage(entry.stage);
    const availabilityValue = cleanText(entry.availability, 40).toLowerCase();
    const availability = ['available', 'summary_only', 'unavailable'].includes(availabilityValue)
      ? availabilityValue as PhaseDeliverablePresentation['availability']
      : 'unavailable';
    if (!stage) return null;
    const preview = cleanText(record(entry.preview).summary ?? entry.preview, MAX_SUMMARY_LENGTH);
    return {
      stage,
      round: positiveInteger(entry.round, 0),
      // The public wire contract is snake_case. Accept the camelCase form too so
      // an already-presented descriptor can safely pass through local UI state.
      runOrdinal: positiveInteger(entry.run_ordinal ?? entry.runOrdinal, 0),
      itemRevision: positiveInteger(entry.item_revision ?? entry.itemRevision, 0),
      availability,
      reason: deliverableReason(entry.reason, availability, Boolean(preview)),
      digest: cleanText(entry.digest, 160),
      createdAt: cleanText(entry.created_at ?? entry.createdAt, 120),
      preview,
      entryCount: positiveInteger(entry.entry_count ?? entry.entryCount, 0),
    };
  }).filter((entry): entry is PhaseDeliverablePresentation => entry !== null)
    .sort((left, right) => left.runOrdinal - right.runOrdinal || left.round - right.round)
    .slice(-MAX_DELIVERABLES);
}

function deliverableIndex(value: unknown): DeliverableIndexPresentation {
  const index = record(value);
  return {
    total: positiveInteger(index.total, 0),
    returned: positiveInteger(index.returned, 0),
    truncated: index.truncated === true,
    nextCursor: cleanText(index.next_cursor ?? index.nextCursor, 2_000),
  };
}

function canonicalStage(value: unknown): CanonicalStageId | '' {
  const normalized = cleanText(value, 120).toLowerCase().replace(/[.\s-]+/g, '_');
  if (normalized.includes('architect') || normalized.includes('architecture') || normalized.includes('plan')) return 'planning';
  if (normalized.includes('implement') || normalized.includes('execution') || normalized.includes('fix')) return 'execution';
  if (normalized.includes('review')) return 'review';
  return '';
}

function stageState(value: unknown, outcomeValue: unknown = ''): WorkItemStageState {
  const outcome = cleanText(outcomeValue, 120).toLowerCase();
  if (['blocked', 'needs_changes', 'changes_required', 'partial', 'inconclusive'].includes(outcome)) return 'attention';
  const normalized = cleanText(value, 120).toLowerCase();
  if (['dispatching', 'running', 'in_progress', 'active', 'cancelling'].includes(normalized)) return 'active';
  if (['succeeded', 'completed', 'approved', 'planned', 'implemented', 'reviewed', 'complete'].includes(normalized)) return 'complete';
  if (['failed', 'blocked', 'needs_attention', 'needs_changes', 'attention'].includes(normalized)) return 'attention';
  if (['cancelled', 'canceled'].includes(normalized)) return 'cancelled';
  if (normalized === 'skipped') return 'skipped';
  return 'pending';
}

function overallState(value: unknown): WorkItemOverallState {
  const normalized = cleanText(value, 120).toLowerCase();
  if (['running', 'working', 'in_progress'].includes(normalized)) return 'working';
  if (['finalizing', 'publishing'].includes(normalized)) return 'finalizing';
  if (['needs_attention', 'needs_changes', 'failed', 'blocked', 'awaiting_reconciliation', 'attention'].includes(normalized)) return 'attention';
  if (['completed', 'approved', 'complete', 'succeeded'].includes(normalized)) return 'complete';
  if (['cancelled', 'canceled', 'cancelling'].includes(normalized)) return 'cancelled';
  if (normalized === 'archived') return 'archived';
  if (['queued', 'ready'].includes(normalized)) return 'queued';
  return 'draft';
}

function activity(value: unknown, overall: WorkItemOverallState, active: CanonicalStageId | ''): WorkItemActivity {
  const activityValue = record(value);
  const normalized = cleanText(Object.keys(activityValue).length ? activityValue.kind : value, 120).toLowerCase();
  const known: WorkItemActivity[] = [
    'waiting', 'preparing_workspace', 'working', 'analyzing', 'researching', 'planning',
    'changing', 'implementing', 'fixing', 'verifying', 'reviewing',
    'publishing', 'recovering', 'cancelling', 'completed', 'cancelled',
    'attention', 'waiting_for_choice', 'archived', 'finished',
  ];
  if (known.includes(normalized as WorkItemActivity)) return normalized as WorkItemActivity;
  if (overall === 'archived') return 'archived';
  if (overall === 'complete') return 'finished';
  if (overall === 'finalizing') return 'publishing';
  if (overall === 'attention') return 'waiting_for_choice';
  if (active) return 'working';
  return 'waiting';
}

function headlineCopy(
  selected: WorkItemActivity,
  overall: WorkItemOverallState,
  active: CanonicalStageId | '',
): { headline: string; explanation: string } {
  if (selected === 'archived' || overall === 'archived') return {
    headline: 'Sesión archivada',
    explanation: 'La sesión quedó guardada en el historial. Podés consultar lo que ocurrió y sus entregas.',
  };
  if (selected === 'preparing_workspace') return {
    headline: 'Preparando un lugar seguro',
    explanation: 'Baldr está preparando el espacio donde realizará el trabajo.',
  };
  if (selected === 'working') return {
    headline: active === 'planning'
      ? 'Trabajando en la planificación'
      : active === 'execution'
        ? 'Trabajando en la ejecución'
        : active === 'review'
          ? 'Trabajando en la revisión'
          : 'Trabajando en la sesión',
    explanation: 'La etapa está en curso. Baldr mostrará un avance cuando tenga información confirmada para compartir.',
  };
  if (selected === 'analyzing') return {
    headline: active === 'execution'
      ? 'Analizando cómo hacer los cambios'
      : active === 'review'
        ? 'Analizando el resultado'
        : 'Analizando el pedido',
    explanation: active === 'execution'
      ? 'Baldr está revisando la mejor forma de realizar los cambios.'
      : active === 'review'
        ? 'Baldr está revisando el resultado antes de dar una conclusión.'
        : 'Baldr está revisando lo que necesitás antes de organizar el trabajo.',
  };
  if (selected === 'researching') return {
    headline: active === 'execution'
      ? 'Buscando información para los cambios'
      : active === 'review'
        ? 'Buscando información para comprobar el resultado'
        : 'Buscando información útil',
    explanation: 'Baldr está reuniendo el contexto necesario para avanzar con seguridad.',
  };
  if (selected === 'planning') return {
    headline: 'Organizando el trabajo',
    explanation: 'Baldr está entendiendo tu pedido y armando un plan.',
  };
  if (selected === 'implementing') return {
    headline: 'Haciendo los cambios',
    explanation: 'Baldr está trabajando según el plan acordado.',
  };
  if (selected === 'changing') return {
    headline: active === 'planning'
      ? 'Preparando el plan'
      : active === 'review'
        ? 'Preparando una corrección'
        : 'Preparando los cambios',
    explanation: active === 'review'
      ? 'Baldr está preparando una corrección a partir de lo que encontró.'
      : 'Baldr está trabajando en los cambios acordados.',
  };
  if (selected === 'fixing') return {
    headline: 'Ajustando el resultado',
    explanation: 'Baldr está corrigiendo lo que encontró la revisión.',
  };
  if (selected === 'reviewing') return {
    headline: 'Comprobando el resultado',
    explanation: 'Baldr verifica que los cambios cumplan tu pedido y funcionen correctamente.',
  };
  if (selected === 'verifying') return {
    headline: active === 'planning'
      ? 'Comprobando el plan'
      : active === 'execution'
        ? 'Comprobando los cambios'
        : 'Comprobando el resultado',
    explanation: 'Baldr está verificando que el trabajo funcione como esperabas.',
  };
  if (selected === 'publishing') return {
    headline: 'Guardando el resultado',
    explanation: 'El trabajo terminó y se está aplicando de forma segura.',
  };
  if (selected === 'recovering') return {
    headline: 'Retomando el trabajo',
    explanation: 'Baldr está recuperando el último estado seguro para poder continuar.',
  };
  if (selected === 'cancelling') return {
    headline: 'Deteniendo el trabajo',
    explanation: 'Baldr está cerrando la sesión de forma segura.',
  };
  if (selected === 'completed') return {
    headline: 'Trabajo listo',
    explanation: 'Los cambios fueron realizados y revisados.',
  };
  if (selected === 'cancelled') return {
    headline: 'Trabajo cancelado',
    explanation: 'Baldr dejó de trabajar en esta sesión.',
  };
  if (selected === 'attention') return {
    headline: 'Necesitamos que elijas cómo continuar',
    explanation: 'El trabajo está preservado. Revisá las opciones disponibles para decidir el próximo paso.',
  };
  if (selected === 'waiting_for_choice') return {
    headline: 'Necesitamos que elijas cómo continuar',
    explanation: 'El trabajo está preservado. Revisá las opciones disponibles para decidir el próximo paso.',
  };
  if (selected === 'finished') return {
    headline: 'Trabajo listo',
    explanation: 'Los cambios fueron realizados y revisados.',
  };
  if (overall === 'cancelled') return {
    headline: 'Trabajo cancelado',
    explanation: 'Baldr dejó de trabajar en esta sesión.',
  };
  if (overall === 'queued') return {
    headline: 'En espera',
    explanation: 'La sesión está lista y comenzará en cuanto sea posible.',
  };
  return {
    headline: 'Todavía no empezó',
    explanation: 'La sesión está guardada y lista para comenzar.',
  };
}

function decisions(value: unknown): string[] {
  if (Array.isArray(value)) {
    return value.map((entry) => {
      const item = record(entry);
      const key = cleanText(item.key);
      const decision = cleanText(item.value);
      return key && decision ? `${key}: ${decision}` : '';
    }).filter(Boolean).slice(0, MAX_ITEMS);
  }
  return Object.entries(record(value))
    .map(([key, decision]) => {
      const rendered = cleanText(decision);
      return rendered ? `${key}: ${rendered}` : '';
    })
    .filter(Boolean)
    .slice(0, MAX_ITEMS);
}

function section(id: string, title: string, items: string[], tone: PresentationSection['tone'] = 'neutral'): PresentationSection | null {
  return items.length ? { id, title, items, tone } : null;
}

function plural(count: number, singular: string, pluralValue: string): string {
  return `${count} ${count === 1 ? singular : pluralValue}`;
}

function durationValue(seconds: number): string {
  if (seconds < 60) return `${seconds} s`;
  if (seconds < 3_600) return `${Math.max(1, Math.floor(seconds / 60))} min`;
  const hours = Math.floor(seconds / 3_600);
  const minutes = Math.floor((seconds % 3_600) / 60);
  return minutes ? `${hours} h ${minutes} min` : `${hours} h`;
}

export function formatStageDuration(
  startedAt: unknown,
  completedAt: unknown,
  state: WorkItemStageState,
  nowMs = Date.now(),
): string {
  const started = Date.parse(cleanText(startedAt, 120));
  const completedText = cleanText(completedAt, 120);
  const finished = completedText ? Date.parse(completedText) : Number.NaN;
  if (state !== 'active' && !Number.isFinite(finished)) return '';
  const end = Number.isFinite(finished) ? finished : nowMs;
  if (!Number.isFinite(started) || !Number.isFinite(end) || end < started) return '';
  const elapsed = Math.max(0, Math.floor((end - started) / 1_000));
  return state === 'active' ? `En curso hace ${durationValue(elapsed)}` : `Duró ${durationValue(elapsed)}`;
}

function reportPresentation(
  value: unknown,
  stage: CanonicalStageId | 'final',
  overall: WorkItemOverallState = 'draft',
): ReportPresentation {
  const report = record(value);
  const status = cleanText(report.status, 120).toLowerCase();
  const reviewDecision = cleanText(report.review_decision, 120).toLowerCase();
  const files = stringList(report.files_modified);
  const tests = stringList(report.tests_run);
  const risks = stringList(report.risks);
  const blockers = stringList(report.blockers);
  const verification = stringList(report.verification_needed);
  const followUp = stringList(report.follow_up);
  const interpretation = cleanText(report.interpretation, MAX_SUMMARY_LENGTH);
  const scope = stringList(report.scope);
  const approach = stringList(report.approach);
  const planSteps = stringList(report.plan_steps);
  const workCompleted = stringList(report.work_completed);
  const workNext = stringList(report.work_next);
  const findings = stringList(report.findings);
  const corrections = stringList(report.corrections);
  const verificationEvidence = stringList(report.verification_evidence);
  const criteria = stringList(report.acceptance_criteria);
  const assumptions = stringList(report.assumptions);
  const reportDecisions = decisions(report.decisions);
  const facts: PresentationFact[] = [];
  if (files.length) facts.push({ id: 'files', label: plural(files.length, 'archivo', 'archivos'), tone: 'neutral' });
  if (tests.length) facts.push({ id: 'tests', label: plural(tests.length, 'comprobación informada', 'comprobaciones informadas'), tone: 'neutral' });
  if (risks.length) facts.push({ id: 'risks', label: plural(risks.length, 'punto a tener en cuenta', 'puntos a tener en cuenta'), tone: 'warning' });
  if (blockers.length) facts.push({ id: 'blockers', label: plural(blockers.length, 'pendiente importante', 'pendientes importantes'), tone: 'danger' });
  const verdict = {
    approved: { label: 'Veredicto: revisión aprobada', tone: 'positive' as const },
    changes_required: { label: 'Veredicto: hay cambios pendientes', tone: 'warning' as const },
    inconclusive: { label: 'Veredicto: revisión inconclusa', tone: 'warning' as const },
    not_applicable: { label: 'Veredicto: no aplica', tone: 'neutral' as const },
  }[reviewDecision];
  if (verdict && (stage === 'review' || stage === 'final')) {
    facts.push({ id: 'review-verdict', ...verdict });
  }

  const sections: PresentationSection[] = [];
  const push = (candidate: PresentationSection | null): void => { if (candidate) sections.push(candidate); };
  if (stage === 'planning') {
    push(section('interpretation', 'Lo que entendió Baldr', interpretation ? [interpretation] : []));
    push(section('scope', 'Qué incluye', scope));
    push(section('approach', 'Cómo lo va a encarar', approach));
    push(section('plan-steps', 'Pasos acordados', planSteps));
    push(section('decisions', 'Decisiones tomadas', reportDecisions));
    push(section('criteria', 'Cómo sabremos que está listo', criteria));
    push(section('assumptions', 'Supuestos que tomó', assumptions));
    push(section('blockers', 'Qué impide avanzar', blockers, 'danger'));
    push(section('risks', 'A tener en cuenta', risks, 'warning'));
  } else if (stage === 'execution') {
    push(section('work-completed', 'Qué completó', workCompleted, 'positive'));
    push(section('corrections', 'Correcciones aplicadas', corrections, 'positive'));
    push(section('work-next', 'Qué sigue', workNext));
    push(section('files', 'Qué cambió', files));
    push(section('verification-evidence', 'Qué comprobó', verificationEvidence, 'positive'));
    push(section('tests', 'Comprobaciones informadas', tests));
    push(section('verification', 'Qué falta comprobar', verification, 'warning'));
    push(section('blockers', 'Qué impide avanzar', blockers, 'danger'));
    push(section('risks', 'A tener en cuenta', risks, 'warning'));
    push(section('follow-up', 'Próximos pasos', followUp));
  } else if (stage === 'review') {
    push(section('findings', 'Qué encontró', findings, findings.length ? 'warning' : 'neutral'));
    push(section('corrections', 'Correcciones realizadas', corrections, 'positive'));
    push(section('verification-evidence', 'Evidencia de la revisión', verificationEvidence, 'positive'));
    push(section('tests', 'Comprobaciones informadas', tests));
    push(section('blockers', 'Qué hay que corregir', blockers, 'danger'));
    push(section('verification', 'Qué falta comprobar', verification, 'warning'));
    push(section('risks', 'A tener en cuenta', risks, 'warning'));
    push(section('follow-up', 'Próximos pasos', followUp));
    push(section('work-next', 'Qué sigue', workNext));
  } else {
    push(section('work-completed', 'Trabajo realizado', workCompleted, 'positive'));
    push(section('corrections', 'Correcciones realizadas', corrections, 'positive'));
    push(section('findings', 'Resultado de la revisión', findings, findings.length ? 'warning' : 'neutral'));
    push(section('files', 'Cambios realizados', files));
    push(section('verification-evidence', 'Qué se comprobó', verificationEvidence, 'positive'));
    push(section('tests', 'Comprobaciones informadas', tests));
    push(section('blockers', 'Qué queda por resolver', blockers, 'danger'));
    push(section('verification', 'Qué falta comprobar', verification, 'warning'));
    push(section('risks', 'A tener en cuenta', risks, 'warning'));
    push(section('follow-up', 'Próximos pasos', followUp));
    push(section('work-next', 'Qué sigue', workNext));
  }

  const technicalSections: PresentationSection[] = [];
  const technical = [
    section('commands', 'Comandos ejecutados', stringList(report.commands_run)),
    section('constraints', 'Límites considerados', stringList(report.constraints)),
    stage === 'planning' ? null : section('assumptions', 'Supuestos', assumptions),
    section('alternatives', 'Opciones descartadas', stringList(report.alternatives_rejected)),
  ];
  for (const candidate of technical) if (candidate) technicalSections.push(candidate);

  const finalNeedsAttention = [
    'needs_changes', 'blocked', 'partial', 'failed', 'inconclusive',
  ].includes(status) || ['changes_required', 'inconclusive'].includes(reviewDecision);
  const attentionTitle = status === 'failed'
    ? 'El trabajo necesita atención'
    : status === 'inconclusive' || reviewDecision === 'inconclusive'
      ? 'No se pudo confirmar el resultado'
      : 'Hay cambios pendientes';
  return {
    title: stage === 'final'
      ? finalNeedsAttention
        ? attentionTitle
        : overall === 'complete'
          ? 'Trabajo listo'
          : 'Resultado hasta ahora'
      : '',
    tone: stage === 'final'
      ? finalNeedsAttention
        ? 'warning'
        : overall === 'complete'
          ? 'positive'
          : 'neutral'
      : 'neutral',
    status,
    reviewDecision,
    summary: cleanText(report.summary, MAX_SUMMARY_LENGTH),
    facts,
    sections,
    technicalSections,
  };
}

function reportFromStep(step: JsonRecord): JsonRecord {
  const direct = record(step.report);
  if (Object.keys(direct).length) return direct;
  const output = record(step.output);
  const final = record(output.final_report);
  if (Object.keys(final).length) return final;
  const stepFinal = record(step.final_report);
  return Object.keys(stepFinal).length ? stepFinal : {};
}

function fallbackStages(item: JsonRecord): JsonRecord[] {
  const workflow = record(item.workflow);
  const steps = records(workflow.steps);
  if (steps.length) {
    return steps.map((step) => {
      const report = reportFromStep(step);
      return {
        ...step,
        id: canonicalStage(step.phase ?? step.step_key ?? step.key),
        state: stageState(step.status, report.status),
        outcome: report.status,
        round_count: positiveInteger(step.round_number, 0) + 1,
        report,
        technical: {
          step: cleanText(step.step_key),
          strategy: cleanText(step.strategy),
          participants: records(step.participants).map((participant) => ({
            profile: cleanText(participant.profile_name ?? participant.profile),
            provider: cleanText(participant.provider),
            model: cleanText(participant.model ?? participant.agent),
            attempts: Array.isArray(participant.attempts)
              ? participant.attempts.length
              : positiveInteger(participant.attempt_count, 0),
          })),
        },
      };
    });
  }
  return records(item.phases).map((phase) => ({
    ...phase,
    id: canonicalStage(phase.phase ?? phase.key),
    state: stageState(phase.status),
    outcome: cleanText(phase.status),
    round_count: positiveInteger(phase.round, 0) + 1,
    report: record(phase.report),
    technical: { participants: phase.participants },
  }));
}

function technicalRows(value: unknown): TechnicalRow[] {
  const rows: TechnicalRow[] = [];
  const technical = record(value);
  for (const [index, participant] of records(technical.participants).entries()) {
    if (rows.length >= 16) break;
    const profile = cleanText(participant.profile, 240) || `Participante ${index + 1}`;
    const provider = cleanText(participant.provider, 240);
    const model = cleanText(participant.model_or_agent ?? participant.model ?? participant.agent, 320);
    const internalState = cleanText(participant.state, 120).toLowerCase();
    const stateLabel = ['succeeded', 'completed', 'approved'].includes(internalState)
      ? 'completado'
      : ['running', 'dispatching'].includes(internalState)
        ? 'en curso'
        : ['failed', 'blocked', 'needs_changes'].includes(internalState)
          ? 'necesita atención'
          : internalState;
    const attempts = positiveInteger(participant.attempt_count ?? participant.attempts, 0);
    const details = [
      provider ? `Proveedor: ${provider}` : '',
      model ? `Modelo o agente: ${model}` : '',
      stateLabel ? `Estado: ${stateLabel}` : '',
      attempts ? plural(attempts, 'intento', 'intentos') : '',
    ].filter(Boolean);
    rows.push({ label: profile, value: details.join(' · ') || 'Sin detalles adicionales' });
  }
  for (const [key, raw] of Object.entries(technical)) {
    if (['participants', 'commands_run', 'constraints', 'alternatives_rejected'].includes(key)) continue;
    if (rows.length >= 16 || raw === undefined || raw === null || raw === '') continue;
    let rendered = '';
    if (typeof raw === 'string' || typeof raw === 'number' || typeof raw === 'boolean') rendered = cleanText(raw, 2_000);
    else {
      try {
        rendered = cleanText(JSON.stringify(raw), 2_000);
      } catch {
        rendered = '';
      }
    }
    if (rendered) rows.push({ label: key.replace(/_/g, ' '), value: rendered });
  }
  return rows;
}

function stageTechnicalSections(value: unknown): PresentationSection[] {
  const technical = record(value);
  return [
    section('commands', 'Comandos ejecutados', stringList(technical.commands_run)),
    section('constraints', 'Límites considerados', stringList(technical.constraints)),
    section('alternatives', 'Opciones descartadas', stringList(technical.alternatives_rejected)),
  ].filter((item): item is PresentationSection => item !== null);
}

function stageStatusLabel(id: CanonicalStageId, state: WorkItemStageState, outcome: string, roundCount: number): string {
  if (state === 'active') {
    if (id === 'planning') return 'Trabajando ahora';
    if (id === 'execution' && roundCount > 1) return 'Aplicando correcciones';
    if (id === 'execution') return 'Trabajando ahora';
    return 'Revisando ahora';
  }
  if (state === 'attention') {
    if (id === 'review' && ['needs_changes', 'changes_required', 'partial'].includes(outcome)) return 'Hay correcciones pendientes';
    if (id === 'review' && outcome === 'inconclusive') return 'Revisión inconclusa';
    if (id === 'review' && outcome === 'failed') return 'No se pudo completar la revisión';
    return 'Necesita atención';
  }
  if (state === 'cancelled') return 'Etapa cancelada';
  if (state === 'skipped') return 'No fue necesaria';
  if (state === 'complete') {
    if (id === 'planning') return 'Plan listo';
    if (id === 'execution') return 'Cambios realizados';
    if (outcome === 'needs_changes' || outcome === 'changes_required') return 'Hay correcciones pendientes';
    return 'Todo revisado';
  }
  return 'Todavía no empezó';
}

function normalizeStage(raw: JsonRecord, id: CanonicalStageId, historyValues: JsonRecord[], nowMs: number): StagePresentation {
  const report = reportPresentation(raw.report, id);
  const outcome = cleanText(raw.outcome ?? record(raw.report).status, 120).toLowerCase();
  const state = stageState(raw.state ?? raw.status, outcome);
  // While a correction/review round is active, progress v1 intentionally
  // keeps the latest completed report.  It belongs in history, not under the
  // current round where it would look like a fresh result.
  const visibleReport = state === 'active' ? reportPresentation({}, id) : report;
  const roundCount = Math.max(positiveInteger(raw.round_count, 1), historyValues.length + 1, 1);
  const embeddedHistory = records(raw.history);
  // Progress v1 defines ``history`` as previous rounds only. Fallback callers
  // pass their previous steps through ``historyValues`` using the same rule.
  const history = [
    ...historyValues,
    ...embeddedHistory,
  ].slice(-10).map((entry, index) => {
    const entryReport = reportPresentation(entry.report ?? reportFromStep(entry), id);
    const entryOutcome = cleanText(entry.outcome ?? record(entry.report).status, 120);
    const entryState = stageState(entry.state ?? entry.status, entryOutcome);
    const round = index + 1;
    return {
      round,
      label: `Ronda ${Math.max(1, round)}`,
      stateLabel: stageStatusLabel(id, entryState, entryOutcome, round),
      summary: entryReport.summary,
    };
  });
  let purpose = STAGE_COPY[id].pending;
  if (state === 'active') purpose = STAGE_COPY[id].active;
  else if (state === 'complete') purpose = id === 'planning'
    ? 'El plan está listo.'
    : id === 'execution'
      ? 'Los cambios están hechos.'
      : 'La revisión no encontró problemas pendientes.';
  else if (state === 'attention') purpose = id === 'review'
    ? ['needs_changes', 'changes_required', 'partial'].includes(outcome)
      ? 'La revisión encontró algo que hay que corregir.'
      : outcome === 'inconclusive'
        ? 'La revisión no reunió evidencia suficiente para dar una conclusión.'
        : 'La revisión necesita atención antes de poder continuar.'
    : 'Esta etapa necesita atención antes de poder continuar.';
  else if (state === 'cancelled') purpose = 'La sesión se detuvo antes de completar esta etapa.';
  else if (state === 'skipped') purpose = 'Esta etapa no fue necesaria para completar el trabajo.';

  return {
    id,
    title: STAGE_COPY[id].title,
    subtitle: STAGE_COPY[id].subtitle,
    purpose,
    state,
    statusLabel: stageStatusLabel(id, state, outcome, roundCount),
    outcome,
    summary: visibleReport.summary,
    roundCount,
    startedAt: cleanText(raw.started_at, 120),
    completedAt: cleanText(raw.completed_at, 120),
    durationLabel: formatStageDuration(raw.started_at, raw.completed_at, state, nowMs),
    facts: visibleReport.facts,
    sections: visibleReport.sections,
    milestones: [],
    deliverables: [],
    history,
    technicalRows: technicalRows(raw.technical),
    technicalSections: [
      ...visibleReport.technicalSections,
      ...stageTechnicalSections(raw.technical),
    ],
  };
}

function groupedStages(rawStages: JsonRecord[], nowMs: number): StagePresentation[] {
  return STAGE_ORDER.map((id) => {
    const matches = rawStages.filter((stage) => canonicalStage(stage.id ?? stage.phase ?? stage.key) === id);
    if (!matches.length) return normalizeStage({ id, state: 'pending', report: {} }, id, [], nowMs);
    const activeIndex = matches.findIndex((stage) => stageState(stage.state ?? stage.status, stage.outcome) === 'active');
    const selectedIndex = activeIndex >= 0 ? activeIndex : matches.length - 1;
    const selected = matches[selectedIndex];
    const history = matches.filter((_, index) => index !== selectedIndex);
    return normalizeStage(selected, id, history, nowMs);
  });
}

function milestoneLabel(value: unknown): string {
  const normalized = cleanText(value, 180).toLowerCase().replace(/[.\s-]+/g, '_');
  if (!normalized) return '';
  if (normalized.startsWith('created_')) return 'La sesión quedó preparada';
  if (normalized.includes('recovery') || normalized.includes('recovering')) return 'Baldr retomó el trabajo';
  if (normalized.startsWith('cancelled_')) return 'La sesión fue cancelada';
  if (normalized.includes('stage_skipped')) return 'La etapa no fue necesaria';
  if (normalized.includes('planning') && normalized.includes('start')) return 'Comenzó a organizar el trabajo';
  if (normalized.includes('analyz')) return 'Analizó el pedido';
  if (normalized.includes('research')) return 'Buscó información útil';
  if (normalized.includes('changing')) return 'Comenzó a preparar los cambios';
  if (normalized.includes('verifying')) return 'Comenzó a comprobar el resultado';
  if (normalized.includes('planning') && /(complete|ready|succeed)/.test(normalized)) return 'El plan quedó listo';
  if ((normalized.includes('implementation') || normalized.includes('implement') || normalized.includes('execution')) && normalized.includes('start')) return 'Comenzó a hacer los cambios';
  if ((normalized.includes('implementation') || normalized.includes('implement') || normalized.includes('execution')) && /(complete|ready|succeed)/.test(normalized)) return 'Terminó de hacer los cambios';
  if (normalized.includes('fix') && normalized.includes('start')) return 'Comenzó a aplicar correcciones';
  if (normalized.includes('review') && normalized.includes('start')) return 'Comenzó a comprobar el resultado';
  if (normalized.includes('review') && (normalized.includes('change') || normalized.includes('block') || normalized.includes('attention'))) return 'La revisión encontró algo para corregir';
  if (normalized.includes('review') && normalized.includes('approv')) return 'La revisión quedó aprobada';
  if (normalized.includes('review') && normalized.includes('complete')) return 'Terminó de comprobar el resultado';
  if (normalized.includes('checkpoint_verified')) return 'Baldr guardó un punto seguro comprobado';
  if ((normalized.includes('publish') || normalized.includes('publication')) && (normalized.includes('start') || normalized.includes('running'))) return 'Comenzó a guardar el resultado';
  if (normalized.includes('publish') || normalized.includes('publication')) return 'El resultado quedó guardado';
  if (normalized.includes('attention') || normalized.includes('reconciliation')) return 'Baldr necesita una decisión para continuar';
  if (normalized.startsWith('started_') || normalized.includes('work_item_started') || normalized.includes('workflow_created')) return 'La sesión comenzó';
  if (normalized.startsWith('completed_')) return 'El trabajo quedó listo';
  return '';
}

function milestones(value: unknown): MilestonePresentation[] {
  return records(value).map((entry, index) => {
    const kind = entry.kind ?? entry.type ?? entry.event ?? entry.event_type ?? entry.id;
    const normalizedKind = cleanText(kind, 120).toLowerCase();
    const state = cleanText(entry.state, 120).toLowerCase();
    // Live provider activity belongs in the Now card. Treating an in-flight
    // observation as a completed milestone would falsely put a checkmark on it.
    if (['analyzing', 'researching', 'changing', 'verifying', 'working'].includes(normalizedKind)) {
      return null;
    }
    const semanticKey = [kind, entry.stage, entry.state].filter(Boolean).join('_');
    return {
      id: cleanText(entry.id, 160) || `milestone-${index}`,
      label: milestoneLabel(semanticKey),
      occurredAt: cleanText(entry.occurred_at ?? entry.created_at ?? entry.at, 120),
      state,
      evidence: cleanText(entry.evidence, 120).toLowerCase(),
      stage: canonicalStage(entry.stage),
    };
  }).filter((entry): entry is MilestonePresentation => Boolean(entry?.label)).slice(-40);
}

function attentionPresentation(raw: unknown, overall: WorkItemOverallState): AttentionPresentation | null {
  const value = record(raw);
  if (!Object.keys(value).length && overall !== 'attention') return null;
  const retryable = typeof value.retryable === 'boolean' ? value.retryable : null;
  return {
    title: cleanText(value.title, 240) || 'Necesitamos que elijas cómo continuar',
    message: cleanText(value.user_message ?? value.message ?? value.summary, MAX_SUMMARY_LENGTH)
      || 'El trabajo está preservado. Revisá las opciones disponibles antes de continuar.',
    actionLabel: cleanText(value.action_label, 120) || (retryable ? 'Volver a intentar' : 'Elegir cómo continuar'),
    blockers: stringList(value.blockers),
    retryable,
  };
}

function fallbackRunState(item: JsonRecord): unknown {
  const workflow = record(item.workflow);
  const run = record(workflow.run);
  return run.status ?? item.status;
}

function fallbackFinalReport(item: JsonRecord): JsonRecord {
  const workflow = record(item.workflow);
  const run = record(workflow.run);
  const final = record(run.final);
  if (Object.keys(final).length) return final;
  return record(item.final_report);
}

export function buildWorkItemPresentation(value: unknown, nowMs = Date.now()): WorkItemPresentation {
  const item = record(value);
  const progress = record(item.progress);
  const declaresVersion = progress.version !== undefined && progress.version !== null && progress.version !== '';
  const hasV1Progress = progress.contract === 'baldr-work-item-progress' && Number(progress.version) === 1;
  const hasLegacyProgressShape = !declaresVersion && Array.isArray(progress.stages);
  const hasSupportedProgress = hasV1Progress || hasLegacyProgressShape;
  const hasUnknownProgressVersion = declaresVersion && !hasV1Progress;
  const supportedProgress = hasSupportedProgress ? progress : {};
  const rawStages = hasSupportedProgress
    ? records(progress.stages)
    : hasUnknownProgressVersion ? [] : fallbackStages(item);
  const stages = groupedStages(rawStages, nowMs);
  const rawOverall = hasSupportedProgress ? progress.overall_state : fallbackRunState(item);
  let overall = overallState(rawOverall);
  const progressTechnical = record(supportedProgress.technical);
  if (
    hasSupportedProgress
    && cleanText(rawOverall, 120).toLowerCase() === 'pending'
    && cleanText(progressTechnical.run_state, 120).toLowerCase() === 'pending'
  ) {
    overall = 'queued';
  }
  const explicitActive = canonicalStage(supportedProgress.active_stage);
  const inferredActive = stages.find((stage) => stage.state === 'active')?.id ?? '';
  let activeStage: CanonicalStageId | '' = explicitActive || inferredActive;
  if (overall === 'cancelled' || overall === 'archived') activeStage = '';
  const selectedActivity = activity(supportedProgress.activity, overall, activeStage);
  if (selectedActivity === 'cancelling') {
    for (const stage of stages) {
      if (stage.state === 'active') {
        stage.statusLabel = 'Deteniendo esta etapa';
        stage.purpose = 'Baldr está deteniendo esta etapa de forma segura.';
      } else if (stage.state === 'pending') {
        stage.statusLabel = 'No se iniciará';
        stage.purpose = 'Esta etapa no se iniciará mientras Baldr termina de detener la sesión.';
      }
    }
  } else if (overall === 'cancelled') {
    for (const stage of stages) {
      if (stage.state === 'active' || stage.state === 'pending') {
        stage.state = 'cancelled';
        stage.statusLabel = 'Etapa cancelada';
        stage.purpose = 'La sesión se detuvo antes de completar esta etapa.';
      }
    }
  } else if (overall === 'archived') {
    for (const stage of stages) {
      if (stage.state === 'active' || stage.state === 'pending') {
        stage.state = 'skipped';
        stage.statusLabel = 'No se realizó';
        stage.purpose = 'La sesión se archivó antes de completar esta etapa.';
      }
    }
  }
  const copy = headlineCopy(selectedActivity, overall, activeStage);
  const activityValue = record(supportedProgress.activity);
  const finalReport = hasSupportedProgress
    ? record(progress.final_report)
    : hasUnknownProgressVersion ? {} : fallbackFinalReport(item);
  const hasFinalReport = Object.keys(finalReport).length > 0;
  const revision = cleanText(supportedProgress.revision, 160)
    || cleanText(supportedProgress.last_event_at, 160)
    || cleanText(item.updated_at, 160)
    || `${overall}:${activeStage}:${stages.map((stage) => `${stage.id}-${stage.state}-${stage.roundCount}`).join('|')}`;
  const workflow = record(item.workflow);
  const run = record(workflow.run);
  const globalTechnical = hasSupportedProgress ? supportedProgress.technical : {
    run_id: item.current_run_id ?? run.id,
    workflow: run.workflow_name,
    internal_status: run.status ?? item.status,
    recovery_count: run.recovery_count,
  };
  const presentedMilestones = milestones(supportedProgress.milestones);
  const presentedDeliverables = buildPhaseDeliverablePresentations(supportedProgress.deliverables);
  for (const stage of stages) {
    stage.milestones = presentedMilestones
      .filter((entry) => entry.stage === stage.id)
      .slice(-6);
    stage.deliverables = presentedDeliverables
      .filter((entry) => entry.stage === stage.id);
  }

  return {
    version: 1,
    revision,
    overallState: overall,
    activity: selectedActivity,
    activeStage,
    headline: copy.headline,
    explanation: copy.explanation,
    lastEventAt: cleanText(supportedProgress.last_event_at ?? activityValue.since ?? item.updated_at, 120),
    stages,
    deliverableIndex: deliverableIndex(supportedProgress.deliverable_index),
    outcome: hasFinalReport || overall === 'complete' ? reportPresentation(finalReport, 'final', overall) : null,
    attention: attentionPresentation(supportedProgress.attention, overall),
    milestones: presentedMilestones,
    technicalRows: technicalRows(globalTechnical),
  };
}

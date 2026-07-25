/**
 * Single source of truth for Baldr console copy and the "+" action catalog.
 *
 * The extension used to declare the same things three times: once for the
 * quick pick, once for the inline webview menu, and once inside the embedded
 * webview script. The two menus had already drifted (four actions were
 * reachable from the gear but not from "+"), and the same preference was named
 * differently depending on where the user looked.
 *
 * This module has no `vscode` import so it can be unit tested directly and
 * injected into the webview as data.
 */

export type PlusActionGroup = 'add' | 'preferences' | 'tools';

export interface PlusAction {
  /** Identifier consumed by `handlePlusAction`. */
  readonly id: string;
  readonly label: string;
  readonly detail: string;
  readonly group: PlusActionGroup;
  /** Codicon for the quick pick surface. */
  readonly codicon: string;
  /** Text glyph for the inline webview surface, which has no codicon font. */
  readonly glyph: string;
}

export const PLUS_GROUP_HEADINGS: Record<PlusActionGroup, string> = {
  add: 'Agregar',
  preferences: 'Preferencias',
  tools: 'Herramientas',
};

export const PLUS_ACTIONS: readonly PlusAction[] = [
  {
    id: 'path',
    label: 'Archivos y carpetas',
    detail: 'Sumá material útil para el pedido',
    group: 'add',
    codicon: 'folder-opened',
    glyph: '⌕',
  },
  {
    id: 'workspace',
    label: 'Carpeta de trabajo',
    detail: 'Elegí el proyecto activo',
    group: 'add',
    codicon: 'root-folder',
    glyph: '◇',
  },
  {
    id: 'file',
    label: 'Archivo abierto',
    detail: 'Usalo como referencia',
    group: 'add',
    codicon: 'file',
    glyph: '▣',
  },
  {
    id: 'selection',
    label: 'Texto seleccionado',
    detail: 'Sumá solo la parte marcada',
    group: 'add',
    codicon: 'selection',
    glyph: '≡',
  },
  {
    id: 'draft',
    label: 'Guardar para después',
    detail: 'Creá una sesión sin empezarla todavía',
    group: 'add',
    codicon: 'add',
    glyph: '＋',
  },
  {
    id: 'git',
    label: 'Protección de cambios',
    detail: 'Elegí cómo guardar y recuperar el trabajo',
    group: 'preferences',
    codicon: 'shield',
    glyph: '⌘',
  },
  {
    id: 'preset',
    label: 'Nivel de detalle',
    detail: 'Rápido, estándar, detallado o a medida',
    group: 'preferences',
    codicon: 'dashboard',
    glyph: '◈',
  },
  {
    id: 'roles',
    label: 'Equipo de Baldr',
    detail: 'Elegí modelos y cómo se reparte el trabajo',
    group: 'preferences',
    codicon: 'organization',
    glyph: '◌',
  },
  {
    id: 'context',
    label: 'Ayuda adicional',
    detail: 'Buscá información útil cuando haga falta',
    group: 'preferences',
    codicon: 'sparkle',
    glyph: '?',
  },
  {
    id: 'agents',
    label: 'Agentes externos',
    detail: 'Consultá y asigná agentes registrados de forma segura',
    group: 'tools',
    codicon: 'remote-explorer',
    glyph: '◎',
  },
  {
    id: 'profile-create',
    label: 'Configuración avanzada',
    detail: 'Elegí proveedor y modelo paso a paso',
    group: 'tools',
    codicon: 'tools',
    glyph: '⚙',
  },
  {
    id: 'qualification',
    label: 'Calificar VS Code + Codex',
    detail: 'Ejecutá los gates reales y abrí la evidencia pendiente',
    group: 'tools',
    codicon: 'verified-filled',
    glyph: '✓',
  },
  {
    id: 'status',
    label: 'Actualizar',
    detail: 'Volvé a cargar las sesiones y su estado',
    group: 'tools',
    codicon: 'refresh',
    glyph: '↻',
  },
  {
    id: 'logs',
    label: 'Ver detalles técnicos',
    detail: 'Abrí el registro de Baldr',
    group: 'tools',
    codicon: 'output',
    glyph: '⌸',
  },
];

export const PLUS_ACTION_IDS: readonly string[] = PLUS_ACTIONS.map((action) => action.id);

export function escapeHtml(value: string): string {
  return value.replace(/[&<>"']/g, (character) => ({
    '&': '&amp;',
    '<': '&lt;',
    '>': '&gt;',
    '"': '&quot;',
    "'": '&#39;',
  }[character] as string));
}

/** Build the inline "+" menu from the shared catalog. */
export function plusMenuHtml(): string {
  const groups: PlusActionGroup[] = ['add', 'preferences', 'tools'];
  const options = groups.map((group, index) => {
    const heading = index === 0
      ? `<div class="plus-menu-heading" data-plus-heading="${group}">${escapeHtml(PLUS_GROUP_HEADINGS[group])}</div>`
      : `<div class="plus-menu-group" data-plus-heading="${group}">${escapeHtml(PLUS_GROUP_HEADINGS[group])}</div>`;
    const entries = PLUS_ACTIONS.filter((action) => action.group === group).map((action) => (
      `<button type="button" class="plus-option" data-plus-action="${escapeHtml(action.id)}" data-plus-group="${group}">`
      + `<span class="plus-option-icon" aria-hidden="true">${escapeHtml(action.glyph)}</span>`
      + `<span><span class="plus-option-label">${escapeHtml(action.label)}</span>`
      + `<span class="plus-option-detail">${escapeHtml(action.detail)}</span></span></button>`
    ));
    return [heading, ...entries].join('\n      ');
  });
  // The filter comes before the options so keyboard order matches reading
  // order; it used to sit after them and broke tab sequence.
  return [
    '<input class="plus-filter" id="plusFilter" type="search" placeholder="Buscar opciones" aria-label="Buscar opciones de Baldr">',
    ...options,
    '<div class="plus-empty" id="plusEmpty" hidden>No encontramos una opción con ese nombre.</div>',
  ].join('\n      ');
}

export interface PlusQuickPickItem {
  readonly id: string;
  readonly label: string;
  readonly description: string;
}

/** Build the gear quick pick from the same catalog the inline menu uses. */
export function plusQuickPickItems(): PlusQuickPickItem[] {
  return PLUS_ACTIONS.map((action) => ({
    id: action.id,
    label: `$(${action.codicon}) ${action.label}`,
    description: action.detail,
  }));
}

/**
 * Preference and status wording shared by the host surfaces and the webview.
 *
 * Injected into the webview as JSON so a chip and a quick pick can never name
 * the same setting differently.
 */
export const UI_LABELS = {
  safety: {
    automatic: 'Pedir autorización',
    worktree: 'Copia aislada',
    current: 'Trabajar directamente',
    'non-git': 'Sin protección',
  } as Record<string, string>,
  preset: {
    fast: 'Rápido',
    balanced: 'Estándar',
    deep: 'Detallado',
    custom: 'A medida',
  } as Record<string, string>,
  context: {
    auto: 'Ayuda automática',
    on: 'Ayuda activa',
    off: 'Ayuda desactivada',
  } as Record<string, string>,
  status: {
    draft: 'Pendiente',
    queued: 'En espera',
    running: 'En curso',
    cancelling: 'Cancelando',
    completed: 'Lista',
    archived: 'Archivada',
    failed: 'Necesita atención',
    cancelled: 'Cancelada',
    needs_attention: 'Necesita atención',
  } as Record<string, string>,
  phase: {
    architecture: 'Planificación',
    architect: 'Planificación',
    implementation: 'Ejecución',
    implementer: 'Ejecución',
    review: 'Revisión',
    reviewer: 'Revisión',
  } as Record<string, string>,
  role: {
    architect: 'Planificación',
    implementer: 'Ejecución',
    reviewer: 'Revisión',
  } as Record<string, string>,
} as const;

export const UI_LABEL_FALLBACKS = {
  safety: 'Trabajar directamente',
  preset: 'Estándar',
  context: 'Ayuda automática',
  status: 'Pendiente',
  phase: 'Etapa',
} as const;

export function safetyModeLabel(value: string): string {
  return UI_LABELS.safety[value] ?? UI_LABEL_FALLBACKS.safety;
}

export function presetModeLabel(value: string): string {
  return UI_LABELS.preset[value] ?? UI_LABEL_FALLBACKS.preset;
}

export function contextModeLabel(value: string): string {
  return UI_LABELS.context[value] ?? UI_LABEL_FALLBACKS.context;
}

export function roleLabel(role: 'architect' | 'implementer' | 'reviewer'): string {
  return (UI_LABELS.role[role] ?? role).toLowerCase();
}

/**
 * Human copy for the durable error codes a session can stop on.
 *
 * The view used to fall back to the raw `error_code`, so a user could be shown
 * `workspace_reconciliation_required` with no idea what to do next.
 */
export const WORK_ITEM_ERROR_COPY: Record<string, string> = {
  workspace_reconciliation_required:
    'La sesión se detuvo con cambios sin confirmar. Revisá las opciones para aplicarlos o descartarlos.',
  workspace_git_required:
    'Esta opción necesita un repositorio Git. Elegí otra protección de cambios o abrí una carpeta con Git.',
  workspace_non_git_confirmation_required:
    'Esta carpeta no usa Git. Confirmá que querés trabajar sin protección antes de continuar.',
  workflow_phase_failed:
    'Una de las etapas no pudo completarse. Revisá el detalle de la etapa y volvé a intentar.',
  phase_report_blocked:
    'La etapa terminó sin un informe válido. Volvé a intentar o revisá los detalles técnicos.',
  phase_min_successes_not_met:
    'La etapa no alcanzó los resultados mínimos esperados. Revisá el detalle antes de continuar.',
  architecture_conflict:
    'La planificación quedó con decisiones en conflicto. Indicá cuál preferís para continuar.',
  idempotency_conflict:
    'Ya existe un pedido igual en curso. Esperá a que termine o revisá la sesión existente.',
};

export const GENERIC_WORK_ITEM_ERROR =
  'La sesión se detuvo por un problema técnico. Abrí los detalles técnicos para ver el registro.';

export interface WorkItemErrorSource {
  readonly error_code?: unknown;
  readonly error_reason?: unknown;
  readonly safety_mode?: unknown;
}

/**
 * Turn a durable failure into copy a user can act on.
 *
 * A human `error_reason` is preferred when present. A bare code never reaches
 * the user: unknown codes fall back to generic copy that points at the log.
 */
export function workItemErrorMessage(item: WorkItemErrorSource): string {
  const code = typeof item.error_code === 'string' ? item.error_code : '';
  const reason = typeof item.error_reason === 'string' ? item.error_reason.trim() : '';
  if (code === 'workspace_reconciliation_required' && item.safety_mode === 'non-git') {
    return 'La sesión se detuvo al intentar crear un respaldo Git que esta carpeta no usa.'
      + ' Tus archivos siguen en la carpeta: revisá las opciones para continuar con ellos.';
  }
  if (WORK_ITEM_ERROR_COPY[code]) return WORK_ITEM_ERROR_COPY[code] as string;
  if (reason && reason !== code) return reason;
  if (code) return GENERIC_WORK_ITEM_ERROR;
  return '';
}

/** Payload injected into the webview so it shares this module's wording. */
export function webviewCopyPayload(): string {
  return JSON.stringify({
    labels: UI_LABELS,
    fallbacks: UI_LABEL_FALLBACKS,
    errors: WORK_ITEM_ERROR_COPY,
    genericError: GENERIC_WORK_ITEM_ERROR,
  });
}

export type ConsoleCommand =
  | { kind: 'task'; task: string }
  | { kind: 'new'; task: string }
  | { kind: 'run'; task: string }
  | { kind: 'status' }
  | { kind: 'profile'; value?: string }
  | { kind: 'git'; value?: string }
  | { kind: 'context'; value?: string }
  | { kind: 'roles' }
  | { kind: 'cancel' }
  | { kind: 'resume' }
  | { kind: 'archive' }
  | { kind: 'restore' }
  | { kind: 'delete' }
  | { kind: 'setup' }
  | { kind: 'help' }
  | { kind: 'unknown'; command: string; argument: string };

/**
 * Semantic progress is owned by baldr-router.  The extension deliberately
 * keeps the wire values separate from its Spanish presentation copy so other
 * clients can render the same durable state without inheriting UI wording.
 */
export type WorkItemOverallState =
  | 'draft'
  | 'queued'
  | 'working'
  | 'finalizing'
  | 'attention'
  | 'complete'
  | 'cancelled'
  | 'archived';

export type WorkItemActivity =
  | 'waiting'
  | 'preparing_workspace'
  | 'working'
  | 'analyzing'
  | 'researching'
  | 'planning'
  | 'changing'
  | 'implementing'
  | 'fixing'
  | 'verifying'
  | 'reviewing'
  | 'publishing'
  | 'recovering'
  | 'cancelling'
  | 'completed'
  | 'cancelled'
  | 'attention'
  | 'waiting_for_choice'
  | 'archived'
  | 'finished';

export type CanonicalStageId = 'planning' | 'execution' | 'review';
export type WorkItemStageState = 'pending' | 'active' | 'complete' | 'attention' | 'cancelled' | 'skipped';

export interface WorkItemProgressReport {
  status?: unknown;
  summary?: unknown;
  interpretation?: unknown;
  scope?: unknown;
  approach?: unknown;
  plan_steps?: unknown;
  work_completed?: unknown;
  work_next?: unknown;
  findings?: unknown;
  corrections?: unknown;
  verification_evidence?: unknown;
  files_modified?: unknown;
  commands_run?: unknown;
  tests_run?: unknown;
  verification_needed?: unknown;
  risks?: unknown;
  follow_up?: unknown;
  decisions?: unknown;
  constraints?: unknown;
  assumptions?: unknown;
  alternatives_rejected?: unknown;
  acceptance_criteria?: unknown;
  blockers?: unknown;
  review_decision?: unknown;
  [key: string]: unknown;
}

export interface WorkItemProgressStage {
  id?: CanonicalStageId;
  state?: string;
  outcome?: string | null;
  round_count?: number;
  started_at?: string | null;
  completed_at?: string | null;
  report?: WorkItemProgressReport | null;
  history?: Array<Record<string, unknown>>;
  technical?: Record<string, unknown>;
  [key: string]: unknown;
}

export interface WorkItemProgressActivity {
  kind?: string;
  message?: string;
  since?: string | null;
  state?: string;
  stage?: CanonicalStageId | null;
}

export interface WorkItemProgressMilestone {
  kind?: string;
  stage?: CanonicalStageId | null;
  state?: string;
  message?: string;
  at?: string | null;
}

export interface WorkItemProgressAttention {
  required?: boolean;
  kind?: string;
  stage?: CanonicalStageId | null;
  summary?: string;
  blockers?: string[];
  actions?: Array<{ id?: string; label?: string; description?: string }>;
  retryable?: boolean | null;
}

export interface WorkItemProgressV1 {
  contract?: string;
  version?: 1;
  revision?: string | number;
  overall_state?: string;
  activity?: WorkItemProgressActivity;
  active_stage?: CanonicalStageId | null;
  last_event_at?: string | null;
  stages?: WorkItemProgressStage[];
  final_report?: WorkItemProgressReport | null;
  attention?: WorkItemProgressAttention | null;
  milestones?: WorkItemProgressMilestone[];
  technical?: Record<string, unknown>;
  [key: string]: unknown;
}

export const SLASH_COMMANDS = [
  { command: '/new', detail: 'Create a draft work item' },
  { command: '/run', detail: 'Start the selected item or create and run a task' },
  { command: '/status', detail: 'Refresh Baldr status' },
  { command: '/profile', detail: 'Choose fast, balanced, deep, or custom' },
  { command: '/git', detail: 'Choose automatic, current, or off' },
  { command: '/context', detail: 'Choose auto, on, or off' },
  { command: '/roles', detail: 'Choose architecture, implementation, and review profiles' },
  { command: '/cancel', detail: 'Cancel the selected item' },
  { command: '/resume', detail: 'Reconcile or resume the selected item' },
  { command: '/archive', detail: 'Archive the selected item' },
  { command: '/restore', detail: 'Restore the selected archived item' },
  { command: '/delete', detail: 'Permanently delete the selected archived item' },
  { command: '/setup', detail: 'Run guided Baldr setup' },
  { command: '/help', detail: 'Show available commands' },
] as const;

export function parseConsoleInput(raw: string): ConsoleCommand {
  const input = raw.trim();
  if (!input) return { kind: 'help' };
  if (!input.startsWith('/')) return { kind: 'task', task: input };
  const [head, ...rest] = input.split(/\s+/);
  const command = head.slice(1).toLowerCase();
  const argument = rest.join(' ').trim();
  if (command === 'new') return { kind: 'new', task: argument };
  if (command === 'run') return { kind: 'run', task: argument };
  if (command === 'status') return { kind: 'status' };
  if (command === 'profile') return { kind: 'profile', value: argument || undefined };
  if (command === 'git') return { kind: 'git', value: argument || undefined };
  if (command === 'context') return { kind: 'context', value: argument || undefined };
  if (command === 'roles') return { kind: 'roles' };
  if (command === 'cancel') return { kind: 'cancel' };
  if (command === 'resume') return { kind: 'resume' };
  if (command === 'archive') return { kind: 'archive' };
  if (command === 'restore') return { kind: 'restore' };
  if (command === 'delete') return { kind: 'delete' };
  if (command === 'setup') return { kind: 'setup' };
  if (command === 'help') return { kind: 'help' };
  return { kind: 'unknown', command, argument };
}

export function statusGroup(status: string): 'running' | 'attention' | 'draft' | 'completed' {
  if (status === 'running' || status === 'cancelling') return 'running';
  if (status === 'needs_attention' || status === 'failed') return 'attention';
  if (status === 'completed' || status === 'cancelled' || status === 'archived') return 'completed';
  return 'draft';
}

export function statusGlyph(status: string): string {
  if (status === 'completed') return '✓';
  if (status === 'running') return '●';
  if (status === 'cancelling') return '◐';
  if (status === 'needs_attention' || status === 'failed') return '⚠';
  if (status === 'cancelled') return '×';
  if (status === 'archived') return '·';
  return '○';
}

export function normalizeGitMode(value: string | undefined): 'automatic' | 'worktree' | 'current' | 'non-git' | undefined {
  const normalized = String(value ?? '').trim().toLowerCase();
  if (normalized === 'automatic' || normalized === 'auto' || normalized === 'protected') return 'automatic';
  if (normalized === 'worktree' || normalized === 'isolated') return 'worktree';
  if (normalized === 'current' || normalized === 'in-place' || normalized === 'inplace') return 'current';
  if (normalized === 'off' || normalized === 'none' || normalized === 'non-git' || normalized === 'nongit') return 'non-git';
  return undefined;
}

export function normalizePreset(value: string | undefined): 'fast' | 'balanced' | 'deep' | 'custom' | undefined {
  const normalized = String(value ?? '').trim().toLowerCase();
  return normalized === 'fast' || normalized === 'balanced' || normalized === 'deep' || normalized === 'custom'
    ? normalized
    : undefined;
}

export function normalizeContextMode(value: string | undefined): 'auto' | 'on' | 'off' | undefined {
  const normalized = String(value ?? '').trim().toLowerCase();
  return normalized === 'auto' || normalized === 'on' || normalized === 'off'
    ? normalized
    : undefined;
}

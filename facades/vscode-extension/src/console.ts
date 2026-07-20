import * as fs from 'node:fs';
import * as path from 'node:path';
import * as vscode from 'vscode';

import {
  BaldrRuntime,
  CONTEXT7_SECRET_KEY,
  type FacadeOptions,
  type JsonRecord,
  record,
  text,
} from './runtime.js';
import {
  buildPhaseDeliverablePresentations,
  buildWorkItemPresentation,
} from './workItemPresentation.js';
import { renderQualification } from './render.js';
import {
  captureWorkspaceContext,
  contextualWorkspaceRoot,
  resolveWorkspaceRoot,
} from './workspaceContext.js';

const VIEW_TYPE = 'baldr.console';
const POLL_FAST_MS = 2_500;
const POLL_STABLE_MS = 5_000;
const POLL_IDLE_MS = 10_000;
const MAX_SELECTION_CHARS = 20_000;
const RECONCILIATION_ACTIONS = new Set([
  'authorize_changes',
  'decline_changes',
  'inspect_shadow',
  'continue_from_shadow',
  'apply_shadow_changes',
  'discard_shadow',
  'resume_from_checkpoint',
  'accept_existing_changes',
  'discard_worktree',
  'mark_failed',
]);

type WorkItem = JsonRecord & {
  id?: string;
  title?: string;
  task?: string;
  status?: string;
  allowed_actions?: unknown;
};

interface ConsoleMessage {
  type?: string;
  value?: string;
  path?: string;
  itemId?: string;
  action?: string;
  index?: number;
  stage?: string;
  round?: number;
  runOrdinal?: number;
  cursor?: string;
  descriptorDigest?: string;
  requestId?: number;
}

interface PendingContext {
  attachments: JsonRecord[];
  extraContext: string;
  selectionContexts: Record<string, string>;
}

const BALDR_ROLES = ['architect', 'implementer', 'reviewer'] as const;
type BaldrRole = typeof BALDR_ROLES[number];

interface CodexModelOption {
  model: string;
  displayName: string;
  description: string;
  defaultEffort: string;
  efforts: string[];
  isDefault: boolean;
}

interface CodexTeamChoice {
  model: string;
  displayName: string;
  effort: string;
}

type SafetyMode = 'automatic' | 'worktree' | 'current' | 'non-git';

function roleLabel(role: BaldrRole): string {
  return ({
    architect: 'planificación',
    implementer: 'ejecución',
    reviewer: 'revisión',
  } as Record<BaldrRole, string>)[role];
}

function isPathInsideRoot(root: string, target: string): boolean {
  const relative = path.relative(root, target);
  return Boolean(relative)
    && relative !== '..'
    && !relative.startsWith(`..${path.sep}`)
    && !path.isAbsolute(relative);
}

function effortLabel(value: string): string {
  return ({
    minimal: 'Mínimo',
    low: 'Bajo',
    medium: 'Medio',
    high: 'Alto',
    xhigh: 'Muy alto',
    max: 'Máximo',
    ultra: 'Ultra',
  } as Record<string, string>)[value] ?? value;
}

function effortDescription(value: string): string {
  return ({
    minimal: 'Para pedidos muy simples y respuestas inmediatas',
    low: 'Prioriza velocidad en tareas sencillas',
    medium: 'Buen equilibrio entre rapidez y profundidad',
    high: 'Analiza con más profundidad antes de avanzar',
    xhigh: 'Dedica más trabajo a tareas complejas',
    max: 'Usa la mayor profundidad disponible',
    ultra: 'Máxima profundidad para los casos más exigentes',
  } as Record<string, string>)[value] ?? 'Variante disponible para este modelo';
}

function displayModelName(model: string, catalogName = ''): string {
  if (catalogName && catalogName.toLowerCase() !== model.toLowerCase()) return catalogName;
  const match = model.match(/^gpt-(\d+(?:\.\d+)*)(?:-(.+))?$/i);
  if (!match) return catalogName || model;
  const variant = match[2]
    ? ` ${match[2].split('-').map((part) => part.charAt(0).toUpperCase() + part.slice(1)).join(' ')}`
    : '';
  return `GPT-${match[1]}${variant}`;
}

function generatedProfileName(model: string, effort: string): string {
  const modelPart = model.toLowerCase().replace(/[^a-z0-9._-]+/g, '-').replace(/^-+|-+$/g, '') || 'model';
  const suffix = `-${effort || 'default'}`;
  return `baldr-${modelPart.slice(0, Math.max(1, 64 - 'baldr-'.length - suffix.length))}${suffix}`;
}

function asItems(value: unknown): WorkItem[] {
  return Array.isArray(value) ? value.filter((item): item is WorkItem => record(item) === item) : [];
}

function allowedActions(item: WorkItem | undefined): string[] {
  const raw = item?.allowed_actions;
  return Array.isArray(raw) ? raw.map(String) : [];
}

function normalizeMode(value: string): SafetyMode | undefined {
  const normalized = value.trim().toLowerCase();
  if (normalized === 'automatic' || normalized === 'auto' || normalized === 'protected') return 'automatic';
  // Keep the legacy value instead of silently rewriting saved worktree tasks;
  // the UI presents it as automatic protection, but its execution semantics
  // remain unchanged.
  if (normalized === 'worktree' || normalized === 'git') return 'worktree';
  if (normalized === 'current' || normalized === 'in-place' || normalized === 'inplace') return 'current';
  if (normalized === 'off' || normalized === 'none' || normalized === 'non-git' || normalized === 'nogit') return 'non-git';
  return undefined;
}

function normalizePreset(value: string): 'fast' | 'balanced' | 'deep' | 'custom' | undefined {
  const normalized = value.trim().toLowerCase();
  return normalized === 'fast' || normalized === 'balanced' || normalized === 'deep' || normalized === 'custom'
    ? normalized
    : undefined;
}

function normalizeContext(value: string): 'auto' | 'on' | 'off' | undefined {
  const normalized = value.trim().toLowerCase();
  if (normalized === 'auto') return 'auto';
  if (normalized === 'on' || normalized === 'yes' || normalized === 'enabled') return 'on';
  if (normalized === 'off' || normalized === 'no' || normalized === 'disabled') return 'off';
  return undefined;
}

function safetyModeLabel(value: string): string {
  return ({
    automatic: 'Pedir autorización',
    worktree: 'Copia aislada',
    current: 'Trabajar directamente',
    'non-git': 'Sin protección',
  } as Record<string, string>)[value] ?? 'Trabajar directamente';
}

function presetModeLabel(value: string): string {
  return ({ fast: 'Rápido', balanced: 'Estándar', deep: 'Detallado', custom: 'A medida' } as Record<string, string>)[value]
    ?? 'Estándar';
}

function contextModeLabel(value: string): string {
  return ({ auto: 'Automática', on: 'Siempre activa', off: 'Desactivada' } as Record<string, string>)[value]
    ?? 'Automática';
}

export class BaldrConsoleProvider implements vscode.WebviewViewProvider, vscode.Disposable {
  static readonly viewType = VIEW_TYPE;

  private view: vscode.WebviewView | undefined;
  private selectedItemId: string | undefined;
  private refreshTimer: NodeJS.Timeout | undefined;
  private pollDelayMs = POLL_FAST_MS;
  private refreshPromise: Promise<void> | undefined;
  private refreshRequested = 0;
  private refreshCompleted = 0;
  private interactiveRefreshRequested = false;
  private operationCount = 0;
  private lastStatus: JsonRecord = {};
  private pending: PendingContext = { attachments: [], extraContext: '', selectionContexts: {} };
  private selectedWorkspaceRoot: string | undefined;
  private workspacePinned: boolean;
  private selectedByWorkspace: Record<string, string>;
  private readonly disposables: vscode.Disposable[] = [];

  constructor(
    private readonly context: vscode.ExtensionContext,
    private readonly runtime: BaldrRuntime,
    private readonly output: vscode.LogOutputChannel,
  ) {
    this.selectedWorkspaceRoot = context.workspaceState.get<string>('baldr.console.workspaceRoot');
    this.workspacePinned = context.workspaceState.get<boolean>('baldr.console.workspacePinned', false);
    this.selectedByWorkspace = context.workspaceState.get<Record<string, string>>('baldr.console.selectedByWorkspace', {});
    this.selectedItemId = this.selectedWorkspaceRoot
      ? this.selectedByWorkspace[this.workspaceKey(this.selectedWorkspaceRoot)]
      : context.workspaceState.get<string>('baldr.console.selectedItemId');
    this.disposables.push(
      vscode.workspace.onDidChangeWorkspaceFolders(() => void this.refresh()),
      vscode.workspace.onDidGrantWorkspaceTrust(() => void this.refresh()),
      vscode.window.onDidChangeActiveTextEditor(() => void this.refresh()),
    );
  }

  private workspaceKey(root: string): string {
    return process.platform === 'win32' ? path.resolve(root).toLowerCase() : path.resolve(root);
  }

  private currentWorkspaceRoot(): string | undefined {
    const pinned = this.workspacePinned && this.selectedWorkspaceRoot
      ? vscode.workspace.workspaceFolders?.find(
        (folder) => this.workspaceKey(folder.uri.fsPath) === this.workspaceKey(this.selectedWorkspaceRoot as string),
      )?.uri.fsPath
      : undefined;
    const root = pinned ?? contextualWorkspaceRoot(this.selectedWorkspaceRoot);
    if (root && this.workspaceKey(root) !== this.workspaceKey(this.selectedWorkspaceRoot ?? root)) {
      this.selectedWorkspaceRoot = root;
      this.selectedItemId = this.selectedByWorkspace[this.workspaceKey(root)];
      void this.context.workspaceState.update('baldr.console.workspaceRoot', root);
    } else if (root && !this.selectedWorkspaceRoot) {
      this.selectedWorkspaceRoot = root;
      void this.context.workspaceState.update('baldr.console.workspaceRoot', root);
    }
    return root;
  }

  private async rememberSelectedItem(itemId: string | undefined): Promise<void> {
    this.selectedItemId = itemId;
    const root = this.currentWorkspaceRoot();
    if (root) {
      const key = this.workspaceKey(root);
      if (itemId) this.selectedByWorkspace[key] = itemId;
      else delete this.selectedByWorkspace[key];
      await this.context.workspaceState.update('baldr.console.selectedByWorkspace', this.selectedByWorkspace);
    }
    await this.context.workspaceState.update('baldr.console.selectedItemId', itemId);
  }

  private async chooseWorkspace(): Promise<void> {
    const root = await resolveWorkspaceRoot({ promptIfAmbiguous: true, forcePicker: true });
    if (!root) return;
    this.selectedWorkspaceRoot = root;
    this.workspacePinned = true;
    this.selectedItemId = this.selectedByWorkspace[this.workspaceKey(root)];
    await this.context.workspaceState.update('baldr.console.workspaceRoot', root);
    await this.context.workspaceState.update('baldr.console.workspacePinned', true);
    await this.refresh();
  }

  dispose(): void {
    this.stopPolling();
    for (const disposable of this.disposables) disposable.dispose();
  }

  async reveal(): Promise<void> {
    await vscode.commands.executeCommand('workbench.view.extension.baldr');
    await vscode.commands.executeCommand(`${VIEW_TYPE}.focus`);
    await this.refresh();
  }

  resolveWebviewView(view: vscode.WebviewView): void {
    this.view = view;
    view.webview.options = {
      enableScripts: true,
      localResourceRoots: [this.context.extensionUri],
    };
    view.webview.html = this.html(view.webview);
    this.disposables.push(
      view.webview.onDidReceiveMessage((message: ConsoleMessage) => void this.handleMessage(message)),
      view.onDidChangeVisibility(() => {
        if (view.visible) void this.refresh();
        else this.stopPolling();
      }),
      view.onDidDispose(() => {
        this.stopPolling();
        this.view = undefined;
      }),
    );
  }

  private shouldPoll(): boolean {
    if (this.operationCount > 0) return true;
    const workbench = record(this.lastStatus.workbench);
    const selected = record(workbench.selected);
    const selectedProgress = record(selected.progress);
    return ['running', 'cancelling'].includes(text(selected.status))
      || ['running', 'finalizing'].includes(text(selectedProgress.overall_state))
      || asItems(workbench.items).some((item) => ['running', 'cancelling'].includes(text(item.status)));
  }

  private pollingRevision(): string {
    const workbench = record(this.lastStatus.workbench);
    const selected = record(workbench.selected);
    const progress = record(selected.progress);
    const selectedRevision = text(progress.revision, '')
      || `${text(selected.id, '')}:${text(selected.updated_at, '')}:${text(selected.status, '')}`;
    const listRevision = asItems(workbench.items)
      .map((item) => {
        const summary = record(item.progress_summary);
        return `${text(item.id, '')}:${text(item.status, '')}:${text(item.updated_at, '')}:${text(summary.last_event_at, '')}:${text(summary.activity, '')}`;
      })
      .join('|');
    return `${selectedRevision}|${listRevision}`;
  }

  private stopPolling(): void {
    if (this.refreshTimer) clearTimeout(this.refreshTimer);
    this.refreshTimer = undefined;
  }

  private schedulePolling(delay = this.pollDelayMs): void {
    this.stopPolling();
    if (!this.view?.visible || !this.shouldPoll()) return;
    this.refreshTimer = setTimeout(() => void this.pollOnce(), delay);
  }

  private resetPolling(): void {
    this.pollDelayMs = POLL_FAST_MS;
    this.schedulePolling();
  }

  private async pollOnce(): Promise<void> {
    this.refreshTimer = undefined;
    if (!this.view?.visible || !this.shouldPoll()) return;
    const before = this.pollingRevision();
    await this.refresh(true);
    if (!this.view?.visible || !this.shouldPoll()) return;
    const changed = before !== this.pollingRevision();
    this.pollDelayMs = changed
      ? POLL_FAST_MS
      : this.pollDelayMs <= POLL_FAST_MS
        ? POLL_STABLE_MS
        : POLL_IDLE_MS;
    this.schedulePolling();
  }

  private async post(message: JsonRecord): Promise<void> {
    if (this.view) await this.view.webview.postMessage(message);
  }

  private statusForView(status: JsonRecord): JsonRecord {
    const workbench = record(status.workbench);
    const selected = record(workbench.selected);
    if (!selected.id) return status;
    return {
      ...status,
      workbench: {
        ...workbench,
        selected: {
          ...selected,
          presentation: buildWorkItemPresentation(selected),
        },
      },
    };
  }

  async refresh(silent = false): Promise<void> {
    const request = ++this.refreshRequested;
    if (!silent) this.interactiveRefreshRequested = true;
    if (!this.refreshPromise) this.refreshPromise = this.drainRefreshes();
    await this.refreshPromise;
    // A request can arrive after the drain loop observes an empty queue but
    // before its promise is cleared. Make that narrow race schedule a trailing
    // refresh instead of silently losing a selection or operation result.
    if (this.refreshCompleted < request) {
      if (!this.refreshPromise) this.refreshPromise = this.drainRefreshes();
      await this.refreshPromise;
    }
  }

  private async drainRefreshes(): Promise<void> {
    try {
      while (this.refreshCompleted < this.refreshRequested) {
        const throughRequest = this.refreshRequested;
        const silent = !this.interactiveRefreshRequested;
        this.interactiveRefreshRequested = false;
        await this.refreshOnce(silent);
        this.refreshCompleted = throughRequest;
      }
    } finally {
      this.refreshPromise = undefined;
    }
  }

  private async refreshOnce(silent: boolean): Promise<void> {
    const root = this.currentWorkspaceRoot();
    if (!root) {
      this.stopPolling();
      this.lastStatus = {};
      await this.post({
        type: 'state',
        state: {
          ok: false,
          emptyWorkspace: true,
          workspaceChoiceRequired: (vscode.workspace.workspaceFolders?.length ?? 0) > 1,
          workspaceFolders: (vscode.workspace.workspaceFolders ?? []).map((folder) => folder.name),
          trusted: vscode.workspace.isTrusted,
          busy: this.operationCount > 0,
          pending: this.pending,
        },
      });
      return;
    }
    if (!vscode.workspace.isTrusted) {
      this.stopPolling();
      this.lastStatus = {};
      await this.post({
        type: 'state',
        state: {
          ok: false,
          workspaceRoot: root,
          trusted: false,
          busy: this.operationCount > 0,
          pending: this.pending,
        },
      });
      return;
    }
    try {
      if (!silent) await this.post({ type: 'loading', value: true });
      const status = await this.runtime.consoleStatus(root, this.selectedItemId);
      this.lastStatus = status;
      const workbench = record(status.workbench);
      const items = asItems(workbench.items);
      if (!record(workbench.selected).id) {
        const nextItem = items.find((item) => text(item.status) !== 'archived') ?? items[0];
        await this.rememberSelectedItem(nextItem?.id ? String(nextItem.id) : undefined);
        if (this.selectedItemId) {
          const selectedStatus = await this.runtime.consoleStatus(root, this.selectedItemId);
          this.lastStatus = selectedStatus;
        }
      }
      await this.post({
        type: 'state',
        state: {
          ...this.statusForView(this.lastStatus),
          workspaceRoot: root,
          trusted: true,
          busy: this.operationCount > 0,
          pending: this.pending,
          activeContext: captureWorkspaceContext(root).activeLabel,
        },
      });
    } catch (error) {
      this.output.error(error instanceof Error ? error : new Error(String(error)));
      await this.post({ type: 'error', message: error instanceof Error ? error.message : String(error) });
    } finally {
      if (!silent) await this.post({ type: 'loading', value: false });
      if (!silent) this.resetPolling();
    }
  }

  private async handleMessage(message: ConsoleMessage): Promise<void> {
    if (
      this.operationCount > 0
      && ['submit', 'plusAction', 'chip', 'itemAction'].includes(String(message.type ?? ''))
    ) return;
    switch (message.type) {
      case 'ready':
      case 'refresh':
        await this.refresh();
        return;
      case 'select':
        await this.rememberSelectedItem(message.itemId || undefined);
        await this.refresh();
        return;
      case 'submit':
        await this.submit(String(message.value ?? ''));
        return;
      case 'plus':
        await this.openPlusMenu();
        return;
      case 'configure':
        await this.openPlusMenu();
        return;
      case 'plusAction':
        await this.handlePlusAction(String(message.action ?? ''));
        return;
      case 'removePending':
        await this.removePendingAttachment(Number(message.index));
        return;
      case 'chip':
        await this.openChip(String(message.value ?? ''));
        return;
      case 'itemAction':
        await this.itemAction(String(message.action ?? ''), message.itemId);
        return;
      case 'inspectDeliverable':
        await this.inspectDeliverable(message);
        return;
      case 'loadDeliverableIndex':
        await this.loadDeliverableIndex(message);
        return;
      case 'openChangedFile':
        await this.openChangedFile(message.path);
        return;
      case 'openLogs':
        this.output.show(true);
        return;
      case 'requestTrust':
        await vscode.commands.executeCommand('workbench.trust.manage');
        return;
      case 'chooseWorkspace':
        await this.chooseWorkspace();
        return;
      default:
        return;
    }
  }

  private async submit(raw: string): Promise<void> {
    const value = raw.trim();
    if (!value) return;
    if (value.startsWith('/')) {
      await this.executeSlash(value);
      return;
    }
    const root = this.requireWorkspace();
    const requestContext = this.requestContext(root, this.consumePendingContext());
    const selected = this.selectedItem();
    const continuing = Boolean(selected?.id && allowedActions(selected).includes('continue'));
    this.launchOperation(continuing ? 'Continuando la conversación…' : 'Creando y ejecutando la sesión…', async () => {
      const result = continuing
        ? await this.runtime.continueWorkItem(
          root,
          String(selected?.id),
          value,
          requestContext,
        )
        : await this.runtime.runFacade('run', {
          workspaceRoot: root,
          task: value,
          workItemAction: 'execute',
          extraContext: requestContext.extraContext,
          attachments: requestContext.attachments,
        }, undefined, true);
      const item = record(result.work_item);
      if (item.id) this.selectedItemId = String(item.id);
      return result;
    });
    await this.post({ type: 'clearInput' });
  }

  private consumePendingContext(): PendingContext {
    const value = this.pending;
    this.pending = { attachments: [], extraContext: '', selectionContexts: {} };
    return value;
  }

  private requestContext(root: string, pending: PendingContext): PendingContext {
    const automatic = captureWorkspaceContext(root);
    const attachments = [...pending.attachments];
    const seen = new Set(attachments.map((item) => `${text(item.kind)}:${text(item.path)}:${JSON.stringify(item.range ?? null)}`));
    for (const attachment of automatic.attachments) {
      const key = `${text(attachment.kind)}:${text(attachment.path)}:${JSON.stringify(attachment.range ?? null)}`;
      if (!seen.has(key)) {
        seen.add(key);
        attachments.push(attachment);
      }
    }
    return {
      attachments: attachments.slice(0, 50),
      extraContext: [automatic.extraContext, pending.extraContext].filter(Boolean).join('\n\n'),
      selectionContexts: pending.selectionContexts,
    };
  }

  private async removePendingAttachment(index: number): Promise<void> {
    if (!Number.isInteger(index) || index < 0 || index >= this.pending.attachments.length) return;
    const [removed] = this.pending.attachments.splice(index, 1);
    const contextKey = text(record(removed).contextKey, '');
    if (contextKey) delete this.pending.selectionContexts[contextKey];
    this.pending.extraContext = Object.values(this.pending.selectionContexts).join('\n\n');
    await this.post({ type: 'pending', pending: this.pending });
  }

  private async executeSlash(raw: string): Promise<void> {
    const [commandToken, ...rest] = raw.slice(1).trim().split(/\s+/);
    const command = commandToken.toLowerCase();
    const argument = rest.join(' ').trim();
    switch (command) {
      case 'setup':
        await this.openPlusMenu();
        return;
      case 'new':
        if (!argument) {
          await this.post({ type: 'prefill', value: '' });
          return;
        }
        await this.createDraft(argument);
        return;
      case 'run':
        if (argument) {
          await this.submit(argument);
        } else {
          await this.itemAction('start', this.selectedItemId);
        }
        return;
      case 'status':
        await this.refresh();
        return;
      case 'git': {
        const mode = normalizeMode(argument);
        if (mode) await this.setSafetyMode(mode);
        else await this.chooseSafetyMode();
        return;
      }
      case 'profile': {
        const preset = normalizePreset(argument);
        if (preset) await this.setPreset(preset);
        else await this.choosePreset();
        return;
      }
      case 'context': {
        const mode = normalizeContext(argument);
        if (mode) await this.setContextMode(mode);
        else await this.chooseContextMode();
        return;
      }
      case 'roles':
        await this.chooseRoleProfiles();
        return;
      case 'cancel':
        await this.itemAction('cancel', this.selectedItemId);
        return;
      case 'resume':
        await this.chooseReconciliation(this.selectedItemId);
        return;
      case 'archive':
        await this.itemAction('archive', this.selectedItemId);
        return;
      case 'restore':
        await this.itemAction('restore', this.selectedItemId);
        return;
      case 'delete':
        await this.itemAction('delete', this.selectedItemId);
        return;
      case 'help':
        await this.post({ type: 'showHelp' });
        return;
      default:
        void vscode.window.showWarningMessage(`Baldr no reconoce el comando /${command}. Usá /help para ver las opciones.`);
    }
  }

  private requireWorkspace(): string {
    const root = this.currentWorkspaceRoot();
    if (!root) throw new Error('Elegí una carpeta de trabajo antes de crear una sesión.');
    if (!vscode.workspace.isTrusted) throw new Error('Autorizá esta carpeta antes de usar Baldr.');
    return root;
  }

  private async openChangedFile(rawPath: unknown): Promise<void> {
    const root = this.currentWorkspaceRoot();
    const requestedPath = typeof rawPath === 'string' ? rawPath.trim() : '';
    if (!root || !requestedPath || path.isAbsolute(requestedPath)) {
      void vscode.window.showWarningMessage('No pudimos identificar ese archivo dentro del proyecto.');
      return;
    }

    const workspaceRoot = path.resolve(root);
    const target = path.resolve(workspaceRoot, requestedPath);
    if (!isPathInsideRoot(workspaceRoot, target)) {
      void vscode.window.showWarningMessage('Ese archivo no pertenece al proyecto abierto.');
      return;
    }

    try {
      const [canonicalRoot, canonicalTarget] = await Promise.all([
        fs.promises.realpath(workspaceRoot),
        fs.promises.realpath(target),
      ]);
      if (!isPathInsideRoot(canonicalRoot, canonicalTarget)) {
        void vscode.window.showWarningMessage('Ese archivo no pertenece al proyecto abierto.');
        return;
      }
      const stat = await fs.promises.stat(canonicalTarget);
      if (!stat.isFile()) {
        void vscode.window.showWarningMessage('Ese cambio no corresponde a un archivo que se pueda abrir.');
        return;
      }
      const document = await vscode.workspace.openTextDocument(vscode.Uri.file(canonicalTarget));
      await vscode.window.showTextDocument(document, { preview: true, preserveFocus: false });
    } catch (error) {
      const code = typeof error === 'object' && error !== null && 'code' in error
        ? String(error.code)
        : '';
      if (code === 'ENOENT') {
        void vscode.window.showWarningMessage('El archivo ya no existe; probablemente fue eliminado en este cambio.');
        return;
      }
      this.output.warn('Baldr could not open a changed workspace file.');
      void vscode.window.showWarningMessage('No pudimos abrir ese archivo.');
    }
  }

  private launchOperation(
    label: string,
    operation: () => Promise<JsonRecord>,
    afterSuccess?: () => Promise<void>,
    allowAttentionPause = true,
  ): void {
    this.operationCount += 1;
    this.resetPolling();
    void this.post({ type: 'operation', busy: true, label });
    void operation()
      .then(async (result) => {
        const item = record(result.work_item);
        if (item.id) {
          await this.rememberSelectedItem(String(item.id));
        }
        const resultStatus = text(result.status).toLowerCase();
        const itemStatus = text(item.status).toLowerCase();
        const expectedPause = resultStatus === 'cancelled'
          || (allowAttentionPause && (
            resultStatus === 'awaiting_reconciliation'
            || itemStatus === 'needs_attention'
          ));
        if (result.ok === false && !expectedPause && !await this.handlePolicyBlock(result)) {
          const reason = text(result.reason, text(record(result.error).message, 'Baldr no pudo completar la operación.'));
          void vscode.window.showErrorMessage(reason);
        }
        if (result.ok !== false) await afterSuccess?.();
      })
      .catch((error) => {
        this.output.error(error instanceof Error ? error : new Error(String(error)));
        void vscode.window.showErrorMessage(error instanceof Error ? error.message : String(error));
      })
      .finally(async () => {
        this.operationCount = Math.max(0, this.operationCount - 1);
        await this.post({ type: 'operation', busy: this.operationCount > 0, label: '' });
        await this.refresh();
      });
  }

  private async handlePolicyBlock(result: JsonRecord): Promise<boolean> {
    const code = text(record(result.error).code);
    if (!['workspace_git_required', 'workspace_non_git_confirmation_required'].includes(code)) return false;

    const item = record(result.work_item);
    const choice = await vscode.window.showWarningMessage(
      'La sesión quedó guardada, pero esta opción necesita un repositorio Git. Podés pedir autorización antes de cambiar esta carpeta, abrir otra carpeta o trabajar sin protección.',
      { modal: true },
      'Pedir autorización',
      'Sin protección',
      'Abrir otra carpeta…',
    );
    if (choice === 'Abrir otra carpeta…') {
      await vscode.commands.executeCommand('workbench.action.files.openFolder');
      return true;
    }
    if (choice === 'Pedir autorización') {
      const configured = await this.setSafetyMode('automatic');
      if (configured && item.id) {
        this.launchOperation('Iniciando la sesión con autorización previa…', () => this.runtime.startWorkItem(
          this.requireWorkspace(),
          String(item.id),
        ));
      }
      return true;
    }
    if (choice === 'Sin protección') {
      const configured = await this.setSafetyMode('non-git');
      if (configured && item.id) {
        this.launchOperation('Iniciando la sesión sin protección…', () => this.runtime.startWorkItem(
          this.requireWorkspace(),
          String(item.id),
        ));
      }
      return true;
    }
    return true;
  }

  private selectedItem(): WorkItem | undefined {
    const workbench = record(this.lastStatus.workbench);
    const selected = record(workbench.selected) as WorkItem;
    if (selected.id) return selected;
    return asItems(workbench.items).find((item) => String(item.id) === this.selectedItemId);
  }

  private currentRoleProfiles(): Record<string, string[]> {
    const preferences = record(record(this.lastStatus.workbench).preferences);
    const selectedByRole = record(preferences.role_profiles);
    const roleProfiles: Record<string, string[]> = {};
    for (const role of BALDR_ROLES) {
      const selected = selectedByRole[role];
      if (Array.isArray(selected) && selected.length) {
        roleProfiles[role] = selected.map(String);
      }
    }
    return roleProfiles;
  }

  private async itemAction(action: string, itemId = this.selectedItemId): Promise<void> {
    if (!itemId) {
      void vscode.window.showInformationMessage('Elegí una sesión primero.');
      return;
    }
    const root = this.requireWorkspace();
    if (action === 'start') {
      this.launchOperation('Iniciando la sesión…', () => this.runtime.startWorkItem(
        root,
        itemId,
        { roleProfiles: this.currentRoleProfiles() },
      ));
      return;
    }
    if (action === 'cancel') {
      const confirm = await vscode.window.showWarningMessage(
        '¿Querés cancelar esta sesión y detener el trabajo en curso?',
        { modal: true },
        'Cancelar sesión',
      );
      if (confirm !== 'Cancelar sesión') return;
      this.launchOperation('Cancelando la sesión…', () => this.runtime.cancelWorkItem(root, itemId));
      return;
    }
    if (action === 'archive') {
      this.launchOperation(
        'Archivando la sesión…',
        () => this.runtime.archiveWorkItem(root, itemId),
        () => this.post({ type: 'historyFilter', filter: 'archived' }),
      );
      return;
    }
    if (action === 'restore') {
      this.launchOperation(
        'Restaurando la sesión…',
        () => this.runtime.restoreWorkItem(root, itemId),
        () => this.post({ type: 'historyFilter', filter: 'active' }),
      );
      return;
    }
    if (action === 'delete') {
      const item = this.selectedItem();
      const title = text(item?.title, 'esta sesión');
      const confirmed = await vscode.window.showWarningMessage(
        `¿Eliminar permanentemente “${title}”? Se borrará su historial y sus entregas, pero no se tocarán archivos de la carpeta.`,
        { modal: true },
        'Eliminar permanentemente',
      );
      if (confirmed !== 'Eliminar permanentemente') return;
      this.launchOperation(
        'Eliminando la sesión…',
        () => this.runtime.deleteWorkItem(root, itemId),
        () => this.post({ type: 'historyFilter', filter: 'active' }),
      );
      return;
    }
    if (action === 'reconcile') {
      await this.chooseReconciliation(itemId);
      return;
    }
    if (RECONCILIATION_ACTIONS.has(action)) {
      const label = action === 'authorize_changes'
        ? 'Autorizando los cambios y retomando la sesión…'
        : action === 'decline_changes'
          ? 'Cerrando la sesión sin modificar archivos…'
          : 'Recuperando la sesión…';
      this.launchOperation(
        label,
        () => this.runtime.reconcileWorkItem(root, itemId, action),
        undefined,
        false,
      );
      return;
    }
  }

  private async inspectDeliverable(message: ConsoleMessage): Promise<void> {
    const itemId = String(message.itemId ?? '');
    const stage = String(message.stage ?? '');
    const round = Number(message.round);
    const runOrdinal = message.runOrdinal === undefined ? undefined : Number(message.runOrdinal);
    const cursor = String(message.cursor ?? '');
    const descriptorDigest = String(message.descriptorDigest ?? '').slice(0, 128);
    const requestId = Number(message.requestId);
    const responseContext = { itemId, descriptorDigest, requestId };
    if (
      !itemId
      || itemId !== this.selectedItemId
      || !['planning', 'execution', 'review'].includes(stage)
      || !Number.isInteger(round)
      || round < 0
      || (runOrdinal !== undefined && (!Number.isInteger(runOrdinal) || runOrdinal < 0))
      || !Number.isSafeInteger(requestId)
      || requestId < 1
    ) {
      await this.post({
        type: 'deliverableError',
        ...responseContext,
        message: 'No pudimos identificar esa entrega.',
      });
      return;
    }
    try {
      const result = await this.runtime.inspectWorkItemPhase(
        this.requireWorkspace(),
        itemId,
        stage as 'planning' | 'execution' | 'review',
        round,
        { runOrdinal, cursor: cursor || undefined, pageSize: 30 },
      );
      const descriptor = record(result.deliverable);
      const page = record(result.page);
      if (result.ok !== true || !descriptor.stage || !Array.isArray(page.entries)) {
        throw new Error('phase deliverable unavailable');
      }
      const returnedDigest = String(descriptor.digest ?? '');
      if (descriptorDigest && returnedDigest && descriptorDigest !== returnedDigest) {
        throw new Error('phase deliverable changed');
      }
      await this.post({
        type: 'deliverableResult',
        ...responseContext,
        deliverable: {
          ...descriptor,
          contract: result.contract,
          version: result.version,
          sections: Array.isArray(result.sections) ? result.sections : [],
          page,
          redaction: record(result.redaction),
        },
        append: Boolean(cursor),
      });
    } catch {
      this.output.warn('Baldr could not load the requested public phase deliverable.');
      await this.post({
        type: 'deliverableError',
        ...responseContext,
        message: 'No pudimos abrir la entrega. Probá nuevamente.',
      });
    }
  }

  private async loadDeliverableIndex(message: ConsoleMessage): Promise<void> {
    const itemId = String(message.itemId ?? '');
    const cursor = String(message.cursor ?? '');
    const requestId = Number(message.requestId);
    const responseContext = { itemId, cursor, requestId };
    if (
      !itemId
      || itemId !== this.selectedItemId
      || !cursor
      || !Number.isSafeInteger(requestId)
      || requestId < 1
    ) {
      await this.post({
        type: 'deliverableIndexError',
        ...responseContext,
        message: 'No pudimos identificar las entregas anteriores.',
      });
      return;
    }
    try {
      const result = await this.runtime.listWorkItemDeliverables(
        this.requireWorkspace(),
        itemId,
        { cursor, pageSize: 50 },
      );
      const page = record(result.page);
      if (
        result.ok !== true
        || result.contract !== 'baldr-phase-deliverable-index-page'
        || Number(result.version) !== 1
        || !Array.isArray(result.items)
        || typeof page.has_more !== 'boolean'
      ) {
        throw new Error('phase deliverable index unavailable');
      }
      await this.post({
        type: 'deliverableIndexResult',
        ...responseContext,
        items: buildPhaseDeliverablePresentations(result.items),
        page,
      });
    } catch {
      this.output.warn('Baldr could not load the requested public phase deliverable index.');
      await this.post({
        type: 'deliverableIndexError',
        ...responseContext,
        message: 'No pudimos cargar las entregas anteriores. Probá nuevamente.',
      });
    }
  }

  private async createDraft(task?: string): Promise<void> {
    const root = this.requireWorkspace();
    const value = task ?? await vscode.window.showInputBox({
      title: 'Guardar una sesión para después',
      prompt: 'Escribí qué necesitás',
      ignoreFocusOut: true,
    });
    if (!value?.trim()) return;
    const pending = this.requestContext(root, this.consumePendingContext());
    const result = await this.withProgress('Guardando la sesión…', () => this.runtime.createWorkItem(
      root,
      value.trim(),
      { extraContext: pending.extraContext, attachments: pending.attachments },
    ));
    const item = record(result.work_item);
    if (item.id) await this.rememberSelectedItem(String(item.id));
    await this.post({ type: 'clearInput' });
    await this.refresh();
  }

  private async openPlusMenu(): Promise<void> {
    const choice = await vscode.window.showQuickPick([
      { id: 'draft', label: '$(add) Guardar para después', description: 'Crear una sesión sin empezarla' },
      { id: 'file', label: '$(file) Agregar el archivo abierto', description: 'Usarlo como referencia para el pedido' },
      { id: 'selection', label: '$(selection) Agregar el texto seleccionado', description: 'Usar solamente la parte marcada' },
      { id: 'path', label: '$(folder-opened) Agregar archivos o carpetas', description: 'Sumar material útil para el pedido' },
      { id: 'workspace', label: '$(root-folder) Carpeta de trabajo', description: 'Elegir el proyecto activo en un workspace con varias carpetas' },
      { id: 'git', label: '$(shield) Protección de cambios', description: 'Elegir cómo guardar y recuperar el trabajo' },
      { id: 'preset', label: '$(dashboard) Nivel de detalle', description: 'Rápido, estándar, detallado o a medida' },
      { id: 'roles', label: '$(organization) Equipo de Baldr', description: 'Elegir cómo se reparte el trabajo' },
      { id: 'agents', label: '$(remote-explorer) Agentes externos', description: 'Consultar y asignar agentes registrados de forma segura' },
      { id: 'profile-create', label: '$(tools) Crear una configuración avanzada', description: 'Elegir proveedor y modelo paso a paso' },
      { id: 'context', label: '$(sparkle) Ayuda adicional', description: 'Buscar información útil cuando haga falta' },
      { id: 'qualification', label: '$(verified-filled) Calificar VS Code + Codex', description: 'Ejecutar los gates reales y abrir la evidencia pendiente' },
      { id: 'status', label: '$(refresh) Actualizar', description: 'Volver a cargar las sesiones y su estado' },
      { id: 'logs', label: '$(output) Ver detalles técnicos', description: 'Abrir el registro de Baldr' },
    ], {
      title: 'Baldr',
      placeHolder: 'Agregar información o cambiar una opción',
      ignoreFocusOut: true,
    });
    await this.handlePlusAction(choice?.id ?? '');
  }

  private async handlePlusAction(action: string): Promise<void> {
    switch (action) {
      case 'draft': await this.post({ type: 'prefill', value: '/new ' }); break;
      case 'file': await this.attachCurrentFile(); break;
      case 'selection': await this.attachSelection(); break;
      case 'path': await this.attachFileOrFolder(); break;
      case 'workspace': await this.chooseWorkspace(); break;
      case 'git': await this.chooseSafetyMode(); break;
      case 'preset': await this.choosePreset(); break;
      case 'roles': await this.chooseRoleProfiles(); break;
      case 'agents': await this.chooseExternalAgent(); break;
      case 'profile-create': await this.createExecutionProfile(); break;
      case 'context': await this.chooseContextMode(); break;
      case 'qualification': await this.runQualification(); break;
      case 'status': await this.refresh(); break;
      case 'logs': this.output.show(true); break;
      default: break;
    }
  }

  private async runQualification(): Promise<void> {
    const root = this.requireWorkspace();
    let result: JsonRecord;
    try {
      result = await vscode.window.withProgress({
        location: vscode.ProgressLocation.Notification,
        title: 'Calificando VS Code + Codex…',
        cancellable: true,
      }, (_progress, token) => this.runtime.runQualification(
        root,
        { includeProviderSmoke: true },
        token,
      ));
    } catch (error) {
      if (error instanceof vscode.CancellationError) return;
      this.output.error(error instanceof Error ? error : new Error(String(error)));
      void vscode.window.showErrorMessage('No pudimos completar la qualification. Abrí los detalles técnicos para revisar el motivo.');
      return;
    }

    const report = await vscode.workspace.openTextDocument({
      language: 'markdown',
      content: renderQualification(result),
    });
    await vscode.window.showTextDocument(report, { preview: true, preserveFocus: false });

    if (result.status === 'qualified') {
      void vscode.window.showInformationMessage('VS Code + Codex quedó qualified para este entorno.');
      return;
    }
    const choice = await vscode.window.showWarningMessage(
      'La qualification sigue provisional. Completá únicamente evidencia observada en este entorno y volvé a ejecutar esta acción.',
      'Abrir assertions',
      'Abrir canarios',
    );
    const target = choice === 'Abrir assertions'
      ? text(result.client_assertions_path)
      : choice === 'Abrir canarios'
        ? text(result.canary_results_path)
        : '';
    if (!target || !fs.existsSync(target)) return;
    const document = await vscode.workspace.openTextDocument(vscode.Uri.file(target));
    await vscode.window.showTextDocument(document, { preview: false, preserveFocus: false });
  }

  private async openChip(chip: string): Promise<void> {
    if (chip === 'git') await this.chooseSafetyMode();
    else if (chip === 'preset') await this.choosePreset();
    else if (chip === 'context') await this.chooseContextMode();
    else if (chip === 'roles') await this.chooseRoleProfiles();
  }

  private async chooseSafetyMode(): Promise<void> {
    const preference = record(record(this.lastStatus.workbench).preferences);
    const current = text(preference.safety_mode, 'current');
    const selected = await vscode.window.showQuickPick([
      { id: 'current', label: '$(shield) Trabajar directamente', description: 'Recomendada: permite cambios directos sin una pausa de autorización por tarea' },
      { id: 'automatic', label: 'Pedir autorización', description: 'Planifica en solo lectura y pregunta antes de modificar esta carpeta' },
      { id: 'non-git', label: 'Sin protección', description: 'Trabaja directamente, sin exigir Git y sin recuperación automática' },
    ], { title: 'Protección de cambios', placeHolder: `Actual: ${safetyModeLabel(current)}`, ignoreFocusOut: true });
    if (selected) await this.setSafetyMode(selected.id as SafetyMode);
  }

  private async setSafetyMode(mode: SafetyMode): Promise<boolean> {
    const root = this.requireWorkspace();
    let allowNonGit = false;
    if (mode === 'non-git') {
      const confirm = await vscode.window.showWarningMessage(
        'Baldr modificará los archivos de esta carpeta directamente, sin un respaldo automático para recuperarlos si algo se interrumpe.',
        { modal: true },
        'Continuar sin protección',
      );
      if (confirm !== 'Continuar sin protección') return false;
      allowNonGit = true;
    }
    await this.withProgress('Actualizando la protección de cambios…', () => this.runtime.setWorkspacePreferences(root, {
      safetyMode: mode,
      allowNonGit,
    }));
    await this.refresh();
    return true;
  }

  private async choosePreset(): Promise<void> {
    const preference = record(record(this.lastStatus.workbench).preferences);
    const current = text(preference.preset, 'balanced');
    const selected = await vscode.window.showQuickPick([
      { id: 'fast', label: 'Rápido', description: 'Prioriza velocidad y una respuesta breve' },
      { id: 'balanced', label: 'Estándar', description: 'Buen equilibrio entre velocidad y profundidad' },
      { id: 'deep', label: 'Detallado', description: 'Dedica más tiempo al análisis y la revisión' },
      { id: 'custom', label: 'A medida', description: 'Usa la configuración elegida para cada etapa' },
    ], { title: 'Nivel de detalle', placeHolder: `Actual: ${presetModeLabel(current)}`, ignoreFocusOut: true });
    if (selected) await this.setPreset(selected.id as 'fast' | 'balanced' | 'deep' | 'custom');
  }

  private async setPreset(preset: 'fast' | 'balanced' | 'deep' | 'custom'): Promise<void> {
    const root = this.requireWorkspace();
    await this.withProgress('Actualizando el nivel de detalle…', () => this.runtime.setWorkspacePreferences(root, { preset }));
    await this.refresh();
  }

  private async chooseContextMode(): Promise<void> {
    const preference = record(record(this.lastStatus.workbench).preferences);
    const current = text(preference.context_mode, 'auto');
    const selected = await vscode.window.showQuickPick([
      { id: 'auto', label: 'Automática', description: 'Baldr busca información adicional cuando puede ayudar' },
      { id: 'on', label: 'Siempre activa', description: 'Busca información adicional para cada pedido' },
      { id: 'off', label: 'Desactivada', description: 'No busca información adicional' },
    ], { title: 'Ayuda adicional', placeHolder: `Actual: ${contextModeLabel(current)}`, ignoreFocusOut: true });
    if (selected) await this.setContextMode(selected.id as 'auto' | 'on' | 'off');
  }

  private async setContextMode(mode: 'auto' | 'on' | 'off'): Promise<void> {
    const root = this.requireWorkspace();
    if (mode !== 'off') await this.ensureContext7Credential();
    await this.withProgress('Actualizando la ayuda adicional…', () => this.runtime.setWorkspacePreferences(root, { contextMode: mode }));
    await this.refresh();
  }

  private async ensureContext7Credential(): Promise<void> {
    if (await this.context.secrets.get(CONTEXT7_SECRET_KEY)) return;
    const choice = await vscode.window.showQuickPick([
      { id: 'key', label: 'Guardar una clave de Context7', description: 'VS Code la protege y no la comparte en el chat' },
      { id: 'env', label: 'Usar la clave ya configurada', description: 'Leer CONTEXT7_API_KEY del entorno donde se ejecuta Baldr' },
      { id: 'skip', label: 'Continuar sin una clave', description: 'Baldr seguirá funcionando, pero puede tener menos información' },
    ], { title: 'Clave opcional para la ayuda adicional', ignoreFocusOut: true });
    if (choice?.id === 'key') {
      const key = await vscode.window.showInputBox({
        title: 'Clave de Context7',
        password: true,
        prompt: 'VS Code la guarda de forma segura. No se incluye en la carpeta ni en el chat.',
        ignoreFocusOut: true,
      });
      if (key?.trim()) {
        await this.context.secrets.store(CONTEXT7_SECRET_KEY, key.trim());
        await this.runtime.configureContext7FromSecret();
      }
    } else if (choice?.id === 'env') {
      await this.runtime.configureContext7FromEnvironment();
    }
  }

  private async chooseRoleProfiles(): Promise<void> {
    const choice = await vscode.window.showQuickPick([
      {
        id: 'normal',
        label: '$(organization) Codex o Kiro normal (recomendado)',
        description: 'Conservar el proveedor habitual configurado para cada etapa',
      },
      {
        id: 'automatic',
        label: '$(sparkle) Automático',
        description: 'Baldr elige agentes compatibles y usa un respaldo si hace falta',
      },
      {
        id: 'per-stage',
        label: '$(remote-explorer) Elegir por etapa',
        description: 'Fijar un agente registrado sólo donde lo necesites',
      },
    ], {
      title: 'Equipo de Baldr',
      placeHolder: 'Elegí qué querés hacer',
      ignoreFocusOut: true,
    });
    if (choice?.id === 'automatic') {
      const root = this.requireWorkspace();
      const updated = await this.withProgress(
        'Activando el equipo automático…',
        () => this.runtime.setWorkspacePreferences(root, {
          teamMode: 'automatic',
          agentOverrides: {},
        }),
      );
      if (updated.ok === false) throw new Error(this.resultReason(updated));
      await this.refresh();
      void vscode.window.showInformationMessage('Baldr elegirá automáticamente el mejor agente compatible para cada etapa.');
    } else if (choice?.id === 'per-stage') await this.chooseExternalAgent();
    else if (choice?.id === 'normal') await this.restoreStandardProvider();
  }

  private async chooseExternalAgent(): Promise<void> {
    const role = await vscode.window.showQuickPick([
      { id: 'architect' as BaldrRole, label: 'Planificación', description: 'Necesita acceso de lectura' },
      { id: 'reviewer' as BaldrRole, label: 'Revisión', description: 'Necesita acceso de lectura' },
      { id: 'implementer' as BaldrRole, label: 'Ejecución', description: 'Necesita acceso de escritura explícito' },
    ], {
      title: 'Etapa para el agente externo',
      placeHolder: 'Elegí dónde participará el agente',
      ignoreFocusOut: true,
    });
    if (!role) return;

    let catalog: JsonRecord;
    try {
      catalog = await vscode.window.withProgress({
        location: vscode.ProgressLocation.Notification,
        title: 'Consultando agentes registrados…',
        cancellable: true,
      }, (_progress, token) => this.runtime.agentCatalog(this.requireWorkspace(), token));
    } catch (error) {
      if (error instanceof vscode.CancellationError) return;
      this.output.appendLine(`[agentes] ${error instanceof Error ? error.message : String(error)}`);
      void vscode.window.showErrorMessage('No pudimos consultar los agentes registrados. Tu equipo no cambió.');
      return;
    }
    if (catalog.ok !== true) {
      const reason = this.resultReason(catalog);
      this.output.appendLine(`[agentes] ${reason}`);
      void vscode.window.showErrorMessage(`No pudimos consultar los agentes registrados. ${reason}`);
      return;
    }
    const needsWrite = role.id === 'implementer';
    const agents = (Array.isArray(catalog.agents) ? catalog.agents : [])
      .map(record)
      .filter((agent) => {
        const capabilities = Array.isArray(agent.capabilities) ? agent.capabilities.map(String) : [];
        if (agent.enabled === false) return false;
        if (!capabilities.includes('workspace.read')) return false;
        if (needsWrite) {
          return capabilities.includes('workspace.write') && text(agent.effect_mode) === 'workspace-write';
        }
        return true;
      });
    if (!agents.length) {
      void vscode.window.showInformationMessage(
        needsWrite
          ? 'No hay agentes registrados con permiso explícito de escritura.'
          : 'No hay agentes de lectura registrados.',
      );
      return;
    }
    const selected = await vscode.window.showQuickPick(agents.map((agent) => {
      const ready = agent.ready !== false && agent.enabled !== false;
      const lastSuccess = record(agent.last_success);
      const namespace = text(agent.namespace);
      const agentName = text(agent.name);
      const displayName = namespace === 'codex'
        ? `Codex${agentName ? ` · ${agentName}` : ''}`
        : namespace === 'kiro'
          ? `Kiro${agentName ? ` · ${agentName}` : ''}`
          : text(agent.name, text(agent.ref));
      const canWrite = text(agent.effect_mode) === 'workspace-write';
      const accessLabel = canWrite ? 'Lectura y escritura' : 'Solo lectura';
      const stateLabel = agent.enabled === false
        ? 'Deshabilitado'
        : ready ? 'Listo' : 'No disponible';
      return {
      agent,
      label: `${ready ? '$(check)' : '$(warning)'} ${displayName} — ${accessLabel}`,
      description: [
        `v${text(agent.version)}`,
        stateLabel,
      ].filter(Boolean).join(' · '),
      detail: [
        `AgentRef: ${text(agent.ref)}`,
        `Digest: ${text(agent.digest)}`,
        lastSuccess.run_id ? `Último éxito: ${text(lastSuccess.updated_at)} · ${text(lastSuccess.run_id)}` : '',
        ready ? '' : `Motivo: ${text(agent.reason, 'agente no disponible')}`,
      ].filter(Boolean).join(' — '),
    };
    }), {
      title: `Agente para ${role.label.toLowerCase()}`,
      placeHolder: 'Se muestran estado, versión y digest de cada agente compatible',
      ignoreFocusOut: true,
    });
    if (!selected) return;
    if (selected.agent.ready === false || selected.agent.enabled === false) {
      void vscode.window.showWarningMessage(
        `No se puede asignar ${text(selected.agent.ref)}: ${text(selected.agent.reason, 'no está disponible')}.`,
      );
      return;
    }

    const reference = text(selected.agent.ref);
    const root = this.requireWorkspace();
    const preferences = record(record(this.lastStatus.workbench).preferences);
    const agentOverrides = Object.fromEntries(
      Object.entries(record(preferences.agent_overrides))
        .filter(([key, value]) => BALDR_ROLES.includes(key as BaldrRole) && text(value))
        .map(([key, value]) => [key, text(value)]),
    );
    agentOverrides[role.id] = reference;
    const updated = await this.withProgress('Guardando el agente para esta etapa…', () => (
      this.runtime.setWorkspacePreferences(root, {
        teamMode: 'automatic',
        agentOverrides,
      })
    ));
    if (updated.ok === false) throw new Error(this.resultReason(updated));
    await this.refresh();
    void vscode.window.showInformationMessage(`${reference} quedó asignado a ${role.label.toLowerCase()}.`);
  }

  private async manageExternalAgents(): Promise<void> {
    const root = this.requireWorkspace();
    let catalog: JsonRecord;
    try {
      catalog = await this.withProgress('Consultando el catálogo de agentes…', () => this.runtime.agentCatalog(root));
    } catch (error) {
      void vscode.window.showErrorMessage(error instanceof Error ? error.message : String(error));
      return;
    }
    const localAgents = (Array.isArray(catalog.agents) ? catalog.agents : [])
      .map(record)
      .filter((agent) => text(agent.source) === 'local');
    const selected = await vscode.window.showQuickPick([
      {
        id: 'publish',
        label: '$(add) Registrar una versión de agente',
        description: 'Publica un AgentRef inmutable en el catálogo local',
        agent: {} as JsonRecord,
      },
      ...localAgents.map((agent) => ({
        id: 'agent',
        agent,
        label: `${agent.enabled === false ? '$(circle-slash)' : agent.ready === false ? '$(warning)' : '$(check)'} ${text(agent.ref)}`,
        description: [`v${text(agent.version)}`, text(agent.state), text(agent.owner)].filter(Boolean).join(' · '),
        detail: `${text(agent.digest)}${agent.reason ? ` — ${text(agent.reason)}` : ''}`,
      })),
    ], {
      title: 'Administrar agentes externos',
      placeHolder: 'Elegí una versión exacta o registrá una nueva',
      ignoreFocusOut: true,
    });
    if (!selected) return;
    if (selected.id === 'publish') {
      await this.registerExternalAgent();
      return;
    }
    const agent = selected.agent;
    const enabled = agent.enabled !== false;
    const action = await vscode.window.showQuickPick([
      { id: 'inspect', label: '$(info) Inspeccionar', description: 'Ver contrato, destino y diagnóstico seguro' },
      { id: enabled ? 'disable' : 'enable', label: enabled ? '$(circle-slash) Deshabilitar' : '$(play) Habilitar', description: enabled ? 'Impide nuevas resoluciones' : 'Permite volver a resolver esta versión' },
      { id: 'new-version', label: '$(versions) Publicar nueva versión', description: 'Conserva esta versión y registra otro AgentRef' },
      { id: 'remove', label: '$(trash) Eliminar del catálogo local', description: 'Sólo si está deshabilitado y ninguna sesión durable lo usa' },
    ], { title: text(agent.ref), ignoreFocusOut: true });
    if (!action) return;
    if (action.id === 'new-version') {
      await this.registerExternalAgent(agent);
      return;
    }
    if (action.id === 'remove') {
      const confirmation = await vscode.window.showWarningMessage(
        `¿Eliminar ${text(agent.ref)} del catálogo local?`,
        { modal: true },
        'Eliminar',
      );
      if (confirmation !== 'Eliminar') return;
    }
    const command = action.id === 'inspect' ? ['inspect', text(agent.ref)] : [action.id, text(agent.ref)];
    const result = await this.withProgress('Actualizando el catálogo…', () => this.runtime.manageLocalAgent(root, command));
    if (result.ok !== true) {
      void vscode.window.showErrorMessage(this.resultReason(result));
      return;
    }
    if (action.id === 'inspect') {
      const details = record(result.agent);
      this.output.appendLine(`[agente] ${JSON.stringify(details, null, 2)}`);
      void vscode.window.showInformationMessage(
        `${text(details.ref)} · ${details.enabled === false ? 'deshabilitado' : 'habilitado'} · ${text(details.digest)}`,
      );
    } else {
      await this.refresh();
      void vscode.window.showInformationMessage(`${text(agent.ref)}: operación ${action.id} completada.`);
    }
  }

  private async registerExternalAgent(existing: JsonRecord = {}): Promise<void> {
    const root = this.requireWorkspace();
    const reference = await vscode.window.showInputBox({
      title: 'AgentRef exacto',
      value: text(existing.ref),
      placeHolder: 'local://equipo/revisor@1.0.0',
      validateInput: (value) => /^[a-z][a-z0-9+.-]*:\/\/[A-Za-z0-9._-]+\/[A-Za-z0-9._-]+@[A-Za-z0-9._+-]+$/.test(value)
        ? undefined
        : 'Usá registry://namespace/name@version.',
      ignoreFocusOut: true,
    });
    if (!reference) return;
    const owner = await vscode.window.showInputBox({
      title: 'Propietario del agente',
      value: text(existing.owner),
      placeHolder: 'equipo-producto',
      ignoreFocusOut: true,
    });
    if (!owner) return;
    const kind = await vscode.window.showQuickPick([
      { id: 'codex', label: 'Codex', description: 'Agente externo resuelto por el proveedor Codex' },
      { id: 'kiro-cli', label: 'Kiro', description: 'Agente externo definido en Kiro CLI' },
      { id: 'http-json', label: 'HTTP JSON', description: 'Servicio externo de sólo lectura' },
    ], { title: 'Transporte del agente', ignoreFocusOut: true });
    if (!kind) return;
    const permission = await vscode.window.showQuickPick([
      { id: 'read', label: 'Sólo lectura', description: 'Planificación o revisión' },
      { id: 'write', label: 'Lectura y escritura', description: 'Sólo para proveedores locales que trabajan en el workspace' },
    ], { title: 'Capacidades declaradas', ignoreFocusOut: true });
    if (!permission) return;
    const targets: string[] = [];
    let transport = 'provider';
    if (kind.id === 'http-json') {
      transport = 'http-json';
      if (permission.id === 'write') {
        void vscode.window.showWarningMessage('HTTP JSON v1 es de sólo lectura. Elegí sólo lectura para ese transporte.');
        return;
      }
      const endpoint = await vscode.window.showInputBox({ title: 'Endpoint HTTPS del agente', placeHolder: 'https://agents.example/invoke', ignoreFocusOut: true });
      if (!endpoint) return;
      targets.push(`endpoint=${endpoint}`);
    } else if (kind.id === 'kiro-cli') {
      const agentName = await vscode.window.showInputBox({ title: 'Nombre del agente en Kiro', placeHolder: 'mi-agente', ignoreFocusOut: true });
      if (!agentName) return;
      targets.push('provider=kiro-cli', `agent=${agentName}`);
    } else {
      targets.push('provider=codex', 'runner=exec-json');
    }
    const args = [
      'publish', reference,
      '--owner', owner,
      '--transport', transport,
      '--capability', 'workspace.read',
      '--effect-mode', permission.id === 'write' ? 'workspace-write' : 'read-only',
      ...targets.flatMap((target) => ['--target', target]),
    ];
    if (permission.id === 'write') args.push('--capability', 'workspace.write');
    const result = await this.withProgress('Registrando la versión del agente…', () => this.runtime.manageLocalAgent(root, args));
    if (result.ok !== true) {
      void vscode.window.showErrorMessage(this.resultReason(result));
      return;
    }
    await this.refresh();
    void vscode.window.showInformationMessage(`${reference} quedó registrado sin editar agents.json manualmente.`);
  }

  private async restoreStandardProvider(): Promise<void> {
    const role = await vscode.window.showQuickPick([
      { id: 'architect' as BaldrRole, label: 'Planificación' },
      { id: 'implementer' as BaldrRole, label: 'Ejecución' },
      { id: 'reviewer' as BaldrRole, label: 'Revisión' },
    ], { title: 'Etapa que volverá a un proveedor normal', ignoreFocusOut: true });
    if (!role) return;
    const provider = await vscode.window.showQuickPick([
      { id: 'codex', label: 'Codex normal', description: 'Usa la configuración predeterminada de Codex' },
      { id: 'kiro-cli', label: 'Kiro normal', description: 'Usa la configuración predeterminada de Kiro CLI' },
    ], { title: `Proveedor normal para ${role.label.toLowerCase()}`, ignoreFocusOut: true });
    if (!provider) return;
    const root = this.requireWorkspace();
    const profileName = `provider-${provider.id}-default`;
    const preferences = record(record(this.lastStatus.workbench).preferences);
    const selectedByRole = record(preferences.role_profiles);
    const roleProfiles: Record<string, string[]> = {};
    for (const currentRole of BALDR_ROLES) {
      const current = selectedByRole[currentRole];
      if (Array.isArray(current) && current.length) roleProfiles[currentRole] = current.map(String);
    }
    roleProfiles[role.id] = [profileName];
    await this.withProgress('Restaurando el proveedor normal…', async () => {
      const saved = await this.runtime.upsertExecutionProfile(root, {
        name: profileName,
        provider: provider.id,
        agent: provider.id === 'kiro-cli' ? 'kiro_default' : '',
        description: `Proveedor ${provider.label} sin AgentRef externo.`,
      });
      if (saved.ok === false) throw new Error(this.resultReason(saved));
      const updated = await this.runtime.setWorkspacePreferences(root, {
        preset: 'custom',
        roleProfiles,
        teamMode: 'configured',
        agentOverrides: {},
      });
      if (updated.ok === false) throw new Error(this.resultReason(updated));
    });
    await this.refresh();
    void vscode.window.showInformationMessage(`${role.label} volvió a ${provider.label}.`);
  }

  private currentRoleProfile(role: BaldrRole): JsonRecord {
    const workbench = record(this.lastStatus.workbench);
    const profilesData = record(workbench.profiles);
    const preferences = record(workbench.preferences);
    const executionProfiles = record(profilesData.execution_profiles);
    const selectedByRole = record(preferences.role_profiles);
    const selectedNames = Array.isArray(selectedByRole[role])
      ? (selectedByRole[role] as unknown[]).map(String)
      : [];
    if (selectedNames[0] && executionProfiles[selectedNames[0]]) {
      return record(executionProfiles[selectedNames[0]]);
    }
    const resolvedByRole = record(profilesData.resolved_roles);
    const resolved = Array.isArray(resolvedByRole[role]) ? resolvedByRole[role] : [];
    return record(resolved[0]);
  }

  private codexCatalogOptions(catalog: JsonRecord): CodexModelOption[] {
    const rawModels = Array.isArray(catalog.models) ? catalog.models : [];
    const seen = new Set<string>();
    const models: CodexModelOption[] = [];
    for (const rawModel of rawModels) {
      const value = record(rawModel);
      const model = text(value.model, text(value.id)).trim();
      if (!model || seen.has(model)) continue;
      seen.add(model);
      const rawEfforts = Array.isArray(value.reasoning_efforts) ? value.reasoning_efforts : [];
      const efforts = [...new Set(rawEfforts
        .map((rawEffort) => text(record(rawEffort).id).trim())
        .filter(Boolean))];
      const defaultEffort = text(value.default_reasoning_effort).trim();
      if (!efforts.length && defaultEffort) efforts.push(defaultEffort);
      models.push({
        model,
        displayName: displayModelName(model, text(value.display_name).trim()),
        description: text(value.description).trim(),
        defaultEffort,
        efforts,
        isDefault: value.is_default === true,
      });
    }
    return models;
  }

  private async chooseCodexTeamModels(): Promise<void> {
    let catalog: JsonRecord;
    try {
      catalog = await vscode.window.withProgress({
        location: vscode.ProgressLocation.Notification,
        title: 'Cargando los modelos disponibles…',
        cancellable: true,
      }, (_progress, token) => this.runtime.providerModels('codex', token));
    } catch (error) {
      if (error instanceof vscode.CancellationError) return;
      this.output.appendLine(`[modelos] ${error instanceof Error ? error.message : String(error)}`);
      await this.offerModelCatalogFallback();
      return;
    }

    const models = this.codexCatalogOptions(catalog);
    if (catalog.ok !== true || !models.length) {
      const reason = text(catalog.reason, text(record(catalog.error).message));
      if (reason) this.output.appendLine(`[modelos] ${reason}`);
      await this.offerModelCatalogFallback();
      return;
    }

    const mode = await vscode.window.showQuickPick([
      {
        id: 'same',
        label: 'Usar el mismo modelo en todo',
        description: 'La opción más simple',
      },
      {
        id: 'per-role',
        label: 'Elegir un modelo para cada etapa',
        description: 'Planificación, ejecución y revisión pueden ser diferentes',
      },
    ], {
      title: 'Modelos del equipo',
      placeHolder: 'Podés usar una opción en todo el trabajo o combinar varias',
      ignoreFocusOut: true,
    });
    if (!mode) return;

    const choices = {} as Record<BaldrRole, CodexTeamChoice>;
    if (mode.id === 'same') {
      const current = this.currentRoleProfile('architect');
      const selected = await this.pickCodexModel(models, 'Modelo para todo el trabajo', current);
      if (!selected) return;
      for (const role of BALDR_ROLES) choices[role] = selected;
    } else {
      for (const role of BALDR_ROLES) {
        const selected = await this.pickCodexModel(
          models,
          `Modelo para ${roleLabel(role)}`,
          this.currentRoleProfile(role),
        );
        if (!selected) return;
        choices[role] = selected;
      }
    }

    const roleProfiles = {} as Record<BaldrRole, string[]>;
    const definitions = new Map<string, CodexTeamChoice>();
    for (const role of BALDR_ROLES) {
      const selected = choices[role];
      const name = generatedProfileName(selected.model, selected.effort);
      definitions.set(name, selected);
      roleProfiles[role] = [name];
    }

    const root = this.requireWorkspace();
    try {
      await this.withProgress('Guardando el equipo de Baldr…', async () => {
        const profilesData = record(record(this.lastStatus.workbench).profiles);
        const existingProfiles = record(profilesData.execution_profiles);
        for (const [name, selected] of definitions) {
          const existing = record(existingProfiles[name]);
          if (
            text(existing.provider) === 'codex'
            && text(existing.model) === selected.model
            && text(existing.reasoning_effort) === selected.effort
          ) continue;
          const saved = await this.runtime.upsertExecutionProfile(root, {
            name,
            provider: 'codex',
            model: selected.model,
            reasoning_effort: selected.effort,
            description: `${selected.displayName} con nivel ${effortLabel(selected.effort).toLowerCase()}.`,
          });
          if (saved.ok === false) throw new Error(this.resultReason(saved));
        }
        const updated = await this.runtime.setWorkspacePreferences(root, {
          preset: 'custom',
          roleProfiles,
        });
        if (updated.ok === false) throw new Error(this.resultReason(updated));
      });
    } catch (error) {
      const message = error instanceof Error ? error.message : String(error);
      this.output.appendLine(`[modelos] ${message}`);
      void vscode.window.showErrorMessage(`No pudimos guardar el equipo de Baldr. ${message}`);
      return;
    }
    await this.refresh();
  }

  private async pickCodexModel(
    models: CodexModelOption[],
    title: string,
    current: JsonRecord,
  ): Promise<CodexTeamChoice | undefined> {
    const currentModel = text(current.model);
    const orderedModels = [...models].sort((left, right) => {
      const leftRank = left.model === currentModel ? 0 : left.isDefault ? 1 : 2;
      const rightRank = right.model === currentModel ? 0 : right.isDefault ? 1 : 2;
      return leftRank - rightRank;
    });
    const selected = await vscode.window.showQuickPick(orderedModels.map((model) => ({
      model,
      label: `${model.model === currentModel ? '$(check)' : '$(sparkle)'} ${model.displayName}`,
      description: model.model === currentModel
        ? 'Actual'
        : model.isDefault ? 'Recomendado por Codex' : 'Disponible',
    })), {
      title,
      placeHolder: 'Elegí el modelo que querés usar',
      ignoreFocusOut: true,
    });
    if (!selected) return undefined;

    const model = selected.model;
    if (!model.efforts.length) {
      return { model: model.model, displayName: model.displayName, effort: model.defaultEffort };
    }
    const currentEffort = model.model === currentModel
      ? text(current.reasoning_effort, model.defaultEffort)
      : model.defaultEffort;
    const orderedEfforts = [...model.efforts].sort((left, right) => {
      const leftRank = left === currentEffort ? 0 : left === model.defaultEffort ? 1 : 2;
      const rightRank = right === currentEffort ? 0 : right === model.defaultEffort ? 1 : 2;
      return leftRank - rightRank;
    });
    const effort = await vscode.window.showQuickPick(orderedEfforts.map((value) => ({
      id: value,
      label: `${value === currentEffort ? '$(check)' : '$(settings)'} ${effortLabel(value)}`,
      description: value === currentEffort
        ? 'Actual'
        : value === model.defaultEffort ? 'Recomendado para este modelo' : '',
      detail: effortDescription(value),
    })), {
      title: `Variante de ${model.displayName}`,
      placeHolder: 'Elegí cuánta profundidad querés para el análisis',
      ignoreFocusOut: true,
    });
    if (!effort) return undefined;
    return { model: model.model, displayName: model.displayName, effort: effort.id };
  }

  private resultReason(result: JsonRecord): string {
    return text(result.reason, text(record(result.error).message, 'Baldr no pudo guardar la configuración.'));
  }

  private async offerModelCatalogFallback(): Promise<void> {
    const choice = await vscode.window.showWarningMessage(
      'No pudimos cargar los modelos de Codex. Tu configuración actual no se modificó.',
      'Usar configuraciones guardadas',
      'Configurar manualmente',
    );
    if (choice === 'Usar configuraciones guardadas') await this.chooseSavedRoleProfiles();
    else if (choice === 'Configurar manualmente') await this.createExecutionProfile();
  }

  private async chooseSavedRoleProfiles(): Promise<void> {
    const workbench = record(this.lastStatus.workbench);
    const profilesData = record(workbench.profiles);
    const executionProfiles = record(profilesData.execution_profiles);
    const profileNames = Object.keys(executionProfiles).sort();
    if (!profileNames.length) {
      const choice = await vscode.window.showWarningMessage(
        'Todavía no hay configuraciones guardadas.',
        'Crear una configuración',
      );
      if (choice === 'Crear una configuración') await this.createExecutionProfile();
      return;
    }
    const mode = await vscode.window.showQuickPick([
      { id: 'same', label: 'Usar la misma configuración en todo', description: 'La opción más simple' },
      { id: 'per-role', label: 'Elegir una configuración para cada etapa', description: 'Planificación, ejecución y revisión pueden ser diferentes' },
    ], { title: 'Equipo de Baldr', ignoreFocusOut: true });
    if (!mode) return;

    const roleProfiles: Record<string, string[]> = {};
    if (mode.id === 'same') {
      const chosen = await this.pickProfile(profileNames, executionProfiles, 'Configuración para todas las etapas');
      if (!chosen) return;
      for (const role of BALDR_ROLES) roleProfiles[role] = [chosen];
    } else {
      for (const role of BALDR_ROLES) {
        const chosen = await this.pickProfile(profileNames, executionProfiles, `Configuración para ${roleLabel(role)}`);
        if (!chosen) return;
        roleProfiles[role] = [chosen];
      }
    }
    const root = this.requireWorkspace();
    await this.withProgress('Actualizando el equipo de Baldr…', () => this.runtime.setWorkspacePreferences(root, {
      preset: 'custom',
      roleProfiles,
    }));
    await this.refresh();
  }

  private async pickProfile(names: string[], profiles: JsonRecord, title: string): Promise<string | undefined> {
    const selected = await vscode.window.showQuickPick(names.map((name) => {
      const value = record(profiles[name]);
      return {
        id: name,
        label: name,
        description: [text(value.agent_ref), text(value.provider), text(value.model), text(value.reasoning_effort)].filter(Boolean).join(' · '),
      };
    }), { title, ignoreFocusOut: true });
    return selected?.id;
  }

  private async createExecutionProfile(): Promise<void> {
    const status = await this.runtime.runRouterJson(['provider-status']);
    const providers = Object.keys(record(status.providers));
    const provider = await vscode.window.showQuickPick(providers, {
      title: 'Nueva configuración avanzada — proveedor',
      ignoreFocusOut: true,
    });
    if (!provider) return;
    const name = await vscode.window.showInputBox({
      title: 'Nueva configuración avanzada — nombre',
      placeHolder: 'trabajo-rapido',
      validateInput: (value) => /^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$/.test(value)
        ? undefined
        : 'Usá letras, números, puntos, guiones bajos o guiones.',
      ignoreFocusOut: true,
    });
    if (!name) return;
    const modelOrAgent = await vscode.window.showInputBox({
      title: provider === 'kiro-cli' ? 'Agente de Kiro (opcional)' : 'Modelo (opcional)',
      placeHolder: 'Dejalo vacío para usar el valor predeterminado',
      ignoreFocusOut: true,
    });
    const effort = await vscode.window.showQuickPick(['', 'minimal', 'low', 'medium', 'high', 'xhigh'], {
      title: 'Nivel de análisis',
      placeHolder: 'Vacío usa el valor predeterminado',
      ignoreFocusOut: true,
    });
    if (effort === undefined) return;
    await this.withProgress('Guardando la configuración…', () => this.runtime.upsertExecutionProfile(this.currentWorkspaceRoot(), {
      name,
      provider,
      model: provider === 'kiro-cli' ? '' : modelOrAgent ?? '',
      agent: provider === 'kiro-cli' ? modelOrAgent ?? '' : '',
      reasoning_effort: provider === 'kiro-cli' ? '' : effort,
      effort: provider === 'kiro-cli' ? effort : '',
      description: `Creada desde la consola de Baldr para ${provider}.`,
    }));
    await this.refresh();
    const assign = await vscode.window.showInformationMessage(
      `La configuración ${name} está lista. ¿Querés usarla ahora?`,
      'Configurar equipo',
      'Más tarde',
    );
    if (assign === 'Configurar equipo') await this.chooseRoleProfiles();
  }

  private async chooseReconciliation(itemId = this.selectedItemId): Promise<void> {
    if (!itemId) return;
    const item = this.selectedItem();
    const actions = allowedActions(item);
    const nonGit = text(item?.safety_mode) === 'non-git';
    const options = [
      { id: 'authorize_changes', label: 'Autorizar cambios y reintentar', description: 'Permitir que Baldr cree o modifique los archivos necesarios y continúe' },
      { id: 'decline_changes', label: 'No autorizar', description: 'Cerrar la sesión sin crear ni modificar archivos' },
      { id: 'inspect_shadow', label: 'Ver la copia protegida', description: 'Revisar el trabajo guardado por Baldr sin cambiar tus archivos' },
      { id: 'continue_from_shadow', label: 'Continuar desde la copia protegida', description: 'Retomar desde el último punto que Baldr verificó' },
      { id: 'apply_shadow_changes', label: 'Aplicar los cambios protegidos', description: 'Comprobar de nuevo la carpeta y copiar sólo los cambios de Baldr' },
      { id: 'discard_shadow', label: 'Descartar la copia protegida', description: 'Eliminar esta copia sin modificar la carpeta original' },
      { id: 'resume_from_checkpoint', label: 'Continuar desde el último respaldo', description: 'Recuperar el último punto seguro de Baldr' },
      {
        id: 'accept_existing_changes',
        label: nonGit ? 'Continuar con los archivos actuales' : 'Conservar los cambios actuales',
        description: nonGit
          ? 'Seguir con lo que ya está en la carpeta; no hay un respaldo para volver atrás'
          : 'Tomar como válidos los cambios que ya están en la carpeta',
      },
      { id: 'discard_worktree', label: 'Descartar los cambios interrumpidos', description: 'Eliminar la copia aislada que no se terminó' },
      { id: 'mark_failed', label: 'Dar la sesión por fallida', description: 'Detener la recuperación y conservar los detalles' },
    ].filter((option) => actions.includes(option.id));
    if (!options.length) {
      void vscode.window.showInformationMessage('Esta sesión no necesita ninguna acción de recuperación.');
      return;
    }
    const selected = await vscode.window.showQuickPick(options, {
      title: actions.includes('authorize_changes') ? 'Autorizar cambios' : 'Recuperar la sesión',
      ignoreFocusOut: true,
    });
    if (!selected) return;
    const root = this.requireWorkspace();
    if (selected.id === 'inspect_shadow') {
      this.launchOperation('Preparando la copia para revisar…', async () => {
        const result = await this.runtime.reconcileWorkItem(root, itemId, selected.id);
        const shadowPath = text(record(result.reconciliation).execution_root, '');
        if (shadowPath) {
          const choice = await vscode.window.showInformationMessage(
            `La copia protegida está en ${shadowPath}`,
            'Mostrar carpeta',
          );
          if (choice === 'Mostrar carpeta') {
            await vscode.commands.executeCommand('revealFileInOS', vscode.Uri.file(shadowPath));
          }
        }
        return { ...result, ok: true };
      });
      return;
    }
    this.launchOperation(
      'Recuperando la sesión…',
      () => this.runtime.reconcileWorkItem(root, itemId, selected.id),
      undefined,
      false,
    );
  }

  private async attachCurrentFile(): Promise<void> {
    const editor = vscode.window.activeTextEditor;
    if (!editor) {
      void vscode.window.showInformationMessage('Abrí un archivo para poder agregarlo.');
      return;
    }
    const root = this.requireWorkspace();
    const relative = path.relative(root, editor.document.uri.fsPath);
    if (!relative || relative.startsWith('..') || path.isAbsolute(relative)) {
      void vscode.window.showWarningMessage('Solo podés agregar archivos que estén dentro de esta carpeta.');
      return;
    }
    this.pending.attachments.push({ kind: 'file', label: relative, path: editor.document.uri.fsPath });
    await this.post({ type: 'pending', pending: this.pending });
  }

  private async attachFileOrFolder(): Promise<void> {
    const root = this.requireWorkspace();
    const selected = await vscode.window.showOpenDialog({
      title: 'Agregar archivos o carpetas',
      defaultUri: vscode.Uri.file(root),
      canSelectFiles: true,
      canSelectFolders: true,
      canSelectMany: true,
      openLabel: 'Agregar',
    });
    if (!selected?.length) return;

    let skipped = 0;
    for (const uri of selected) {
      const relative = path.relative(root, uri.fsPath);
      if (!relative || relative.startsWith('..') || path.isAbsolute(relative)) {
        skipped += 1;
        continue;
      }
      const stat = await vscode.workspace.fs.stat(uri);
      this.pending.attachments.push({
        kind: stat.type & vscode.FileType.Directory ? 'folder' : 'file',
        label: relative,
        path: uri.fsPath,
      });
    }
    await this.post({ type: 'pending', pending: this.pending });
    if (skipped) {
      void vscode.window.showWarningMessage('Solo podés agregar archivos y carpetas que estén dentro de la carpeta abierta.');
    }
  }

  private async attachSelection(): Promise<void> {
    const editor = vscode.window.activeTextEditor;
    if (!editor || editor.selection.isEmpty) {
      void vscode.window.showInformationMessage('Seleccioná primero el texto que querés agregar.');
      return;
    }
    const selected = editor.document.getText(editor.selection).slice(0, MAX_SELECTION_CHARS);
    const root = this.currentWorkspaceRoot();
    const relative = root ? path.relative(root, editor.document.uri.fsPath) : editor.document.uri.fsPath;
    const range = {
      startLine: editor.selection.start.line + 1,
      endLine: editor.selection.end.line + 1,
    };
    const contextKey = `${editor.document.uri.toString()}:${range.startLine}-${range.endLine}:${Date.now()}`;
    this.pending.attachments.push({ kind: 'selection', label: `${relative}:${range.startLine}-${range.endLine}`, path: editor.document.uri.fsPath, range, language: editor.document.languageId, contextKey });
    this.pending.selectionContexts[contextKey] = `Texto seleccionado en ${relative}:${range.startLine}-${range.endLine}:\n\n${selected}`;
    this.pending.extraContext = Object.values(this.pending.selectionContexts).join('\n\n');
    await this.post({ type: 'pending', pending: this.pending });
  }

  private async withProgress<T>(title: string, task: () => Promise<T>): Promise<T> {
    return vscode.window.withProgress({ location: vscode.ProgressLocation.Notification, title }, task);
  }

  private html(webview: vscode.Webview): string {
    const nonce = Math.random().toString(36).slice(2);
    return `<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src ${webview.cspSource} 'unsafe-inline'; script-src 'nonce-${nonce}';">
<title>Baldr</title>
<style>
:root {
  color-scheme: light dark;
  --baldr-content-width: 720px;
  --baldr-surface: color-mix(in srgb, var(--vscode-editorWidget-background, var(--vscode-sideBar-background)) 72%, transparent);
  --baldr-warning-accent: var(--vscode-editorWarning-foreground, var(--vscode-inputValidation-warningBorder, var(--vscode-testing-iconFailed)));
  --baldr-error-accent: var(--vscode-editorError-foreground, var(--vscode-inputValidation-errorBorder, var(--vscode-testing-iconFailed)));
}
* { box-sizing: border-box; }
body { margin: 0; height: 100vh; overflow: hidden; color: var(--vscode-foreground); background: var(--vscode-sideBar-background); font: 13px/1.4 var(--vscode-font-family); }
button, textarea { font: inherit; }
button { color: inherit; -webkit-tap-highlight-color: transparent; }
button:focus-visible { outline: 1px solid var(--vscode-focusBorder); outline-offset: 1px; }
#root { height: 100%; display: grid; grid-template-rows: auto minmax(0, 1fr) auto; }
.header { padding: 12px 16px 8px; border-bottom: 1px solid var(--vscode-sideBar-border, transparent); }
.header-row, .history-panel { width: 100%; max-width: var(--baldr-content-width); margin-left: auto; margin-right: auto; }
.header-row { display: flex; align-items: center; justify-content: space-between; gap: 8px; }
.header-actions { display: flex; align-items: center; justify-content: center; gap: 2px; }
.heading { font-size: 11px; font-weight: 700; letter-spacing: .08em; text-transform: uppercase; opacity: .8; }
.icon-button { display: inline-grid; place-items: center; border: 0; background: transparent; border-radius: 6px; width: 28px; height: 28px; padding: 0; cursor: pointer; color: var(--vscode-icon-foreground, var(--vscode-foreground)); opacity: .82; }
.icon-button:hover { background: var(--vscode-toolbar-hoverBackground); opacity: 1; }
.button-icon { display: block; width: 16px; height: 16px; fill: currentColor; pointer-events: none; }
.history-panel[hidden] { display: none; }
.history-toggle-icon { transition: transform 120ms ease; }
.history-toggle[aria-expanded="true"] .history-toggle-icon { transform: rotate(180deg); }
.history-controls { display: flex; gap: 4px; margin-top: 8px; }
.history-filter { flex: 1 1 0; min-width: 0; min-height: 26px; overflow: hidden; border: 1px solid var(--vscode-widget-border); border-radius: 5px; padding: 3px 5px; background: transparent; color: var(--vscode-descriptionForeground); cursor: pointer; font-size: 10px; text-overflow: ellipsis; white-space: nowrap; }
.history-filter:hover { background: var(--vscode-list-hoverBackground); color: var(--vscode-foreground); }
.history-filter[aria-pressed="true"] { border-color: var(--vscode-focusBorder); background: var(--vscode-list-activeSelectionBackground); color: var(--vscode-list-activeSelectionForeground); }
.history-search { width: 100%; margin-top: 6px; border: 1px solid var(--vscode-input-border, var(--vscode-widget-border)); border-radius: 5px; padding: 5px 7px; color: var(--vscode-input-foreground); background: var(--vscode-input-background); font: inherit; font-size: 11px; }
.history-search:focus { border-color: var(--vscode-focusBorder); outline: 0; }
.task-list { margin-top: 6px; max-height: min(30vh, 240px); overflow: auto; }
.task-empty { display: flex; align-items: center; justify-content: space-between; gap: 8px; padding: 8px 4px; color: var(--vscode-descriptionForeground); }
.task-empty-action { flex: none; border: 0; padding: 2px 0; background: transparent; color: var(--vscode-textLink-foreground); cursor: pointer; font-size: 10px; }
.task-empty-action:hover { color: var(--vscode-textLink-activeForeground, var(--vscode-textLink-foreground)); text-decoration: underline; }
.task-wrap { display: grid; grid-template-columns: minmax(0, 1fr) 28px; align-items: start; border-radius: 6px; }
.task { width: 100%; display: grid; grid-template-columns: 13px minmax(0,1fr); gap: 7px; align-items: start; text-align: left; border: 0; padding: 8px 4px; background: transparent; border-radius: 6px; cursor: pointer; }
.task:hover, .task.selected { background: var(--vscode-list-hoverBackground); }
.task.selected { outline: 1px solid var(--vscode-focusBorder); }
.task-main { min-width: 0; }
.task-title { display: block; overflow: hidden; color: var(--vscode-foreground); font-weight: 600; text-overflow: ellipsis; white-space: nowrap; }
.task-summary { display: block; margin-top: 1px; overflow: hidden; color: var(--vscode-descriptionForeground); font-size: 10px; line-height: 1.35; text-overflow: ellipsis; white-space: nowrap; }
.task-meta { display: flex; min-width: 0; gap: 5px; margin-top: 3px; align-items: center; color: var(--vscode-descriptionForeground); font-size: 10px; }
.task-meta-status { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.task-meta-time { flex: none; }
.task-menu { width: 28px; height: 28px; margin-top: 3px; border: 0; border-radius: 5px; padding: 0; background: transparent; color: var(--vscode-descriptionForeground); cursor: pointer; font-size: 16px; line-height: 1; }
.task-menu:hover, .task-menu[aria-expanded="true"] { background: var(--vscode-toolbar-hoverBackground); color: var(--vscode-foreground); }
.task-actions { grid-column: 1 / -1; display: flex; flex-wrap: wrap; gap: 4px; padding: 1px 4px 7px 24px; }
.task-action { min-height: 25px; border: 1px solid var(--vscode-widget-border); border-radius: 5px; padding: 3px 6px; background: var(--vscode-button-secondaryBackground); color: var(--vscode-button-secondaryForeground); cursor: pointer; font-size: 10px; }
.task-action:hover { background: var(--vscode-button-secondaryHoverBackground, var(--vscode-toolbar-hoverBackground)); }
.task-action.danger { border-color: var(--baldr-error-accent); }
.dot { width: 7px; height: 7px; border-radius: 50%; background: var(--vscode-descriptionForeground); }
.task .dot { margin-top: 5px; }
.dot.running { background: var(--vscode-progressBar-background); box-shadow: 0 0 0 2px color-mix(in srgb, var(--vscode-progressBar-background) 25%, transparent); }
.dot.completed { background: var(--vscode-testing-iconPassed); }
.dot.failed, .dot.needs_attention { background: var(--vscode-testing-iconFailed); }
.dot.draft, .dot.cancelled { background: var(--vscode-descriptionForeground); }
.content { min-height: 0; overflow: auto; padding: 16px; position: relative; }
.content-background { width: 100%; max-width: var(--baldr-content-width); margin: 0 auto; }
.empty { min-height: 100%; display: grid; place-items: center; text-align: center; color: var(--vscode-descriptionForeground); }
.empty-inner { max-width: 330px; line-height: 1.5; }
.empty-mark { display: grid; place-items: center; margin-bottom: 14px; color: var(--vscode-descriptionForeground); opacity: .72; }
.empty-logo { display: block; width: 34px; height: 34px; fill: none; stroke: currentColor; stroke-width: 1.5; stroke-linecap: round; stroke-linejoin: round; }
.empty-title { color: var(--vscode-foreground); font-weight: 600; margin-bottom: 3px; }
.empty-detail { color: var(--vscode-descriptionForeground); }
.item-title { font-size: 16px; font-weight: 650; margin: 0 0 4px; line-height: 1.3; }
.item-meta { display: flex; flex-wrap: wrap; gap: 6px; font-size: 11px; color: var(--vscode-descriptionForeground); }
.task-body { margin: 12px 0; white-space: pre-wrap; line-height: 1.45; overflow-wrap: anywhere; }
.conversation-turn { padding: 8px 0; border-bottom: 1px solid var(--vscode-widget-border); }
.conversation-turn:last-child { border-bottom: 0; }
.conversation-turn-label { margin-bottom: 4px; color: var(--vscode-descriptionForeground); font-size: 10px; font-weight: 600; text-transform: uppercase; }
.session-section { margin-top: 12px; border: 1px solid var(--vscode-widget-border); border-radius: 9px; background: var(--baldr-surface); }
.session-section > summary { padding: 9px 11px; color: var(--vscode-foreground); cursor: pointer; font-size: 11px; font-weight: 600; }
.session-section[open] > summary { border-bottom: 1px solid var(--vscode-widget-border); }
.session-section-body { padding: 0 11px 11px; }
.session-section .stage-list { margin-bottom: 0; }
.sr-only { position: absolute !important; width: 1px; height: 1px; padding: 0; margin: -1px; overflow: hidden; clip: rect(0,0,0,0); white-space: nowrap; border: 0; }
.now-card, .result-card, .attention-card { min-width: 0; margin: 14px 0; padding: 12px; border: 1px solid var(--vscode-widget-border); border-radius: 10px; background: var(--baldr-surface); overflow-wrap: anywhere; }
.now-card { border-color: color-mix(in srgb, var(--vscode-progressBar-background) 48%, var(--vscode-widget-border)); }
.attention-card, .result-card.warning { border-color: var(--baldr-warning-accent); border-left-width: 4px; }
.result-card.positive { border-color: color-mix(in srgb, var(--vscode-testing-iconPassed) 48%, var(--vscode-widget-border)); }
.refresh-error { margin: 10px 0; padding: 10px; border: 1px solid var(--baldr-error-accent); border-left-width: 4px; border-radius: 8px; background: var(--baldr-surface); }
.refresh-error-title { font-weight: 650; }
.refresh-error-copy { margin: 4px 0 9px; color: var(--vscode-descriptionForeground); }
.card-eyebrow { margin-bottom: 3px; color: var(--vscode-descriptionForeground); font-size: 10px; font-weight: 700; letter-spacing: .08em; text-transform: uppercase; }
.attention-card .card-eyebrow, .result-card.warning .card-eyebrow { color: var(--baldr-warning-accent); }
.card-title { margin: 0; font-size: 14px; font-weight: 650; line-height: 1.3; }
.card-copy { margin: 5px 0 0; color: var(--vscode-descriptionForeground); line-height: 1.45; white-space: pre-wrap; }
.last-update { margin-top: 8px; color: var(--vscode-descriptionForeground); font-size: 10px; }
.stage-strip { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 5px; margin-top: 11px; }
.stage-strip-item { min-width: 0; padding-top: 6px; border-top: 2px solid var(--vscode-widget-border); color: var(--vscode-descriptionForeground); font-size: 10px; overflow-wrap: anywhere; }
.stage-strip-item.active { border-color: var(--vscode-progressBar-background); color: var(--vscode-foreground); }
.stage-strip-item.complete { border-color: var(--vscode-testing-iconPassed); color: var(--vscode-foreground); }
.stage-strip-item.attention { border-color: var(--vscode-testing-iconFailed); color: var(--vscode-foreground); }
.stage-strip-icon { margin-right: 3px; font-weight: 700; }
.recent-milestone { display: flex; gap: 6px; margin-top: 9px; color: var(--vscode-descriptionForeground); font-size: 11px; }
.stage-milestones { margin-top: 11px; }
.stage-milestones-title { margin: 0 0 5px; font-size: 11px; font-weight: 650; }
.stage-milestones-list { list-style: none; margin: 0; padding: 0; }
.stage-milestone { display: grid; grid-template-columns: 14px minmax(0, 1fr); gap: 5px; margin: 5px 0; color: var(--vscode-descriptionForeground); font-size: 11px; }
.stage-milestone-copy { min-width: 0; }
.stage-milestone-evidence { display: block; margin-top: 1px; font-size: 10px; opacity: .78; }
.stage-deliverables { margin-top: 11px; }
.stage-deliverables-title { margin: 0 0 5px; font-size: 11px; font-weight: 650; }
.stage-deliverable-row { display: flex; align-items: flex-start; justify-content: space-between; gap: 8px; margin: 7px 0; }
.stage-deliverable-copy { display: block; flex: 1; min-width: 0; color: var(--vscode-descriptionForeground); font-size: 10px; overflow-wrap: anywhere; }
.stage-deliverable-name { display: block; color: var(--vscode-foreground); font-weight: 650; }
.stage-deliverable-preview, .stage-deliverable-note { display: block; margin-top: 2px; }
.stage-deliverable-status { flex: none; color: var(--vscode-descriptionForeground); font-size: 10px; }
.deliverable-index { display: grid; justify-items: center; gap: 5px; margin: 12px 0; text-align: center; }
.deliverable-index-note { color: var(--vscode-descriptionForeground); font-size: 10px; }
.deliverable-panel { position: fixed; z-index: 30; inset: 8px 8px 132px 8px; display: flex; flex-direction: column; min-width: 0; border: 1px solid var(--vscode-widget-border); border-radius: 10px; background: var(--vscode-editorWidget-background, var(--vscode-sideBar-background)); box-shadow: 0 8px 28px var(--vscode-widget-shadow); }
.deliverable-header { display: flex; align-items: flex-start; justify-content: space-between; gap: 8px; padding: 12px; border-bottom: 1px solid var(--vscode-widget-border); }
.deliverable-title { margin: 0; font-size: 14px; }
.deliverable-subtitle { margin-top: 3px; color: var(--vscode-descriptionForeground); font-size: 10px; }
.deliverable-body { min-height: 0; overflow: auto; padding: 12px; }
.deliverable-note { margin: 0 0 10px; color: var(--vscode-descriptionForeground); font-size: 11px; }
.deliverable-section { margin: 0 0 14px; }
.deliverable-section-title { margin: 0 0 5px; font-size: 12px; }
.deliverable-entries { margin: 0; padding-left: 18px; }
.deliverable-entry { margin: 4px 0; white-space: pre-wrap; overflow-wrap: anywhere; }
.deliverable-footer { display: flex; justify-content: center; padding: 10px 12px; border-top: 1px solid var(--vscode-widget-border); }
.stage-list { display: grid; gap: 8px; margin: 12px 0; }
.stage-card { min-width: 0; border: 1px solid var(--vscode-widget-border); border-radius: 9px; overflow: hidden; background: color-mix(in srgb, var(--vscode-sideBar-background) 72%, var(--vscode-input-background)); }
.stage-toggle { display: grid; grid-template-columns: 20px minmax(0, 1fr) auto; align-items: center; width: 100%; min-width: 0; gap: 7px; padding: 10px; border: 0; color: inherit; text-align: left; background: transparent; cursor: pointer; }
.stage-toggle:hover { background: var(--vscode-list-hoverBackground); }
.stage-state-icon { display: grid; place-items: center; width: 18px; height: 18px; border: 1px solid var(--vscode-widget-border); border-radius: 50%; color: var(--vscode-descriptionForeground); font-size: 10px; font-weight: 700; }
.stage-card.active .stage-state-icon { border-color: var(--vscode-progressBar-background); color: var(--vscode-progressBar-background); }
.stage-card.complete .stage-state-icon { border-color: var(--vscode-testing-iconPassed); color: var(--vscode-testing-iconPassed); }
.stage-card.attention .stage-state-icon { border-color: var(--vscode-testing-iconFailed); color: var(--vscode-testing-iconFailed); }
.stage-heading { min-width: 0; }
.stage-title { display: block; font-weight: 650; }
.stage-subtitle { display: block; color: var(--vscode-descriptionForeground); font-size: 10px; line-height: 1.35; }
.stage-chevron { color: var(--vscode-descriptionForeground); font-size: 11px; transition: transform 120ms ease; }
.stage-toggle[aria-expanded="true"] .stage-chevron { transform: rotate(90deg); }
.stage-body { min-width: 0; padding: 0 10px 11px 37px; overflow-wrap: anywhere; }
.stage-body[hidden] { display: none; }
.stage-status { color: var(--vscode-descriptionForeground); font-size: 11px; font-weight: 600; }
.stage-duration { margin-top: 2px; color: var(--vscode-descriptionForeground); font-size: 10px; }
.stage-duration:empty { display: none; }
.stage-purpose { margin: 4px 0 0; line-height: 1.45; }
.report-summary { margin: 9px 0 0; padding-left: 9px; border-left: 2px solid var(--vscode-widget-border); line-height: 1.45; white-space: pre-wrap; }
.facts { display: flex; flex-wrap: wrap; gap: 5px; margin-top: 9px; }
.fact { padding: 2px 6px; border: 1px solid var(--vscode-widget-border); border-radius: 999px; color: var(--vscode-descriptionForeground); font-size: 10px; }
.fact.positive { border-color: color-mix(in srgb, var(--vscode-testing-iconPassed) 45%, var(--vscode-widget-border)); }
.fact.warning, .fact.danger { border-color: var(--baldr-warning-accent); color: var(--vscode-foreground); }
.report-section { margin-top: 11px; }
.report-section-title { margin: 0 0 4px; font-size: 11px; font-weight: 650; }
.report-section ul { margin: 0; padding-left: 17px; }
.report-section li { margin: 3px 0; white-space: pre-wrap; }
.report-section.warning, .report-section.danger { color: var(--vscode-foreground); }
.disclosure { margin-top: 10px; border-top: 1px solid var(--vscode-widget-border); padding-top: 7px; }
.disclosure summary { color: var(--vscode-textLink-foreground); font-size: 11px; cursor: pointer; }
.history-row { margin-top: 7px; }
.history-label { font-size: 11px; font-weight: 600; }
.history-copy, .technical-value { color: var(--vscode-descriptionForeground); font-size: 10px; white-space: pre-wrap; overflow-wrap: anywhere; }
.technical-grid { display: grid; gap: 7px; margin-top: 8px; }
.technical-label { color: var(--vscode-descriptionForeground); font-size: 10px; font-weight: 600; text-transform: capitalize; }
.result-card .facts { margin-bottom: 4px; }
.file-changes { margin-top: 12px; border: 1px solid var(--vscode-widget-border); border-radius: 9px; overflow: hidden; background: color-mix(in srgb, var(--vscode-sideBar-background) 72%, var(--vscode-input-background)); }
.file-changes-header { display: flex; align-items: center; justify-content: space-between; gap: 10px; padding: 9px 10px; border-bottom: 1px solid var(--vscode-widget-border); }
.file-changes-title { font-size: 11px; font-weight: 650; }
.file-changes-total, .file-change-stat { display: flex; flex: none; gap: 6px; font-variant-numeric: tabular-nums; font-size: 10px; }
.file-change-additions { color: var(--vscode-gitDecoration-addedResourceForeground, var(--vscode-testing-iconPassed)); }
.file-change-deletions { color: var(--vscode-gitDecoration-deletedResourceForeground, var(--vscode-testing-iconFailed)); }
.file-change-row { display: grid; grid-template-columns: 18px minmax(0, 1fr) auto; align-items: center; gap: 7px; width: 100%; min-width: 0; margin: 0; padding: 7px 10px; border: 0; border-top: 1px solid color-mix(in srgb, var(--vscode-widget-border) 55%, transparent); color: inherit; background: transparent; font: inherit; text-align: left; cursor: pointer; }
.file-changes-header + .file-change-row { border-top: 0; }
.file-change-row:hover { background: var(--vscode-list-hoverBackground); }
.file-change-row:focus-visible { outline: 1px solid var(--vscode-focusBorder); outline-offset: -2px; }
.file-change-kind { display: grid; place-items: center; width: 17px; height: 17px; border-radius: 4px; color: var(--vscode-descriptionForeground); background: var(--vscode-badge-background); font-size: 9px; font-weight: 700; }
.file-change-kind.added { color: var(--vscode-gitDecoration-addedResourceForeground, var(--vscode-testing-iconPassed)); }
.file-change-kind.deleted { color: var(--vscode-gitDecoration-deletedResourceForeground, var(--vscode-testing-iconFailed)); }
.file-change-path { min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-size: 10px; }
.file-changes-more { border-top: 1px solid var(--vscode-widget-border); }
.file-changes-more summary { padding: 7px 10px; color: var(--vscode-textLink-foreground); font-size: 10px; cursor: pointer; }
.attention-action { display: flex; flex-wrap: wrap; justify-content: center; gap: 8px; margin-top: 10px; }
.timeline { border-left: 1px solid var(--vscode-widget-border); margin: 14px 0 10px 6px; padding-left: 14px; }
.phase { position: relative; padding: 0 0 14px; }
.phase::before { content: ''; position: absolute; width: 8px; height: 8px; border-radius: 50%; left: -19px; top: 3px; background: var(--vscode-descriptionForeground); }
.phase.approved::before, .phase.succeeded::before, .phase.completed::before { background: var(--vscode-testing-iconPassed); }
.phase.running::before { background: var(--vscode-progressBar-background); }
.phase.failed::before, .phase.blocked::before, .phase.needs_changes::before { background: var(--vscode-testing-iconFailed); }
.phase-name { font-weight: 600; text-transform: capitalize; }
.phase-detail { font-size: 11px; color: var(--vscode-descriptionForeground); margin-top: 2px; }
.notice { border: 1px solid var(--baldr-warning-accent); border-left-width: 4px; background: var(--baldr-surface); padding: 9px; border-radius: 6px; margin: 10px 0; }
.actions { display: flex; flex-wrap: wrap; align-items: center; justify-content: center; gap: 8px; margin-top: 14px; }
.action { display: inline-flex; min-height: 28px; align-items: center; justify-content: center; border: 1px solid var(--vscode-button-border, transparent); border-radius: 6px; padding: 4px 10px; cursor: pointer; background: var(--vscode-button-secondaryBackground); color: var(--vscode-button-secondaryForeground); }
.action:hover { background: var(--vscode-button-secondaryHoverBackground, var(--vscode-toolbar-hoverBackground)); }
.action.primary { background: var(--vscode-button-background); color: var(--vscode-button-foreground); }
.composer { position: relative; padding: 10px 16px 12px; background: var(--vscode-sideBar-background); }
.input-shell { width: 100%; max-width: var(--baldr-content-width); margin: 0 auto; border: 1px solid var(--vscode-input-border, var(--vscode-widget-border)); background: var(--vscode-input-background); border-radius: 16px; padding: 10px 11px 9px; box-shadow: 0 3px 12px rgba(0,0,0,.12); transition: border-color 120ms ease, box-shadow 120ms ease; }
.input-shell:focus-within { border-color: var(--vscode-focusBorder); box-shadow: 0 3px 12px rgba(0,0,0,.12), 0 0 0 1px color-mix(in srgb, var(--vscode-focusBorder) 20%, transparent); }
textarea { display: block; width: 100%; min-height: 44px; max-height: 150px; resize: none; border: 0; outline: none; background: transparent; color: var(--vscode-input-foreground); padding: 3px 4px 8px; line-height: 1.45; }
textarea::placeholder { color: var(--vscode-input-placeholderForeground); opacity: 1; }
.composer-row { display: grid; grid-template-columns: 28px minmax(0, 1fr) 28px; align-items: center; gap: 8px; min-height: 28px; }
.plus, .send { display: inline-grid; place-items: center; border: 0; border-radius: 50%; width: 28px; height: 28px; padding: 0; cursor: pointer; }
.plus { justify-self: start; background: transparent; color: var(--vscode-descriptionForeground); }
.plus:hover { background: var(--vscode-toolbar-hoverBackground); }
.send { justify-self: end; background: var(--vscode-button-background); color: var(--vscode-button-foreground); }
.send:hover:not(:disabled) { background: var(--vscode-button-hoverBackground); }
.send:disabled { opacity: .42; cursor: default; }
.chips { display: flex; min-width: 0; align-items: center; justify-content: center; flex-wrap: nowrap; gap: 4px; overflow: hidden; }
.chip { display: inline-flex; flex: 0 0 auto; min-height: 26px; align-items: center; justify-content: center; gap: 5px; border: 0; border-radius: 6px; padding: 4px 6px; background: transparent; color: var(--vscode-foreground); font-size: 11px; line-height: 16px; cursor: pointer; white-space: nowrap; }
.chip:hover { background: var(--vscode-toolbar-hoverBackground); }
.chip-icon { width: 14px; height: 14px; fill: currentColor; color: var(--vscode-descriptionForeground); }
.attachments { display: flex; flex-wrap: wrap; gap: 8px; padding: 0 3px 9px; }
.attachments:empty { display: none; }
.active-context { min-height: 0; padding: 0 8px 6px; overflow: hidden; color: var(--vscode-descriptionForeground); font-size: 10px; text-overflow: ellipsis; white-space: nowrap; }
.active-context:empty { display: none; }
.attachment { position: relative; display: grid; grid-template-columns: 30px minmax(0, 1fr); align-items: center; width: min(170px, calc(50% - 4px)); min-width: 120px; min-height: 48px; gap: 8px; padding: 7px 24px 7px 8px; border: 1px solid var(--vscode-widget-border); border-radius: 10px; background: color-mix(in srgb, var(--vscode-sideBar-background) 58%, var(--vscode-input-background)); }
.attachment-icon { display: grid; place-items: center; width: 30px; height: 30px; border-radius: 7px; background: var(--vscode-toolbar-hoverBackground); color: var(--vscode-descriptionForeground); font-size: 15px; }
.attachment-label { min-width: 0; overflow: hidden; color: var(--vscode-foreground); font-size: 11px; line-height: 1.3; text-overflow: ellipsis; white-space: nowrap; }
.attachment-kind { color: var(--vscode-descriptionForeground); font-size: 10px; }
.remove-attachment { position: absolute; top: 4px; right: 4px; display: grid; place-items: center; width: 18px; height: 18px; padding: 0; border: 0; border-radius: 50%; background: transparent; color: var(--vscode-descriptionForeground); cursor: pointer; }
.remove-attachment:hover { color: var(--vscode-foreground); background: var(--vscode-toolbar-hoverBackground); }
.plus-menu { position: absolute; left: 50%; right: auto; width: calc(100% - 32px); max-width: var(--baldr-content-width); transform: translateX(-50%); bottom: calc(100% + 8px); display: none; max-height: min(520px, calc(100vh - 150px)); overflow: auto; padding: 10px; border: 1px solid var(--vscode-widget-border); border-radius: 12px; background: var(--vscode-quickInput-background); box-shadow: 0 -3px 18px var(--vscode-widget-shadow); z-index: 20; }
.plus-menu.visible { display: block; }
.plus-menu-heading, .plus-menu-group { color: var(--vscode-descriptionForeground); font-size: 11px; font-weight: 600; margin: 0 3px 5px; }
.plus-menu-group { margin-top: 10px; }
.plus-option { display: grid; grid-template-columns: 22px minmax(0, 1fr); align-items: center; width: 100%; min-height: 42px; gap: 8px; border: 0; border-radius: 7px; padding: 6px 7px; color: inherit; text-align: left; background: transparent; cursor: pointer; }
.plus-option[hidden], .plus-menu-heading[hidden], .plus-menu-group[hidden], .plus-empty[hidden] { display: none; }
.plus-option:hover, .plus-option:focus-visible { background: var(--vscode-list-hoverBackground); }
.plus-option-icon { text-align: center; color: var(--vscode-descriptionForeground); }
.plus-option-label { display: block; }
.plus-option-detail { display: block; margin-top: 1px; color: var(--vscode-descriptionForeground); font-size: 11px; }
.plus-empty { padding: 13px 8px 5px; color: var(--vscode-descriptionForeground); text-align: center; font-size: 11px; }
.plus-filter { width: 100%; margin-top: 10px; border: 1px solid var(--vscode-input-border, transparent); border-radius: 6px; outline: 0; padding: 7px 9px; color: var(--vscode-input-foreground); background: var(--vscode-input-background); }
.plus-filter:focus { border-color: var(--vscode-focusBorder); }
.slash { position: absolute; left: 50%; right: auto; width: calc(100% - 32px); max-width: var(--baldr-content-width); transform: translateX(-50%); bottom: 77px; max-height: 220px; overflow: auto; border: 1px solid var(--vscode-widget-border); background: var(--vscode-quickInput-background); box-shadow: 0 5px 18px var(--vscode-widget-shadow); border-radius: 7px; z-index: 10; display: none; }
.slash.visible { display: block; }
.slash-item { padding: 7px 9px; cursor: pointer; display: flex; gap: 8px; }
.slash-item:hover, .slash-item.active { background: var(--vscode-list-activeSelectionBackground); color: var(--vscode-list-activeSelectionForeground); }
.slash-command { min-width: 75px; font-weight: 600; }
.slash-description { opacity: .8; }
.loading { position: absolute; inset: 0; pointer-events: none; background: linear-gradient(90deg, transparent, color-mix(in srgb, var(--vscode-progressBar-background) 16%, transparent), transparent); background-size: 200% 100%; animation: shimmer 1.2s infinite; opacity: 0; }
.loading.visible { opacity: 1; }
@keyframes shimmer { to { background-position: -200% 0; } }
@media (max-width: 560px) {
  .chip[data-chip="roles"] { display: none; }
}
@media (max-width: 420px) {
  .header, .composer { padding-left: 10px; padding-right: 10px; }
  .plus-menu, .slash { width: calc(100% - 20px); }
  .composer-row { gap: 5px; }
  .chip { padding-left: 5px; padding-right: 5px; }
  .chip[data-chip="context"] { display: none; }
}
@media (max-width: 300px) {
  .chip[data-chip="preset"] { display: none; }
  .content { padding-left: 10px; padding-right: 10px; }
  .now-card, .result-card, .attention-card { padding: 10px; }
  .stage-toggle { grid-template-columns: 18px minmax(0, 1fr) auto; padding: 9px 8px; }
  .stage-body { padding-left: 33px; padding-right: 8px; }
  .stage-strip { gap: 3px; }
}
@media (prefers-reduced-motion: reduce) {
  .loading { animation: none; }
  .input-shell { transition: none; }
  .stage-chevron { transition: none; }
  .history-toggle-icon { transition: none; }
}
@media (forced-colors: active) {
  .now-card, .result-card, .attention-card, .stage-card, .refresh-error, .deliverable-panel, .session-section { border: 1px solid CanvasText; }
  .stage-strip-item.active, .stage-strip-item.complete { border-color: Highlight; }
  .action.primary, .send { border: 1px solid ButtonText; }
}
</style>
</head>
<body>
<div id="root">
  <div class="sr-only" id="liveStatus" role="status" aria-live="polite" aria-atomic="true"></div>
  <section class="header" id="header">
    <div class="header-row">
      <div class="heading">Tus sesiones</div>
      <div class="header-actions">
        <button type="button" class="icon-button history-toggle" id="historyToggle" title="Ocultar historial" aria-label="Ocultar historial de sesiones" aria-expanded="true" aria-controls="historyPanel"><svg class="button-icon history-toggle-icon" viewBox="0 0 16 16" aria-hidden="true"><path fill="none" stroke="currentColor" stroke-width="1.25" stroke-linecap="round" stroke-linejoin="round" d="m4.5 6 3.5 3.5L11.5 6"/></svg></button>
        <button type="button" class="icon-button" id="refresh" title="Actualizar" aria-label="Actualizar sesiones"><svg class="button-icon" viewBox="0 0 16 16" aria-hidden="true"><path fill="none" stroke="currentColor" stroke-width="1.2" stroke-linecap="round" stroke-linejoin="round" d="M13 8a5 5 0 1 1-1.46-3.54L13 5.92M13 3v3h-3"/></svg></button>
        <button type="button" class="icon-button" id="configure" title="Opciones de Baldr" aria-label="Opciones de Baldr"><svg class="button-icon" viewBox="0 0 16 16" aria-hidden="true"><path fill="none" stroke="currentColor" stroke-width="1.15" stroke-linejoin="round" d="M6.8 2h2.4l.42 1.55 1.25.72 1.55-.42 1.2 2.08-1.13 1.13v1.45l1.13 1.13-1.2 2.08-1.55-.42-1.25.72L9.2 13.6H6.8l-.42-1.58-1.25-.72-1.55.42-1.2-2.08 1.13-1.13V7.06L2.38 5.93l1.2-2.08 1.55.42 1.25-.72L6.8 2Z"/><circle cx="8" cy="7.79" r="2.15" fill="none" stroke="currentColor" stroke-width="1.15"/></svg></button>
      </div>
    </div>
    <div class="history-panel" id="historyPanel">
      <div class="history-controls" role="group" aria-label="Filtrar historial de sesiones">
        <button type="button" class="history-filter" data-history-filter="active" data-history-label="Activas" aria-pressed="true">Activas</button>
        <button type="button" class="history-filter" data-history-filter="completed" data-history-label="Finalizadas" aria-pressed="false">Finalizadas</button>
        <button type="button" class="history-filter" data-history-filter="archived" data-history-label="Archivadas" aria-pressed="false">Archivadas</button>
      </div>
      <input class="history-search" id="historySearch" type="search" placeholder="Buscar sesiones" aria-label="Buscar sesiones" aria-keyshortcuts="Control+F Meta+F">
      <div class="sr-only" id="historyStatus" role="status" aria-live="polite" aria-atomic="true"></div>
      <div class="task-list" id="tasks" role="region" aria-label="Sesiones"></div>
    </div>
  </section>
  <main class="content" id="content"><div class="loading" id="loading"></div></main>
  <section class="composer" id="composer">
    <div class="input-shell">
      <div class="attachments" id="attachments"></div>
      <div class="active-context" id="activeContext"></div>
      <textarea id="input" rows="2" placeholder="Escribí qué necesitás…" aria-label="Nuevo pedido para Baldr"></textarea>
      <div class="composer-row">
        <button type="button" class="plus" id="plus" title="Agregar archivos u opciones" aria-label="Agregar archivos u opciones" aria-expanded="false" aria-controls="plusMenu"><svg class="button-icon" viewBox="0 0 16 16" aria-hidden="true"><path fill="none" stroke="currentColor" stroke-width="1.35" stroke-linecap="round" d="M8 3v10M3 8h10"/></svg></button>
        <div class="chips" aria-label="Opciones de la sesión">
          <button type="button" class="chip" data-chip="git" id="gitChip"><svg class="chip-icon" viewBox="0 0 16 16" aria-hidden="true"><path fill="none" stroke="currentColor" stroke-width="1.15" stroke-linejoin="round" d="M8 1.8 12.8 3.4v3.45c0 3.15-1.9 5.65-4.8 7.35-2.9-1.7-4.8-4.2-4.8-7.35V3.4L8 1.8Z"/></svg><span id="gitChipLabel">Trabajar directamente</span></button>
          <button type="button" class="chip" data-chip="preset" id="presetChip"><svg class="chip-icon" viewBox="0 0 16 16" aria-hidden="true"><path fill="none" stroke="currentColor" stroke-width="1.15" stroke-linecap="round" d="M3 4h10M3 8h10M3 12h10M6 2.8v2.4M10 6.8v2.4M7 10.8v2.4"/></svg><span id="presetChipLabel">Estándar</span></button>
          <button type="button" class="chip" data-chip="roles" id="rolesChip"><svg class="chip-icon" viewBox="0 0 16 16" aria-hidden="true"><path fill="none" stroke="currentColor" stroke-width="1.15" stroke-linecap="round" stroke-linejoin="round" d="M6.2 7a2.2 2.2 0 1 0 0-4.4A2.2 2.2 0 0 0 6.2 7ZM2.5 13.2c.25-2.55 1.5-3.8 3.7-3.8s3.45 1.25 3.7 3.8M10.2 3.2a2 2 0 0 1 0 3.8M10.9 9.5c1.55.25 2.4 1.45 2.6 3.4"/></svg><span id="rolesChipLabel">Equipo estándar</span></button>
          <button type="button" class="chip" data-chip="context" id="contextChip"><svg class="chip-icon" viewBox="0 0 16 16" aria-hidden="true"><path fill="none" stroke="currentColor" stroke-width="1.15" stroke-linecap="round" stroke-linejoin="round" d="M8 1.8c.35 2.65 1.55 3.85 4.2 4.2C9.55 6.35 8.35 7.55 8 10.2 7.65 7.55 6.45 6.35 3.8 6 6.45 5.65 7.65 4.45 8 1.8ZM12.2 10.2c.18 1.35.85 2.02 2.2 2.2-1.35.18-2.02.85-2.2 2.2-.18-1.35-.85-2.02-2.2-2.2 1.35-.18 2.02-.85 2.2-2.2Z"/></svg><span id="contextChipLabel">Ayuda automática</span></button>
        </div>
        <button type="button" class="send" id="send" title="Enviar pedido" aria-label="Enviar pedido" disabled><svg class="button-icon" viewBox="0 0 16 16" aria-hidden="true"><path fill="none" stroke="currentColor" stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round" d="M8 12.8V3.2M4.4 6.8 8 3.2l3.6 3.6"/></svg></button>
      </div>
    </div>
    <div class="slash" id="slash"></div>
    <div class="plus-menu" id="plusMenu" role="dialog" aria-label="Agregar detalles u opciones de Baldr" aria-hidden="true">
      <div class="plus-menu-heading" data-plus-heading="add">Agregar</div>
      <button type="button" class="plus-option" data-plus-action="path" data-plus-group="add"><span class="plus-option-icon">⌕</span><span><span class="plus-option-label">Archivos y carpetas</span><span class="plus-option-detail">Sumá material útil para el pedido</span></span></button>
      <button type="button" class="plus-option" data-plus-action="workspace" data-plus-group="add"><span class="plus-option-icon">◇</span><span><span class="plus-option-label">Carpeta de trabajo</span><span class="plus-option-detail">Elegí el proyecto activo</span></span></button>
      <button type="button" class="plus-option" data-plus-action="file" data-plus-group="add"><span class="plus-option-icon">▣</span><span><span class="plus-option-label">Archivo abierto</span><span class="plus-option-detail">Usalo como referencia</span></span></button>
      <button type="button" class="plus-option" data-plus-action="selection" data-plus-group="add"><span class="plus-option-icon">≡</span><span><span class="plus-option-label">Texto seleccionado</span><span class="plus-option-detail">Sumá solo la parte marcada</span></span></button>
      <button type="button" class="plus-option" data-plus-action="draft" data-plus-group="add"><span class="plus-option-icon">＋</span><span><span class="plus-option-label">Guardar para después</span><span class="plus-option-detail">Creá una sesión sin empezarla todavía</span></span></button>
      <div class="plus-menu-group" data-plus-heading="preferences">Preferencias</div>
      <button type="button" class="plus-option" data-plus-action="git" data-plus-group="preferences"><span class="plus-option-icon">⌘</span><span><span class="plus-option-label">Protección de cambios</span><span class="plus-option-detail">Elegí cómo guardar y recuperar el trabajo</span></span></button>
      <button type="button" class="plus-option" data-plus-action="preset" data-plus-group="preferences"><span class="plus-option-icon">◈</span><span><span class="plus-option-label">Nivel de detalle</span><span class="plus-option-detail">Rápido, estándar o detallado</span></span></button>
      <button type="button" class="plus-option" data-plus-action="roles" data-plus-group="preferences"><span class="plus-option-icon">◌</span><span><span class="plus-option-label">Equipo de Baldr</span><span class="plus-option-detail">Elegí modelos y cómo se reparte el trabajo</span></span></button>
      <button type="button" class="plus-option" data-plus-action="context" data-plus-group="preferences"><span class="plus-option-icon">?</span><span><span class="plus-option-label">Ayuda adicional</span><span class="plus-option-detail">Buscá información útil cuando haga falta</span></span></button>
      <button type="button" class="plus-option" data-plus-action="qualification" data-plus-group="preferences"><span class="plus-option-icon">✓</span><span><span class="plus-option-label">Calificar VS Code + Codex</span><span class="plus-option-detail">Ejecutá los gates reales y abrí la evidencia pendiente</span></span></button>
      <div class="plus-empty" id="plusEmpty" hidden>No encontramos una opción con ese nombre.</div>
      <input class="plus-filter" id="plusFilter" type="search" placeholder="Buscar opciones" aria-label="Buscar opciones de Baldr">
    </div>
  </section>
</div>
<script nonce="${nonce}">
const vscode = acquireVsCodeApi();
const els = Object.fromEntries(['header','historyPanel','historyToggle','historyStatus','tasks','content','composer','input','send','plus','configure','refresh','gitChip','gitChipLabel','presetChip','presetChipLabel','rolesChip','rolesChipLabel','contextChip','contextChipLabel','attachments','activeContext','slash','plusMenu','plusFilter','plusEmpty','historySearch','loading','liveStatus'].map(id => [id, document.getElementById(id)]));
const persistedView = vscode.getState() || {};
let expandedByItem = persistedView.expandedByItem || {};
let activeStageByItem = persistedView.activeStageByItem || {};
let openDisclosuresByItem = persistedView.openDisclosuresByItem || {};
let historyFilter=['active','completed','archived'].includes(persistedView.historyFilter)?persistedView.historyFilter:'active';let historySearch=String(persistedView.historySearch||'');let historyExpanded=persistedView.historyExpanded!==false;let draftText=String(persistedView.draftText||'');let openTaskMenuId='';let state = {}; let slashIndex = 0; let plusMenuOpen = false; let lastContentKey = ''; let lastTasksKey = ''; let lastPendingKey = ''; let lastAnnouncement = '';let deliverableView={open:false,loading:false,error:'',descriptor:null,data:null,itemId:'',descriptorDigest:'',requestId:0};let deliverableRequestSequence=0;let deliverableIndexView={itemId:'',initialized:false,loading:false,error:'',sourceCursor:'',nextCursor:'',requestCursor:'',requestId:0,items:[]};let deliverableIndexRequestSequence=0;let pendingFocusKey='';let deliverableReturnFocusKey='';let deliverableScrollTop=0;let deliverableTechnicalOpen=false;
const escapeHtml = value => String(value ?? '').replace(/[&<>"']/g, ch => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[ch]));
const post = message => vscode.postMessage(message);
const workbench = () => state.workbench || {};
const selected = () => workbench().selected || null;
const commands = () => ((workbench().options || {}).slash_commands || [
 {id:'setup',usage:'/setup',description:'Abrir las opciones de Baldr.'},{id:'new',usage:'/new <tarea>',description:'Guardar una sesión para después.'},{id:'run',usage:'/run [tarea]',description:'Empezar la sesión seleccionada.'},{id:'status',usage:'/status',description:'Actualizar el estado.'},{id:'profile',usage:'/profile <nivel>',description:'Cambiar el nivel de detalle.'},{id:'git',usage:'/git <modo>',description:'Cambiar la protección de cambios.'},{id:'context',usage:'/context <modo>',description:'Configurar la ayuda adicional.'},{id:'roles',usage:'/roles',description:'Configurar el equipo de Baldr.'},{id:'cancel',usage:'/cancel',description:'Cancelar la sesión seleccionada.'},{id:'resume',usage:'/resume',description:'Continuar una sesión interrumpida.'},{id:'archive',usage:'/archive',description:'Archivar la sesión seleccionada.'},{id:'restore',usage:'/restore',description:'Restaurar la sesión archivada seleccionada.'},{id:'delete',usage:'/delete',description:'Eliminar permanentemente la sesión archivada seleccionada.'},{id:'help',usage:'/help',description:'Ver los comandos disponibles.'}
]);
function statusClass(status){ return String(status || 'draft').replace(/[^a-z_]/g,'_'); }
function statusLabel(status){ const labels={draft:'Pendiente',queued:'En espera',running:'En curso',cancelling:'Cancelando',completed:'Lista',archived:'Archivada',failed:'Necesita atención',cancelled:'Cancelada',needs_attention:'Necesita atención'}; return labels[String(status || 'draft')] || String(status || 'Pendiente'); }
function presetLabel(value){ return ({fast:'Rápido',balanced:'Estándar',deep:'Detallado',custom:'A medida'}[value]||'Estándar'); }
function safetyLabel(value){ return ({automatic:'Pedir autorización',worktree:'Copia aislada',current:'Trabajar directamente','non-git':'Sin protección'}[value]||'Trabajar directamente'); }
function contextLabel(value){ return ({auto:'Ayuda automática',on:'Ayuda activa',off:'Ayuda desactivada'}[value]||'Ayuda automática'); }
function itemErrorMessage(item){ if(item.error_code==='workspace_reconciliation_required'&&item.safety_mode==='non-git')return 'La sesión se detuvo al intentar crear un respaldo Git que esta carpeta no usa. Tus archivos siguen en la carpeta: revisá las opciones para continuar con ellos.'; return item.error_reason||item.error_code||''; }
function phaseLabel(value){ return ({architecture:'Planificación',architect:'Planificación',implementation:'Ejecución',implementer:'Ejecución',review:'Revisión',reviewer:'Revisión'}[String(value||'').toLowerCase()]||String(value||'Etapa')); }
function emptyMark(){ return '<div class="empty-mark"><svg class="empty-logo" viewBox="0 0 24 24" aria-hidden="true"><path d="M6 20V5.5h6.1c3 0 5 1.5 5 4 0 1.8-1 3-2.5 3.6 2 .5 3.4 1.8 3.4 3.8 0 2.4-2 3.1-5.4 3.1H6Z"/><path d="M9.3 8.5h2.8c1.2 0 1.8.4 1.8 1.2s-.6 1.3-1.8 1.3H9.3V8.5Zm0 5.5h3.3c1.4 0 2.1.5 2.1 1.4 0 1-.7 1.5-2.1 1.5H9.3V14Z"/></svg></div>'; }
function historyGroup(item){const status=String(item?.status||'draft');if(status==='archived')return 'archived';if(['completed','failed','cancelled'].includes(status))return 'completed';return 'active';}
function isHistoryItem(item){return historyGroup(item)===historyFilter;}
function historyActionLabel(action){return ({archive:'Archivar',restore:'Restaurar',delete:'Eliminar'}[action]||'');}
function formatSessionWhen(value){if(!value)return '';const date=new Date(value);if(Number.isNaN(date.getTime()))return '';const elapsed=Math.max(0,Date.now()-date.getTime());const minute=60*1000;const hour=60*minute;const day=24*hour;if(elapsed<minute)return 'Ahora';if(elapsed<hour)return 'Hace '+Math.floor(elapsed/minute)+' min';if(elapsed<day)return 'Hace '+Math.floor(elapsed/hour)+' h';if(elapsed<2*day)return 'Ayer';return date.toLocaleDateString('es',{day:'2-digit',month:'short'});}
function sessionListSummary(item){const summary=item?.progress_summary||{};return String(summary.activity||statusLabel(item?.status)||'');}
function renderHistoryControls(visibleCount){if(els.historySearch&&els.historySearch.value!==historySearch)els.historySearch.value=historySearch;const counts={active:0,completed:0,archived:0};(workbench().items||[]).forEach(item=>{counts[historyGroup(item)]+=1;});document.querySelectorAll('[data-history-filter]').forEach(node=>{const filter=node.dataset.historyFilter;node.setAttribute('aria-pressed',String(filter===historyFilter));node.textContent=(node.dataset.historyLabel||'')+' '+String(counts[filter]||0);});if(Number.isInteger(visibleCount)){const label=visibleCount===1?'1 sesión visible':String(visibleCount)+' sesiones visibles';els.historyStatus.textContent=historySearch.trim()?label+' para la búsqueda actual':label;}}
function renderHistoryVisibility(){els.historyPanel.hidden=!historyExpanded;els.historyToggle.setAttribute('aria-expanded',String(historyExpanded));const label=historyExpanded?'Ocultar historial de sesiones':'Mostrar historial de sesiones';els.historyToggle.setAttribute('aria-label',label);els.historyToggle.title=historyExpanded?'Ocultar historial':'Mostrar historial';}
function setHistoryExpanded(expanded){historyExpanded=Boolean(expanded);openTaskMenuId='';renderHistoryVisibility();saveViewState();}
function focusHistorySearch(){if(!historyExpanded)setHistoryExpanded(true);els.historySearch.focus();els.historySearch.select?.();}
function historyItemNodes(){return [...els.tasks.querySelectorAll('[data-item]')];}
function moveHistoryFocus(event,node){if(!['ArrowDown','ArrowUp','Home','End'].includes(event.key))return;const nodes=historyItemNodes();if(!nodes.length)return;event.preventDefault();const index=nodes.indexOf(node);const next=event.key==='Home'?nodes[0]:event.key==='End'?nodes[nodes.length-1]:nodes[(Math.max(0,index)+(event.key==='ArrowDown'?1:-1)+nodes.length)%nodes.length];next?.focus();}
function renderTasks(){
 const allItems = workbench().items || []; const query=normalizeSearch(historySearch.trim());const items=allItems.filter(item=>{const searchable=[item.title,item.task,sessionListSummary(item)].filter(Boolean).join(' ');return isHistoryItem(item)&&(!query||normalizeSearch(searchable).includes(query));});const current = selected()?.id;
 const key=[historyFilter,query,openTaskMenuId,allItems.length,current||'',items.map(item=>[item.id,item.title||item.task,item.status,item.updated_at,sessionListSummary(item),(item.allowed_actions||[]).join(',')].join(':')).join('|')].join('|');if(key===lastTasksKey){renderHistoryControls(items.length);return;}lastTasksKey=key;
 const previousScroll=els.tasks.scrollTop;const focusedItem=els.tasks.contains(document.activeElement)?String(document.activeElement?.dataset?.item||''):'';
 const rows = items.map(item => {const id=String(item.id||'');const title=String(item.title||item.task||'Sin título');const summary=sessionListSummary(item);const when=formatSessionWhen(item?.progress_summary?.last_event_at||item.updated_at);const lifecycle=(item.allowed_actions||[]).filter(action=>['archive','restore','delete'].includes(action));const menuOpen=openTaskMenuId===id;const actions=menuOpen&&lifecycle.length?'<div class="task-actions" role="group" aria-label="Acciones de '+escapeHtml(title)+'">'+lifecycle.map(action=>'<button type="button" class="task-action '+(action==='delete'?'danger':'')+'" data-history-action="'+escapeHtml(action)+'" data-item-id="'+escapeHtml(id)+'">'+historyActionLabel(action)+'</button>').join('')+'</div>':'';const accessible=[title,statusLabel(item.status),summary,when].filter(Boolean).join('. ');return '<div class="task-wrap" role="listitem"><button class="task '+(item.id===current?'selected':'')+'" data-item="'+escapeHtml(id)+'" aria-label="'+escapeHtml(accessible)+'"'+(item.id===current?' aria-current="true"':'')+'><span class="dot '+statusClass(item.status)+'" aria-hidden="true"></span><span class="task-main"><span class="task-title">'+escapeHtml(title)+'</span>'+(summary?'<span class="task-summary">'+escapeHtml(summary)+'</span>':'')+'<span class="task-meta"><span class="task-meta-status">'+escapeHtml(statusLabel(item.status))+'</span>'+(when?'<span aria-hidden="true">·</span><span class="task-meta-time">'+escapeHtml(when)+'</span>':'')+'</span></span></button>'+(lifecycle.length?'<button type="button" class="task-menu" data-item-menu="'+escapeHtml(id)+'" aria-label="Opciones de '+escapeHtml(title)+'" aria-expanded="'+String(menuOpen)+'">⋯</button>':'')+actions+'</div>';}).join('');
 const empty=historyFilter==='archived'?'No hay sesiones archivadas.':historyFilter==='completed'?'No hay sesiones finalizadas.':'No hay sesiones activas.';
 els.tasks.innerHTML = rows?'<div role="list">'+rows+'</div>':allItems.length?(query?'<div class="task-empty" role="status"><span>No encontramos sesiones.</span><button type="button" class="task-empty-action" data-clear-history>Limpiar búsqueda</button></div>':'<div class="task-empty" role="status"><span>'+empty+'</span></div>'):'<div class="task-empty" role="status"><span>Todavía no hay sesiones</span></div>';
 els.tasks.querySelectorAll('[data-item]').forEach(node => {node.addEventListener('click', () => post({type:'select', itemId:node.dataset.item}));node.addEventListener('keydown',event=>moveHistoryFocus(event,node));});
 els.tasks.querySelectorAll('[data-item-menu]').forEach(node=>node.addEventListener('click',()=>{openTaskMenuId=openTaskMenuId===node.dataset.itemMenu?'':String(node.dataset.itemMenu||'');renderTasks();}));
 els.tasks.querySelectorAll('[data-history-action]').forEach(node=>node.addEventListener('click',()=>{openTaskMenuId='';post({type:'itemAction',action:node.dataset.historyAction,itemId:node.dataset.itemId});}));
 els.tasks.querySelector('[data-clear-history]')?.addEventListener('click',()=>{historySearch='';saveViewState();renderTasks();focusHistorySearch();});
 renderHistoryControls(items.length);els.tasks.scrollTop=previousScroll;if(focusedItem){const focusTarget=[...els.tasks.querySelectorAll('[data-item]')].find(node=>node.dataset.item===focusedItem);focusTarget?.focus({preventScroll:true});}
}
function stageIcon(stageState){ return stageState==='complete'?'✓':stageState==='active'?'●':stageState==='attention'?'!':stageState==='cancelled'?'×':stageState==='skipped'?'—':'○'; }
function formatWhen(value){ if(!value)return '';const date=new Date(value);if(Number.isNaN(date.getTime()))return '';return 'Actualizado a las '+date.toLocaleTimeString('es',{hour:'2-digit',minute:'2-digit'}); }
function factsHtml(facts){ return (facts||[]).length?'<div class="facts">'+facts.map(fact=>'<span class="fact '+escapeHtml(fact.tone||'neutral')+'">'+escapeHtml(fact.label)+'</span>').join('')+'</div>':''; }
function sectionsHtml(sections){ return (sections||[]).map(section=>'<section class="report-section '+escapeHtml(section.tone||'neutral')+'"><h4 class="report-section-title">'+escapeHtml(section.title)+'</h4><ul>'+((section.items||[]).map(item=>'<li>'+escapeHtml(item)+'</li>').join(''))+'</ul></section>').join(''); }
function fileChangeStats(change){const additions=Number.isInteger(change?.additions)?change.additions:null;const deletions=Number.isInteger(change?.deletions)?change.deletions:null;return additions===null||deletions===null?'':'<span class="file-change-stat"><span class="file-change-additions">+'+escapeHtml(additions)+'</span><span class="file-change-deletions">-'+escapeHtml(deletions)+'</span></span>';}
function fileChangeRow(change){const kind=String(change?.kind||'modified');const marker=({added:'A',modified:'M',deleted:'D'}[kind]||'M');const filePath=String(change?.path||'');return '<button type="button" class="file-change-row" data-open-changed-file="'+escapeHtml(filePath)+'" aria-label="'+escapeHtml('Abrir '+filePath)+'"><span class="file-change-kind '+escapeHtml(kind)+'" aria-label="'+escapeHtml(({added:'Agregado',modified:'Modificado',deleted:'Eliminado'}[kind]||'Modificado')+'')+'">'+marker+'</span><span class="file-change-path" title="'+escapeHtml(filePath)+'">'+escapeHtml(filePath)+'</span>'+fileChangeStats(change)+'</button>';}
function fileChangesHtml(changes){const values=Array.isArray(changes)?changes:[];if(!values.length)return '';const complete=values.every(change=>Number.isInteger(change?.additions)&&Number.isInteger(change?.deletions));const additions=complete?values.reduce((total,change)=>total+change.additions,0):null;const deletions=complete?values.reduce((total,change)=>total+change.deletions,0):null;const title=values.length===1?'1 archivo cambiado':values.length+' archivos cambiados';const totals=complete?'<span class="file-changes-total"><span class="file-change-additions">+'+escapeHtml(additions)+'</span><span class="file-change-deletions">-'+escapeHtml(deletions)+'</span></span>':'';const visible=values.slice(0,5).map(fileChangeRow).join('');const remaining=values.slice(5);const more=remaining.length?'<details class="file-changes-more"><summary>Ver '+remaining.length+' '+(remaining.length===1?'archivo más':'archivos más')+'</summary>'+remaining.map(fileChangeRow).join('')+'</details>':'';return '<section class="file-changes" aria-label="Cambios en archivos"><div class="file-changes-header"><span class="file-changes-title">'+escapeHtml(title)+'</span>'+totals+'</div>'+visible+more+'</section>';}
function technicalRowsHtml(rows){ return (rows||[]).map(row=>'<div><div class="technical-label">'+escapeHtml(row.label)+'</div><div class="technical-value">'+escapeHtml(row.value)+'</div></div>').join(''); }
function milestoneIcon(entry){return entry?.state==='complete'&&entry?.evidence==='verified'?'✓':entry?.state==='attention'?'!':entry?.state==='cancelled'?'×':'•';}
function evidenceLabel(value){return value==='verified'?'Comprobado por Baldr':value==='reported'?'Informado por el equipo':'Registrado por Baldr';}
function stageMilestonesHtml(stage){const values=stage.milestones||[];if(!values.length)return '';return '<section class="stage-milestones" aria-label="Hitos de '+escapeHtml(stage.title)+'"><h4 class="stage-milestones-title">Hitos</h4><ol class="stage-milestones-list">'+values.map(entry=>'<li class="stage-milestone"><span aria-hidden="true">'+milestoneIcon(entry)+'</span><span class="stage-milestone-copy">'+escapeHtml(entry.label)+'<span class="stage-milestone-evidence">'+escapeHtml(evidenceLabel(entry.evidence))+'</span></span></li>').join('')+'</ol></section>';}
		function deliverableRunOrdinal(entry){return Number(entry?.runOrdinal??entry?.run_ordinal??0);}
		function deliverableLabel(entry){const parts=[];const runOrdinal=deliverableRunOrdinal(entry);if(runOrdinal>1)parts.push('Intento '+runOrdinal);parts.push('Ronda '+(Number(entry?.round||0)+1));return parts.join(' · ');}
		function deliverablePreview(entry){const value=entry?.preview;return value&&typeof value==='object'?String(value.summary||''):String(value||'');}
		function deliverableDescriptorKey(entry){return [String(entry?.stage||''),Number(entry?.round||0),deliverableRunOrdinal(entry)].join('|');}
		function stageDeliverableValues(stage){const values=[...(stage.deliverables||[])];if(deliverableIndexView.itemId===String(selected()?.id||''))values.push(...deliverableIndexView.items.filter(entry=>entry.stage===stage.id));const unique=new Map();values.forEach(entry=>unique.set(deliverableDescriptorKey(entry),entry));return [...unique.values()].sort((left,right)=>deliverableRunOrdinal(left)-deliverableRunOrdinal(right)||Number(left.round||0)-Number(right.round||0));}
		function stageDeliverablesHtml(stage){const values=stageDeliverableValues(stage);if(!values.length)return '';return '<section class="stage-deliverables" aria-label="Entregas de '+escapeHtml(stage.title)+'"><h4 class="stage-deliverables-title">Entregas</h4>'+values.map(entry=>{const key='deliverable-'+stage.id+'-'+entry.runOrdinal+'-'+entry.round;const preview=deliverablePreview(entry);const label=entry.availability==='available'?'Ver entrega completa':entry.availability==='summary_only'?(preview?'Ver resumen disponible':'Ver información disponible'):'No disponible';return '<div class="stage-deliverable-row"><span class="stage-deliverable-copy"><span class="stage-deliverable-name">'+escapeHtml(deliverableLabel(entry))+'</span>'+(preview?'<span class="stage-deliverable-preview">'+escapeHtml(preview)+'</span>':'')+(entry.reason?'<span class="stage-deliverable-note">'+escapeHtml(entry.reason)+'</span>':'')+'</span>'+(entry.availability==='unavailable'?'<span class="stage-deliverable-status">'+label+'</span>':'<button type="button" class="action" data-deliverable-stage="'+escapeHtml(stage.id)+'" data-deliverable-round="'+escapeHtml(entry.round)+'" data-deliverable-run="'+escapeHtml(entry.runOrdinal)+'" data-focus-key="'+escapeHtml(key)+'">'+label+'</button>')+'</div>';}).join('')+'</section>';}
		function deliverableIndexHtml(presentation){const index=presentation?.deliverableIndex||{};if(index.truncated!==true)return '';if(deliverableIndexView.error)return '<section class="deliverable-index" role="alert"><span class="deliverable-index-note">'+escapeHtml(deliverableIndexView.error)+'</span><button type="button" class="action" data-deliverable-index data-focus-key="deliverable-index">Reintentar entregas anteriores</button></section>';if(deliverableIndexView.loading)return '<section class="deliverable-index" role="status" aria-live="polite"><button type="button" class="action" data-focus-key="deliverable-index" disabled>Cargando entregas anteriores…</button></section>';if(!deliverableIndexView.nextCursor)return '<section class="deliverable-index"><span class="deliverable-index-note">Ya estás viendo todas las entregas de esta sesión.</span></section>';return '<section class="deliverable-index"><button type="button" class="action" data-deliverable-index data-focus-key="deliverable-index">Ver entregas anteriores</button>'+(Number(index.total)>0?'<span class="deliverable-index-note">Hay '+escapeHtml(index.total)+' entregas guardadas.</span>':'')+'</section>';}
function deliverableSectionTitle(value){return ({summary:'Resumen',interpretation:'Lo que entendió Baldr',scope:'Alcance',approach:'Enfoque',plan_steps:'Plan acordado',decisions:'Decisiones',acceptance_criteria:'Cómo sabremos que está listo',assumptions:'Supuestos',work_completed:'Trabajo completado',work_next:'Qué sigue',changes_added:'Qué agregó',changes_modified:'Qué modificó',changes_removed:'Qué quitó',files_added:'Archivos agregados',files_modified:'Archivos modificados',files_deleted:'Archivos eliminados',tests_run:'Comprobaciones informadas',verification_evidence:'Evidencia de comprobación',verification_needed:'Qué falta comprobar',findings:'Hallazgos',corrections:'Correcciones',risks:'A tener en cuenta',follow_up:'Próximos pasos',blockers:'Qué impide avanzar',review_decision:'Veredicto',commands_run:'Comandos ejecutados',constraints:'Límites considerados',alternatives_rejected:'Opciones descartadas'}[value]||String(value||'Detalle').replace(/_/g,' '));}
	function deliverableEntryValue(entry){const value=entry?.value;if(value&&typeof value==='object'&&!Array.isArray(value)){const key=String(value.key??value.title??'');const detail=String(value.value??value.detail??'');return key&&detail?key+': '+detail:key||detail;}return String(value??'');}
	function deliverableEntriesHtml(entries){const visible=(entries||[]).filter(entry=>!entry.technical);const technical=(entries||[]).filter(entry=>entry.technical);const groups=values=>{const by={};values.forEach(entry=>{const key=String(entry.section||'summary');(by[key]||(by[key]=[])).push(entry);});return Object.entries(by).map(([key,items])=>'<section class="deliverable-section"><h4 class="deliverable-section-title">'+escapeHtml(deliverableSectionTitle(key))+'</h4><ul class="deliverable-entries">'+items.map(entry=>'<li class="deliverable-entry">'+escapeHtml(deliverableEntryValue(entry))+'</li>').join('')+'</ul></section>').join('');};return groups(visible)+(technical.length?'<details class="disclosure" data-deliverable-technical'+(deliverableTechnicalOpen?' open':'')+'><summary data-focus-key="deliverable-technical">Detalles técnicos de la entrega</summary>'+groups(technical)+'</details>':'');}
		function deliverableViewHtml(){if(!deliverableView.open)return '';const data=deliverableView.data||{};const descriptor=deliverableView.descriptor||data;const page=data.page||{};const title=({planning:'Plan completo',execution:'Entrega de ejecución',review:'Informe de revisión'}[descriptor.stage]||'Entrega completa');const preview=deliverablePreview(data)||deliverablePreview(descriptor);const safeReason=descriptor.reason||'Esta entrega completa no está disponible.';const body=deliverableView.error?'<div class="refresh-error" role="alert"><div class="refresh-error-title">No pudimos abrir la entrega</div><p class="refresh-error-copy">'+escapeHtml(deliverableView.error)+'</p><button type="button" class="action" data-deliverable-retry data-focus-key="deliverable-retry">Reintentar</button></div>':deliverableView.loading&&!deliverableView.data?'<div role="status" aria-live="polite">Cargando la entrega…</div>':data.availability==='unavailable'?'<p>'+escapeHtml(safeReason)+'</p>':data.availability==='summary_only'?'<p class="deliverable-note">'+escapeHtml(safeReason)+'</p>'+(preview?'<section class="deliverable-section"><h4 class="deliverable-section-title">Resumen disponible</h4><p>'+escapeHtml(preview)+'</p></section>':'<p>No se conservaron más detalles de esta entrega.</p>'):'<p class="deliverable-note">Contenido estructurado y protegido. No incluye prompts, razonamiento ni salida cruda del proveedor.</p>'+((page.entries||[]).length?deliverableEntriesHtml(page.entries||[]):'<p>Esta entrega no agregó detalles adicionales.</p>');return '<section class="deliverable-panel" role="dialog" aria-modal="true" aria-labelledby="deliverable-title"><div class="deliverable-header"><div><h3 class="deliverable-title" id="deliverable-title">'+escapeHtml(title)+'</h3><div class="deliverable-subtitle">'+escapeHtml(deliverableLabel(descriptor))+'</div></div><button type="button" class="icon-button" data-deliverable-close data-focus-key="deliverable-close" aria-label="Cerrar entrega">×</button></div><div class="deliverable-body">'+body+'</div>'+(!deliverableView.error&&page.has_more?'<div class="deliverable-footer"><button type="button" class="action" data-deliverable-more data-focus-key="deliverable-more"'+(deliverableView.loading?' disabled':'')+'>'+(deliverableView.loading?'Cargando…':'Cargar más')+'</button></div>':'')+'</section>';}
function disclosureAttributes(id){const itemId=String(selected()?.id||'selected');const values=Array.isArray(openDisclosuresByItem[itemId])?openDisclosuresByItem[itemId]:[];return ' data-disclosure="'+escapeHtml(id)+'"'+(values.includes(id)?' open':'');}
function historyHtml(stage){ const history=stage.history||[];if(!history.length)return '';const id='history-'+stage.id;return '<details class="disclosure"'+disclosureAttributes(id)+'><summary data-focus-key="disclosure-'+escapeHtml(id)+'">Rondas anteriores ('+history.length+')</summary>'+history.map(entry=>'<div class="history-row"><div class="history-label">'+escapeHtml(entry.label)+' · '+escapeHtml(entry.stateLabel)+'</div>'+(entry.summary?'<div class="history-copy">'+escapeHtml(entry.summary)+'</div>':'')+'</div>').join('')+'</details>'; }
function stageTechnicalHtml(stage){ const rows=stage.technicalRows||[];const sections=stage.technicalSections||[];if(!rows.length&&!sections.length)return '';const id='technical-'+stage.id;return '<details class="disclosure"'+disclosureAttributes(id)+'><summary data-focus-key="disclosure-'+escapeHtml(id)+'">Detalles técnicos de esta etapa</summary>'+(rows.length?'<div class="technical-grid">'+technicalRowsHtml(rows)+'</div>':'')+sectionsHtml(sections)+'</details>'; }
function saveViewState(){ vscode.setState({expandedByItem,activeStageByItem,openDisclosuresByItem,historyFilter,historySearch,historyExpanded,draftText}); }
function expandedStages(item){ const id=String(item.id||'selected');const presentation=item.presentation||{};const hasSaved=Array.isArray(expandedByItem[id]);let values=hasSaved?[...expandedByItem[id]]:[];const preferred=presentation.activeStage||(presentation.overallState==='complete'?'review':'planning');if(preferred&&activeStageByItem[id]!==preferred){if(!values.includes(preferred))values.push(preferred);activeStageByItem[id]=preferred;}expandedByItem[id]=values;saveViewState();return new Set(values); }
function setStageExpanded(itemId,stageId,open){ const values=new Set(Array.isArray(expandedByItem[itemId])?expandedByItem[itemId]:[]);if(open)values.add(stageId);else values.delete(stageId);expandedByItem[itemId]=[...values];saveViewState();const toggle=[...els.content.querySelectorAll('[data-stage-toggle]')].find(node=>node.dataset.stageToggle===stageId);const body=document.getElementById('stage-body-'+stageId);if(toggle){toggle.setAttribute('aria-expanded',String(open));toggle.setAttribute('aria-label',toggle.dataset.stageTitle+': '+toggle.dataset.stageStatus+'. '+(open?'Contraer':'Expandir'));}if(body)body.hidden=!open; }
function stageHtml(stage,open){ const label=stage.title+': '+stage.statusLabel+'. '+(open?'Contraer':'Expandir');return '<article class="stage-card '+escapeHtml(stage.state)+'"><button type="button" class="stage-toggle" data-stage-toggle="'+escapeHtml(stage.id)+'" data-stage-title="'+escapeHtml(stage.title)+'" data-stage-status="'+escapeHtml(stage.statusLabel)+'" data-focus-key="stage-'+escapeHtml(stage.id)+'" aria-label="'+escapeHtml(label)+'" aria-expanded="'+String(open)+'" aria-controls="stage-body-'+escapeHtml(stage.id)+'"><span class="stage-state-icon" aria-hidden="true">'+stageIcon(stage.state)+'</span><span class="stage-heading"><span class="stage-title">'+escapeHtml(stage.title)+'</span><span class="stage-subtitle">'+escapeHtml(stage.subtitle)+'</span></span><span class="stage-chevron" aria-hidden="true">›</span></button><div class="stage-body" id="stage-body-'+escapeHtml(stage.id)+'"'+(open?'':' hidden')+'><div class="stage-status">'+escapeHtml(stage.statusLabel)+'</div><div class="stage-duration" data-stage-duration="'+escapeHtml(stage.id)+'">'+escapeHtml(stage.durationLabel||'')+'</div><p class="stage-purpose">'+escapeHtml(stage.purpose)+'</p>'+(stage.summary?'<div class="report-summary">'+escapeHtml(stage.summary)+'</div>':'')+factsHtml(stage.facts)+sectionsHtml(stage.sections)+stageMilestonesHtml(stage)+stageDeliverablesHtml(stage)+historyHtml(stage)+stageTechnicalHtml(stage)+'</div></article>'; }
function stageStripHtml(stages){ return '<div class="stage-strip" aria-label="Avance de la sesión">'+(stages||[]).map(stage=>'<div class="stage-strip-item '+escapeHtml(stage.state)+'"'+(stage.state==='active'?' aria-current="step"':'')+'><span class="stage-strip-icon" aria-hidden="true">'+stageIcon(stage.state)+'</span>'+escapeHtml(stage.title)+'<span class="sr-only">: '+escapeHtml(stage.statusLabel)+'</span></div>').join('')+'</div>'; }
function nowCardHtml(presentation){ const latest=(presentation.milestones||[]).slice(-1)[0];return '<section class="now-card" aria-labelledby="now-title"><div class="card-eyebrow">Ahora</div><h3 class="card-title" id="now-title">'+escapeHtml(presentation.headline)+'</h3><p class="card-copy">'+escapeHtml(presentation.explanation)+'</p>'+stageStripHtml(presentation.stages)+(latest?'<div class="recent-milestone"><span aria-hidden="true">'+milestoneIcon(latest)+'</span><span>'+escapeHtml(latest.label)+'</span></div>':'')+(presentation.lastEventAt?'<div class="last-update">'+escapeHtml(formatWhen(presentation.lastEventAt))+'</div>':'')+'</section>'; }
function outcomeHtml(outcome,isFinal=false){if(!outcome)return '';const sections=outcome.sections||[];const technical=outcome.technicalSections||[];const fallback=outcome.tone==='positive'?'Baldr completó y revisó el trabajo.':'Este es el resultado disponible hasta el momento.';const eyebrow=isFinal?'Resultado final':'Resultado hasta ahora';const id='result-technical';const technicalDetails=technical.length?'<details class="disclosure"'+disclosureAttributes(id)+'><summary data-focus-key="disclosure-'+id+'">Detalles técnicos del resultado</summary>'+sectionsHtml(technical)+'</details>':'';return '<section class="result-card '+escapeHtml(outcome.tone||'neutral')+'" aria-labelledby="result-title"><div class="card-eyebrow">'+eyebrow+'</div><h3 class="card-title" id="result-title">'+escapeHtml(outcome.title||'Resultado hasta ahora')+'</h3><p class="card-copy">'+escapeHtml(outcome.summary||fallback)+'</p>'+factsHtml(outcome.facts)+fileChangesHtml(outcome.fileChanges)+sectionsHtml(sections)+technicalDetails+'</section>';}
function hasReconciliation(item){ return (item.allowed_actions||[]).some(action=>['authorize_changes','decline_changes','inspect_shadow','continue_from_shadow','apply_shadow_changes','discard_shadow','resume_from_checkpoint','accept_existing_changes','discard_worktree','mark_failed'].includes(action)); }
function authorizationActions(item,attention){if(attention?.kind!=='authorization')return null;const actions=item.allowed_actions||[];if(!actions.includes('authorize_changes')||!actions.includes('decline_changes'))return null;return {approve:'authorize_changes',decline:'decline_changes'};}
function attentionPrimaryAction(item,attention){ if(!attention||authorizationActions(item,attention))return null;if(hasReconciliation(item))return {id:'reconcile',label:attention.actionLabel||'Elegir cómo continuar'};if(attention.kind==='changes_requested'&&(item.allowed_actions||[]).includes('continue'))return {id:'continue',label:attention.actionLabel||'Indicar correcciones'};if(attention.retryable===true&&(item.allowed_actions||[]).includes('start'))return {id:'start',label:attention.actionLabel||'Volver a intentar'};return null; }
function attentionHtml(item,attention){ if(!attention)return '';const authorization=authorizationActions(item,attention);const primary=attentionPrimaryAction(item,attention);const blockers=attention.blockers||[];const disabled=state.busy?' disabled':'';const authorizationHtml=authorization?'<div class="attention-action"><button class="action primary" data-action="'+authorization.approve+'" data-focus-key="action-'+authorization.approve+'"'+disabled+'>Autorizar cambios y reintentar</button><button class="action" data-action="'+authorization.decline+'" data-focus-key="action-'+authorization.decline+'"'+disabled+'>No autorizar</button></div>':'';return '<section class="attention-card" role="alert" aria-labelledby="attention-title"><div class="card-eyebrow">Necesita tu atención</div><h3 class="card-title" id="attention-title">'+escapeHtml(attention.title)+'</h3><p class="card-copy">'+escapeHtml(attention.message)+'</p>'+(blockers.length?'<section class="report-section danger"><h4 class="report-section-title">Qué necesita resolverse</h4><ul>'+blockers.map(blocker=>'<li>'+escapeHtml(blocker)+'</li>').join('')+'</ul></section>':'')+authorizationHtml+(primary?'<div class="attention-action"><button class="action primary" data-action="'+escapeHtml(primary.id)+'" data-focus-key="action-'+escapeHtml(primary.id)+'"'+disabled+'>'+escapeHtml(primary.label)+'</button></div>':'')+'</section>'; }
function refreshErrorHtml(hasItemAttention){ if(!state.error||hasItemAttention)return '';return '<section class="refresh-error" role="alert"><div class="refresh-error-title">No pudimos actualizar esta vista</div><p class="refresh-error-copy">Tu sesión sigue guardada. Probá nuevamente.</p><button type="button" class="action" data-refresh-action data-focus-key="refresh-error">Reintentar</button></section>'; }
function globalTechnicalHtml(presentation){ const rows=presentation.technicalRows||[];const id='technical-session';return '<details class="disclosure"'+disclosureAttributes(id)+'><summary data-focus-key="disclosure-'+id+'">Detalles técnicos de la sesión</summary>'+(rows.length?'<div class="technical-grid">'+technicalRowsHtml(rows)+'</div>':'')+'<p><button class="action" data-action="logs" data-focus-key="action-logs">Abrir registro técnico</button></p></details>'; }
function sessionRequestHtml(item){const turns=Array.isArray(item?.turns)&&item.turns.length?item.turns:[{ordinal:1,request:String(item?.task||'')}];const visible=turns.filter(turn=>String(turn?.request||'').trim());if(!visible.length)return '';const id='session-request';const title=visible.length===1?'Pedido original':'Conversación ('+visible.length+' pedidos)';return '<details class="session-section"'+disclosureAttributes(id)+'><summary data-focus-key="disclosure-'+id+'">'+title+'</summary><div class="session-section-body">'+visible.map((turn,index)=>'<article class="conversation-turn"><div class="conversation-turn-label">'+(index===0?'Pedido original':'Continuación '+String(index))+'</div><div class="task-body">'+escapeHtml(turn.request)+'</div></article>').join('')+'</div></details>';}
function sessionProgressHtml(stages,presentation,expanded){if(!stages.length)return '';const id='session-progress';return '<details class="session-section"'+disclosureAttributes(id)+'><summary data-focus-key="disclosure-'+id+'">Etapas y entregas</summary><div class="session-section-body"><section class="stage-list" aria-label="Etapas de la sesión">'+stages.map(stage=>stageHtml(stage,expanded.has(stage.id))).join('')+'</section>'+deliverableIndexHtml(presentation)+'</div></details>';}
	function actionButtons(item,attention,attentionActionShown){ const actions = item.allowed_actions || []; const rows=[];
 const disabled=state.busy?' disabled':'';
 if(actions.includes('start')&&!attentionActionShown&&(!attention||attention.retryable===true)) rows.push('<button class="action primary" data-action="start" data-focus-key="action-start"'+disabled+'>'+escapeHtml(attention?'Volver a intentar':'Empezar')+'</button>');
 if(actions.includes('cancel')) rows.push('<button class="action" data-action="cancel" data-focus-key="action-cancel"'+disabled+'>Cancelar</button>');
 if(!attentionActionShown&&hasReconciliation(item)) rows.push('<button class="action primary" data-action="reconcile" data-focus-key="action-reconcile"'+disabled+'>Revisar opciones</button>');
	 if(actions.includes('archive')) rows.push('<button class="action" data-action="archive" data-focus-key="action-archive"'+disabled+'>Archivar</button>');
	 if(actions.includes('restore')) rows.push('<button class="action primary" data-action="restore" data-focus-key="action-restore"'+disabled+'>Restaurar</button>');
	 if(actions.includes('delete')) rows.push('<button class="action" data-action="delete" data-focus-key="action-delete"'+disabled+'>Eliminar permanentemente</button>');
	 return rows.join(''); }
		function deliverableFingerprint(){const descriptor=deliverableView.descriptor||{};const data=deliverableView.data||{};const page=data.page||{};return [deliverableView.open,deliverableView.loading,deliverableView.error,deliverableView.itemId,deliverableView.descriptorDigest,deliverableView.requestId,descriptor.stage,descriptor.round,deliverableRunOrdinal(descriptor),data.digest,(page.entries||[]).length,page.next_cursor].join('|');}
		function blankDeliverableView(){return {open:false,loading:false,error:'',descriptor:null,data:null,itemId:'',descriptorDigest:'',requestId:0};}
		function syncDeliverableModality(){const open=Boolean(deliverableView.open);els.content.style.overflow=open?'hidden':'';for(const node of [els.header,els.composer]){if(!node)continue;node.inert=open;if(open){node.setAttribute('inert','');node.setAttribute('aria-hidden','true');}else{node.removeAttribute('inert');node.removeAttribute('aria-hidden');}}const background=els.content.querySelector('[data-deliverable-background]');if(background){background.inert=open;if(open){background.setAttribute('inert','');background.setAttribute('aria-hidden','true');}else{background.removeAttribute('inert');background.removeAttribute('aria-hidden');}}}
		function rerenderDeliverable(){lastContentKey='';renderContent();syncDeliverableModality();updateSendState();}
		function requestDeliverable(descriptor,cursor){const item=selected();if(!deliverableView.open||!item||!descriptor||String(item.id||'')!==deliverableView.itemId)return;const requestId=++deliverableRequestSequence;deliverableView.requestId=requestId;post({type:'inspectDeliverable',itemId:deliverableView.itemId,stage:descriptor.stage,round:Number(descriptor.round||0),runOrdinal:deliverableRunOrdinal(descriptor)||undefined,cursor:cursor||undefined,descriptorDigest:deliverableView.descriptorDigest,requestId});}
		function openDeliverable(node){const item=selected();const stageId=String(node?.dataset?.deliverableStage||'');const round=Number(node?.dataset?.deliverableRound);const runOrdinal=Number(node?.dataset?.deliverableRun);const stage=(item?.presentation?.stages||[]).find(value=>value.id===stageId);const descriptor=stageDeliverableValues(stage||{}).find(value=>Number(value.round)===round&&deliverableRunOrdinal(value)===runOrdinal);if(!item||!descriptor)return;setPlusMenu(false);deliverableReturnFocusKey=String(node.dataset.focusKey||'');deliverableScrollTop=0;deliverableTechnicalOpen=false;deliverableView={open:true,loading:true,error:'',descriptor,data:null,itemId:String(item.id||''),descriptorDigest:String(descriptor.digest||''),requestId:0};pendingFocusKey='deliverable-close';requestDeliverable(descriptor,'');rerenderDeliverable();}
		function closeDeliverable(restoreFocus=true){if(!deliverableView.open)return;const returnFocus=restoreFocus?deliverableReturnFocusKey:'';++deliverableRequestSequence;deliverableView=blankDeliverableView();deliverableReturnFocusKey='';deliverableScrollTop=0;deliverableTechnicalOpen=false;pendingFocusKey=returnFocus;rerenderDeliverable();}
		function loadMoreDeliverable(){const descriptor=deliverableView.descriptor;const cursor=String(deliverableView.data?.page?.next_cursor||'');if(!descriptor||!cursor||deliverableView.loading)return;deliverableView.loading=true;deliverableView.error='';pendingFocusKey='deliverable-more';requestDeliverable(descriptor,cursor);rerenderDeliverable();}
		function retryDeliverable(){const descriptor=deliverableView.descriptor;if(!descriptor||deliverableView.loading)return;deliverableView.loading=true;deliverableView.error='';deliverableView.data=null;deliverableScrollTop=0;pendingFocusKey='deliverable-close';requestDeliverable(descriptor,'');rerenderDeliverable();}
		function matchesDeliverableResponse(message,data){const descriptor=deliverableView.descriptor||{};return deliverableView.open&&String(selected()?.id||'')===deliverableView.itemId&&String(message?.itemId||'')===deliverableView.itemId&&Number(message?.requestId)===deliverableView.requestId&&String(message?.descriptorDigest||'')===deliverableView.descriptorDigest&&String(data?.stage||'')===String(descriptor.stage||'')&&Number(data?.round)===Number(descriptor.round)&&(!deliverableRunOrdinal(data)||!deliverableRunOrdinal(descriptor)||deliverableRunOrdinal(data)===deliverableRunOrdinal(descriptor));}
		function applyDeliverableResult(message){const data=message?.deliverable||{};if(!matchesDeliverableResponse(message,data))return;const incomingPage=data.page||{};if(message.append&&deliverableView.data){const previousEntries=deliverableView.data.page?.entries||[];deliverableView.data={...data,page:{...incomingPage,entries:[...previousEntries,...(incomingPage.entries||[])]}};}else{deliverableView.data=data;}deliverableView.loading=false;deliverableView.error='';pendingFocusKey=incomingPage.has_more?'deliverable-more':'deliverable-close';rerenderDeliverable();}
		function applyDeliverableError(message){if(!matchesDeliverableResponse(message,deliverableView.descriptor||{}))return;deliverableView.loading=false;deliverableView.error=String(message?.message||'No pudimos abrir la entrega. Probá nuevamente.');pendingFocusKey='deliverable-close';rerenderDeliverable();}
		function blankDeliverableIndexView(itemId='',index={}){const sourceCursor=String(index?.nextCursor||'');return {itemId,initialized:Boolean(itemId),loading:false,error:'',sourceCursor,nextCursor:sourceCursor,requestCursor:'',requestId:0,items:[]};}
		function syncDeliverableIndexForItem(item){const itemId=String(item?.id||'');const index=item?.presentation?.deliverableIndex||{};const sourceCursor=String(index?.nextCursor||'');if(deliverableIndexView.itemId!==itemId||!deliverableIndexView.initialized||deliverableIndexView.sourceCursor!==sourceCursor){++deliverableIndexRequestSequence;deliverableIndexView=blankDeliverableIndexView(itemId,index);}}
		function requestDeliverableIndex(){const item=selected();const cursor=String(deliverableIndexView.nextCursor||'');if(!item||deliverableIndexView.loading||String(item.id||'')!==deliverableIndexView.itemId||!cursor)return;const requestId=++deliverableIndexRequestSequence;deliverableIndexView.loading=true;deliverableIndexView.error='';deliverableIndexView.requestCursor=cursor;deliverableIndexView.requestId=requestId;pendingFocusKey='deliverable-index';lastContentKey='';renderContent();post({type:'loadDeliverableIndex',itemId:deliverableIndexView.itemId,cursor,requestId});}
		function matchesDeliverableIndexResponse(message){return String(selected()?.id||'')===deliverableIndexView.itemId&&String(message?.itemId||'')===deliverableIndexView.itemId&&Number(message?.requestId)===deliverableIndexView.requestId&&String(message?.cursor||'')===deliverableIndexView.requestCursor;}
		function applyDeliverableIndexResult(message){if(!matchesDeliverableIndexResponse(message))return;const unique=new Map();[...deliverableIndexView.items,...(Array.isArray(message.items)?message.items:[])].forEach(entry=>unique.set(deliverableDescriptorKey(entry),entry));deliverableIndexView.items=[...unique.values()];deliverableIndexView.loading=false;deliverableIndexView.error='';deliverableIndexView.nextCursor=message.page?.has_more?String(message.page?.next_cursor||''):'';deliverableIndexView.requestCursor='';pendingFocusKey='deliverable-index';lastContentKey='';renderContent();}
		function applyDeliverableIndexError(message){if(!matchesDeliverableIndexResponse(message))return;deliverableIndexView.loading=false;deliverableIndexView.error=String(message?.message||'No pudimos cargar las entregas anteriores. Probá nuevamente.');deliverableIndexView.requestCursor='';pendingFocusKey='deliverable-index';lastContentKey='';renderContent();}
	function trapDeliverableFocus(event,panel){if(event.key!=='Tab')return;const focusable=[...panel.querySelectorAll('button:not([disabled]), summary, [href], input:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])')];if(!focusable.length)return;const first=focusable[0];const last=focusable[focusable.length-1];if(event.shiftKey&&(document.activeElement===first||!panel.contains(document.activeElement))){event.preventDefault();last.focus();}else if(!event.shiftKey&&(document.activeElement===last||!panel.contains(document.activeElement))){event.preventDefault();first.focus();}}
	function renderContent(){
	 const item=selected();
	 const presentation=item?.presentation||null;const attentionKey=[presentation?.attention?.kind,presentation?.attention?.retryable,presentation?.attention?.actionLabel,presentation?.attention?.message].map(value=>String(value??'')).join(':');const contentKey=(state.emptyWorkspace?'empty':state.trusted===false?'untrusted':!item?'none':String(item.id)+'|'+String(presentation?.revision||item.updated_at||'')+'|'+String(item.status||'')+'|'+JSON.stringify(item.allowed_actions||[])+'|attention:'+attentionKey+'|'+String(Boolean(state.busy)))+'|error:'+String(Boolean(state.error))+'|deliverable:'+deliverableFingerprint()+'|deliverable-index:'+[deliverableIndexView.itemId,deliverableIndexView.loading,deliverableIndexView.error,deliverableIndexView.nextCursor,deliverableIndexView.items.length].join('|');
 if(contentKey===lastContentKey)return;lastContentKey=contentKey;
 els.content.setAttribute('aria-busy',String(Boolean(state.busy)));
 const currentDeliverableBody=els.content.querySelector('.deliverable-body');if(currentDeliverableBody)deliverableScrollTop=currentDeliverableBody.scrollTop;const previousScroll=els.content.scrollTop;const activeElement=document.activeElement;const focusKey=els.content.contains(activeElement)?String(activeElement?.dataset?.focusKey||''):'';
 if(state.emptyWorkspace){const choose=state.workspaceChoiceRequired?'<p><button type="button" class="action primary" data-choose-workspace>Elegir carpeta</button></p>':'';els.content.innerHTML='<div class="empty"><div class="empty-inner">'+emptyMark()+'<div class="empty-title">'+(state.workspaceChoiceRequired?'Elegí el proyecto activo':'Abrí una carpeta para empezar')+'</div><div class="empty-detail">'+(state.workspaceChoiceRequired?'Hay varias carpetas abiertas. Baldr no elegirá una en silencio.':'Baldr necesita una carpeta donde guardar el trabajo.')+'</div>'+choose+'</div></div>';els.content.querySelector('[data-choose-workspace]')?.addEventListener('click',()=>post({type:'chooseWorkspace'}));return; }
 if(state.trusted===false){ els.content.innerHTML='<div class="empty"><div class="empty-inner">'+emptyMark()+'<div class="empty-title">Falta tu permiso</div><div class="empty-detail">Autorizá esta carpeta para que Baldr pueda trabajar.</div><p><button type="button" class="action primary" id="trust">Revisar permisos</button></p></div></div>'; document.getElementById('trust')?.addEventListener('click',()=>post({type:'requestTrust'})); return; }
 if(!item){ els.content.innerHTML=refreshErrorHtml(false)+'<div class="empty"><div class="empty-inner">'+emptyMark()+'<div class="empty-title">¿Qué querés hacer?</div><div class="empty-detail">Escribí lo que necesitás. Baldr lo organiza y te muestra el avance.</div><p><button type="button" class="action primary" data-start-request>Escribir un pedido</button></p></div></div>';els.content.querySelector('[data-refresh-action]')?.addEventListener('click',()=>post({type:'refresh'}));els.content.querySelector('[data-start-request]')?.addEventListener('click',()=>els.input.focus());return; }
 const error = itemErrorMessage(item);const expanded=expandedStages(item);const stages=presentation?.stages||[];
 const activeStagePresentation=(presentation?.stages||[]).find(stage=>stage.id===presentation?.activeStage);const announcementDetail=presentation?.attention?.message||activeStagePresentation?.summary||presentation?.outcome?.summary||'';
 const announcementKey=presentation?String(item.id||'selected')+'|'+String(presentation.revision||'')+'|'+String(presentation.headline||'')+'|'+String(presentation.activeStage||'')+'|'+String(announcementDetail):'';
 if(presentation&&presentation.headline&&announcementKey!==lastAnnouncement){lastAnnouncement=announcementKey;els.liveStatus.textContent=presentation.headline+'. '+presentation.explanation+(announcementDetail?' '+announcementDetail:'');}
 const attentionActionShown=Boolean(attentionPrimaryAction(item,presentation?.attention)||authorizationActions(item,presentation?.attention));const availableActions=actionButtons(item,presentation?.attention,attentionActionShown);
	 const completed=Boolean(presentation?.overallState==='complete'&&presentation?.outcome);const primary=completed?outcomeHtml(presentation.outcome,true):(presentation?nowCardHtml(presentation):'')+attentionHtml(item,presentation?.attention)+outcomeHtml(presentation?.outcome,false);els.content.innerHTML=deliverableViewHtml()+'<div class="content-background" data-deliverable-background'+(deliverableView.open?' inert aria-hidden="true"':'')+'><h2 class="item-title">'+escapeHtml(item.title || 'Sin título')+'</h2><div class="item-meta"><span>'+escapeHtml(statusLabel(item.status))+'</span><span>'+escapeHtml(presetLabel(item.preset))+'</span><span>'+escapeHtml(safetyLabel(item.safety_mode))+'</span></div>'+refreshErrorHtml(Boolean(presentation?.attention))+(error&&!presentation?.attention?'<div class="notice">'+escapeHtml(error)+'</div>':'')+primary+(availableActions?'<div class="actions">'+availableActions+'</div>':'')+sessionRequestHtml(item)+sessionProgressHtml(stages,presentation,expanded)+globalTechnicalHtml(presentation||{technicalRows:[]})+'<div class="loading '+(state.busy?'visible':'')+'" id="loading" role="status" aria-live="polite"><span class="sr-only">'+escapeHtml(state.operationLabel||'')+'</span></div></div>';
	 els.content.querySelectorAll('[data-stage-toggle]').forEach(node=>node.addEventListener('click',()=>setStageExpanded(String(item.id||'selected'),node.dataset.stageToggle,node.getAttribute('aria-expanded')!=='true')));
	 els.content.querySelectorAll('[data-open-changed-file]').forEach(node=>node.addEventListener('click',()=>post({type:'openChangedFile',path:node.dataset.openChangedFile})));
	 els.content.querySelectorAll('[data-deliverable-stage]').forEach(node=>node.addEventListener('click',()=>openDeliverable(node)));
		 els.content.querySelector('[data-deliverable-close]')?.addEventListener('click',()=>closeDeliverable(true));
	 els.content.querySelector('[data-deliverable-more]')?.addEventListener('click',loadMoreDeliverable);
		 els.content.querySelector('[data-deliverable-retry]')?.addEventListener('click',retryDeliverable);
		 els.content.querySelector('[data-deliverable-index]')?.addEventListener('click',requestDeliverableIndex);
		 els.content.querySelector('[data-deliverable-technical]')?.addEventListener('toggle',event=>{deliverableTechnicalOpen=Boolean(event.currentTarget?.open);});
	 const deliverablePanel=els.content.querySelector('.deliverable-panel');if(deliverablePanel)deliverablePanel.addEventListener('keydown',event=>trapDeliverableFocus(event,deliverablePanel));
 els.content.querySelectorAll('[data-disclosure]').forEach(node=>node.addEventListener('toggle',()=>{const itemId=String(item.id||'selected');const values=new Set(Array.isArray(openDisclosuresByItem[itemId])?openDisclosuresByItem[itemId]:[]);if(node.open)values.add(node.dataset.disclosure);else values.delete(node.dataset.disclosure);openDisclosuresByItem[itemId]=[...values];saveViewState();}));
 els.content.querySelectorAll('[data-action]').forEach(node => node.addEventListener('click',()=>{ const action=node.dataset.action; if(action==='logs')post({type:'openLogs'}); else if(action==='continue')els.input.focus(); else post({type:'itemAction',action,itemId:item.id}); }));
 els.content.querySelector('[data-refresh-action]')?.addEventListener('click',()=>post({type:'refresh'}));
		 els.content.scrollTop=previousScroll;const nextDeliverableBody=els.content.querySelector('.deliverable-body');if(nextDeliverableBody)nextDeliverableBody.scrollTop=deliverableScrollTop;const requestedFocus=pendingFocusKey||focusKey;pendingFocusKey='';if(requestedFocus){const focusTarget=[...els.content.querySelectorAll('[data-focus-key]')].find(node=>node.dataset.focusKey===requestedFocus);focusTarget?.focus({preventScroll:true});}
}
function shortModelLabel(value){ const raw=String(value||'').trim(); const named=raw.match(/^gpt-[0-9]+(?:[.][0-9]+)*-(sol|terra|luna|spark)$/i); if(named)return named[1].charAt(0).toUpperCase()+named[1].slice(1).toLowerCase(); const version=raw.match(/^gpt-([0-9]+(?:[.][0-9]+)*)(?:-(mini))?$/i); if(version)return 'GPT-'+version[1]+(version[2]?' Mini':''); return raw||''; }
function effortChipLabel(value){ return ({minimal:'Mínimo',low:'Bajo',medium:'Medio',high:'Alto',xhigh:'Muy alto',max:'Máximo',ultra:'Ultra'}[String(value||'').toLowerCase()]||String(value||'')); }
function configuredRole(role){ const wb=workbench(); const pref=wb.preferences||{}; const profiles=wb.profiles||{}; const selected=(((pref.role_profiles||{})[role]||[])[0]); if(selected&&profiles.execution_profiles&&profiles.execution_profiles[selected])return profiles.execution_profiles[selected]; return (((profiles.resolved_roles||{})[role]||[])[0])||{}; }
function renderChips(){ const pref=workbench().preferences||{}; const safety=safetyLabel(pref.safety_mode); const preset=presetLabel(pref.preset); const context=contextLabel(pref.context_mode); const roleNames={architect:'Planificación',implementer:'Ejecución',reviewer:'Revisión'}; const roleConfigurations=['architect','implementer','reviewer'].map(role=>{const config=configuredRole(role);const raw=config.agent_ref||config.model||config.agent||config.provider||'';return {role,label:shortModelLabel(raw),effort:effortChipLabel(config.reasoning_effort||config.effort)};}); const modelNames=[...new Set(roleConfigurations.map(item=>item.label).filter(Boolean))]; const configuredTeam=modelNames.length?modelNames.join(' · '):'Equipo estándar'; const automatic=String(pref.team_mode||'')==='automatic'; const fixedCount=Object.keys(pref.agent_overrides||{}).length; const team=automatic?(fixedCount?'Automático · '+fixedCount+' fijo'+(fixedCount===1?'':'s'):'Automático'):configuredTeam; const configuredDetail=roleConfigurations.filter(item=>item.label).map(item=>roleNames[item.role]+': '+item.label+(item.effort?' ('+item.effort+')':'')).join(' · '); const teamDetail=automatic?(fixedCount?'Selección automática con '+fixedCount+' etapa'+(fixedCount===1?' fija':'s fijas'):'Selección automática por compatibilidad y disponibilidad'):configuredDetail; els.gitChipLabel.textContent=safety; els.gitChip.title='Uso de Git y protección: '+safety; els.gitChip.setAttribute('aria-label',els.gitChip.title); els.presetChipLabel.textContent=preset; els.presetChip.title='Nivel de detalle: '+preset; els.presetChip.setAttribute('aria-label',els.presetChip.title); els.rolesChipLabel.textContent=team; els.rolesChip.title='Equipo de Baldr: '+(teamDetail||team); els.rolesChip.setAttribute('aria-label',els.rolesChip.title); els.contextChipLabel.textContent=context; els.contextChip.title='Ayuda adicional: '+context; els.contextChip.setAttribute('aria-label',els.contextChip.title); }
function renderPending(){ const items=(state.pending||{}).attachments||[];const key=items.map(item=>[item.kind,item.label].join(':')).join('|');if(key===lastPendingKey)return;lastPendingKey=key;const focusedIndex=els.attachments.contains(document.activeElement)?String(document.activeElement?.dataset?.removePending||''):''; const kindLabels={file:'Archivo',folder:'Carpeta',selection:'Selección'}; els.attachments.innerHTML=items.map((item,index)=>'<div class="attachment"><div class="attachment-icon" aria-hidden="true">'+({file:'▤',folder:'◇',selection:'≡'}[item.kind]||'▤')+'</div><div><div class="attachment-label" title="'+escapeHtml(item.label||'Archivo')+'">'+escapeHtml(item.label||'Archivo')+'</div><div class="attachment-kind">'+escapeHtml(kindLabels[item.kind]||'Archivo')+'</div></div><button type="button" class="remove-attachment" data-remove-pending="'+index+'" title="Quitar" aria-label="Quitar '+escapeHtml(item.label||'archivo')+'">×</button></div>').join(''); els.attachments.querySelectorAll('[data-remove-pending]').forEach(node=>node.addEventListener('click',()=>post({type:'removePending',index:Number(node.dataset.removePending)})));if(focusedIndex){const focusTarget=[...els.attachments.querySelectorAll('[data-remove-pending]')].find(node=>node.dataset.removePending===focusedIndex);focusTarget?.focus({preventScroll:true});} }
function renderComposerContext(){const item=selected();const continuing=Boolean(item&&(item.allowed_actions||[]).includes('continue'));els.input.placeholder=continuing?'Continuar esta conversación…':'Escribí qué necesitás…';els.input.setAttribute('aria-label',continuing?'Continuar conversación con Baldr':'Nuevo pedido para Baldr');const active=String(state.activeContext||'');els.activeContext.textContent=active?'Contexto actual: '+active:'';els.activeContext.title=active?'Baldr incluirá este archivo o selección al enviar':'';}
function updateDurations(){ const stages=selected()?.presentation?.stages||[];stages.forEach(stage=>{const node=els.content.querySelector('[data-stage-duration="'+stage.id+'"]');if(node)node.textContent=stage.durationLabel||'';}); }
function resizeComposer(){els.input.style.height='auto';els.input.style.height=Math.min(150,els.input.scrollHeight)+'px';}
function updateSendState(){ const blocked=Boolean(state.busy)||Boolean(deliverableView.open);els.send.disabled=blocked||!els.input.value.trim();for(const control of [els.plus,els.configure,els.refresh,els.gitChip,els.presetChip,els.rolesChip,els.contextChip])control.disabled=blocked; }
function render(){ renderHistoryVisibility();renderTasks(); renderContent(); updateDurations(); renderChips(); renderPending();renderComposerContext(); syncDeliverableModality(); updateSendState(); }
function updateState(next){const incoming={...(next||{}),error:''};const incomingItemId=String(incoming?.workbench?.selected?.id||'');if(deliverableView.open&&incomingItemId!==deliverableView.itemId){++deliverableRequestSequence;deliverableView=blankDeliverableView();deliverableReturnFocusKey='';deliverableScrollTop=0;deliverableTechnicalOpen=false;pendingFocusKey='';lastContentKey='';}state=incoming;syncDeliverableIndexForItem(selected());render(); }
function setPlusMenu(open){ plusMenuOpen=open; els.plusMenu.classList.toggle('visible',open); els.plusMenu.setAttribute('aria-hidden',String(!open)); els.plus.setAttribute('aria-expanded',String(open)); if(open){els.plusFilter.value='';filterPlusActions();els.plusMenu.scrollTop=0;} }
function normalizeSearch(value){ return String(value||'').normalize('NFD').replace(/[̀-ͯ]/g,'').toLowerCase(); }
function visiblePlusActions(){ return [...els.plusMenu.querySelectorAll('[data-plus-action]')].filter(node=>!node.hidden); }
function filterPlusActions(){ const query=normalizeSearch(els.plusFilter.value.trim()); const actions=[...els.plusMenu.querySelectorAll('[data-plus-action]')]; actions.forEach(node=>{node.hidden=Boolean(query)&&!normalizeSearch(node.textContent).includes(query);}); els.plusMenu.querySelectorAll('[data-plus-heading]').forEach(heading=>{const group=heading.dataset.plusHeading;heading.hidden=!actions.some(node=>node.dataset.plusGroup===group&&!node.hidden);}); els.plusEmpty.hidden=actions.some(node=>!node.hidden); }
function renderSlash(){ const value=els.input.value.trim(); if(!value.startsWith('/')){els.slash.classList.remove('visible');return;} const query=value.slice(1).toLowerCase(); const list=commands().filter(c=>c.id.startsWith(query.split(/\s/)[0])).slice(0,8); if(!list.length){els.slash.classList.remove('visible');return;} slashIndex=Math.min(slashIndex,list.length-1); els.slash.innerHTML=list.map((c,i)=>'<div class="slash-item '+(i===slashIndex?'active':'')+'" data-command="'+escapeHtml(c.id)+'"><span class="slash-command">/'+escapeHtml(c.id)+'</span><span class="slash-description">'+escapeHtml(c.description)+'</span></div>').join(''); els.slash.classList.add('visible'); els.slash.querySelectorAll('[data-command]').forEach(n=>n.addEventListener('click',()=>{els.input.value='/'+n.dataset.command+' ';els.input.focus();els.slash.classList.remove('visible');})); }
function submit(){ const value=els.input.value; if(!value.trim()||state.busy||deliverableView.open)return; setPlusMenu(false); post({type:'submit',value}); }
els.send.addEventListener('click',submit); els.plus.addEventListener('click',()=>setPlusMenu(!plusMenuOpen));els.historyToggle.addEventListener('click',()=>setHistoryExpanded(!historyExpanded)); els.configure.addEventListener('click',()=>post({type:'configure'})); els.refresh.addEventListener('click',()=>post({type:'refresh'})); els.plusMenu.querySelectorAll('[data-plus-action]').forEach(node=>{node.addEventListener('click',()=>{setPlusMenu(false);post({type:'plusAction',action:node.dataset.plusAction});});node.addEventListener('keydown',event=>{if(event.key!=='ArrowDown'&&event.key!=='ArrowUp')return;event.preventDefault();const actions=visiblePlusActions();const index=actions.indexOf(node);const next=(index+(event.key==='ArrowDown'?1:-1)+actions.length)%actions.length;actions[next]?.focus();});}); els.plusFilter.addEventListener('input',filterPlusActions); els.plusFilter.addEventListener('keydown',event=>{if(event.key!=='Enter'&&event.key!=='ArrowDown'&&event.key!=='ArrowUp')return;const actions=visiblePlusActions();if(!actions.length)return;event.preventDefault();if(event.key==='Enter')actions[0].click();else if(event.key==='ArrowDown')actions[0].focus();else actions[actions.length-1].focus();}); document.querySelectorAll('[data-chip]').forEach(n=>n.addEventListener('click',()=>post({type:'chip',value:n.dataset.chip})));document.querySelectorAll('[data-history-filter]').forEach(node=>node.addEventListener('click',()=>{historyFilter=['active','completed','archived'].includes(node.dataset.historyFilter)?node.dataset.historyFilter:'active';openTaskMenuId='';saveViewState();renderTasks();}));els.historySearch?.addEventListener('input',()=>{historySearch=els.historySearch.value;openTaskMenuId='';saveViewState();renderTasks();});els.historySearch?.addEventListener('keydown',event=>{if(event.key==='ArrowDown'){const first=historyItemNodes()[0];if(first){event.preventDefault();first.focus();}}else if(event.key==='Escape'&&historySearch){event.preventDefault();historySearch='';saveViewState();renderTasks();}});
els.input.addEventListener('input',()=>{draftText=els.input.value;resizeComposer();slashIndex=0;saveViewState();renderSlash();updateSendState();});
els.input.addEventListener('keydown',event=>{if(event.key==='Enter'&&!event.shiftKey&&!els.slash.classList.contains('visible')){event.preventDefault();submit();}else if(els.slash.classList.contains('visible')&&(event.key==='ArrowDown'||event.key==='ArrowUp')){event.preventDefault();slashIndex=Math.max(0,slashIndex+(event.key==='ArrowDown'?1:-1));renderSlash();}else if(els.slash.classList.contains('visible')&&event.key==='Tab'){event.preventDefault();const active=els.slash.querySelector('.active');if(active){els.input.value='/'+active.dataset.command+' ';els.slash.classList.remove('visible');}}});
document.addEventListener('keydown',event=>{if((event.ctrlKey||event.metaKey)&&String(event.key).toLowerCase()==='f'&&!deliverableView.open){event.preventDefault();focusHistorySearch();return;}if(event.key!=='Escape')return;if(deliverableView.open){event.preventDefault();closeDeliverable(true);return;}if(openTaskMenuId){event.preventDefault();openTaskMenuId='';renderTasks();return;}if(plusMenuOpen){event.preventDefault();setPlusMenu(false);els.plus.focus();}});
document.addEventListener('pointerdown',event=>{if(plusMenuOpen&&!els.plusMenu.contains(event.target)&&!els.plus.contains(event.target))setPlusMenu(false);});
window.addEventListener('message',event=>{const msg=event.data||{};if(msg.type==='state')updateState(msg.state);else if(msg.type==='loading')document.getElementById('loading')?.classList.toggle('visible',Boolean(msg.value));else if(msg.type==='operation'){state.busy=Boolean(msg.busy);state.operationLabel=msg.label||'';render();}else if(msg.type==='pending'){state.pending=msg.pending;renderPending();}else if(msg.type==='historyFilter'){historyFilter=['active','completed','archived'].includes(msg.filter)?msg.filter:'active';historySearch='';historyExpanded=true;openTaskMenuId='';saveViewState();renderHistoryVisibility();renderTasks();}else if(msg.type==='clearInput'){els.input.value='';draftText='';resizeComposer();saveViewState();renderSlash();updateSendState();}else if(msg.type==='prefill'){els.input.value=msg.value||'';draftText=els.input.value;resizeComposer();saveViewState();els.input.focus();renderSlash();updateSendState();}else if(msg.type==='showHelp'){els.input.value='/';draftText='/';resizeComposer();saveViewState();els.input.focus();renderSlash();updateSendState();}else if(msg.type==='deliverableResult'){applyDeliverableResult(msg);}else if(msg.type==='deliverableError'){applyDeliverableError(msg);}else if(msg.type==='deliverableIndexResult'){applyDeliverableIndexResult(msg);}else if(msg.type==='deliverableIndexError'){applyDeliverableIndexError(msg);}else if(msg.type==='error'){state.error=msg.message;render();}});
els.input.value=draftText;resizeComposer();renderHistoryVisibility();renderHistoryControls();updateSendState();
post({type:'ready'});
</script>
</body>
</html>`;
  }
}

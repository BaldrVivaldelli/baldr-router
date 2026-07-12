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

const VIEW_TYPE = 'baldr.console';
const REFRESH_INTERVAL_MS = 2_500;
const MAX_SELECTION_CHARS = 20_000;

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
  itemId?: string;
  action?: string;
  index?: number;
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

function workspaceRoot(): string | undefined {
  return vscode.workspace.workspaceFolders?.[0]?.uri.fsPath;
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
    automatic: 'Protección automática',
    worktree: 'Protección automática',
    current: 'Trabajar directamente',
    'non-git': 'Sin protección',
  } as Record<string, string>)[value] ?? 'Protección automática';
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
  private refreshing = false;
  private operationCount = 0;
  private lastStatus: JsonRecord = {};
  private pending: PendingContext = { attachments: [], extraContext: '', selectionContexts: {} };
  private readonly disposables: vscode.Disposable[] = [];

  constructor(
    private readonly context: vscode.ExtensionContext,
    private readonly runtime: BaldrRuntime,
    private readonly output: vscode.LogOutputChannel,
  ) {
    this.selectedItemId = context.workspaceState.get<string>('baldr.console.selectedItemId');
    this.disposables.push(
      vscode.workspace.onDidChangeWorkspaceFolders(() => void this.refresh()),
      vscode.workspace.onDidGrantWorkspaceTrust(() => void this.refresh()),
    );
  }

  dispose(): void {
    if (this.refreshTimer) clearInterval(this.refreshTimer);
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
      }),
      view.onDidDispose(() => {
        this.view = undefined;
      }),
    );
    this.refreshTimer ??= setInterval(() => {
      if (this.view?.visible && this.shouldPoll()) void this.refresh(true);
    }, REFRESH_INTERVAL_MS);
  }

  private shouldPoll(): boolean {
    if (this.operationCount > 0) return true;
    const workbench = record(this.lastStatus.workbench);
    return asItems(workbench.items).some((item) => ['running', 'cancelling'].includes(text(item.status)));
  }

  private async post(message: JsonRecord): Promise<void> {
    if (this.view) await this.view.webview.postMessage(message);
  }

  async refresh(silent = false): Promise<void> {
    if (this.refreshing) return;
    this.refreshing = true;
    const root = workspaceRoot();
    if (!root) {
      this.lastStatus = {};
      await this.post({
        type: 'state',
        state: {
          ok: false,
          emptyWorkspace: true,
          trusted: vscode.workspace.isTrusted,
          busy: this.operationCount > 0,
          pending: this.pending,
        },
      });
      this.refreshing = false;
      return;
    }
    if (!vscode.workspace.isTrusted) {
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
      this.refreshing = false;
      return;
    }
    try {
      if (!silent) await this.post({ type: 'loading', value: true });
      const status = await this.runtime.consoleStatus(root, this.selectedItemId);
      this.lastStatus = status;
      const workbench = record(status.workbench);
      const items = asItems(workbench.items);
      if (!this.selectedItemId && items[0]?.id) {
        this.selectedItemId = String(items[0].id);
        await this.context.workspaceState.update('baldr.console.selectedItemId', this.selectedItemId);
        const selectedStatus = await this.runtime.consoleStatus(root, this.selectedItemId);
        this.lastStatus = selectedStatus;
      }
      await this.post({
        type: 'state',
        state: {
          ...this.lastStatus,
          workspaceRoot: root,
          trusted: true,
          busy: this.operationCount > 0,
          pending: this.pending,
        },
      });
    } catch (error) {
      this.output.error(error instanceof Error ? error : new Error(String(error)));
      await this.post({ type: 'error', message: error instanceof Error ? error.message : String(error) });
    } finally {
      if (!silent) await this.post({ type: 'loading', value: false });
      this.refreshing = false;
    }
  }

  private async handleMessage(message: ConsoleMessage): Promise<void> {
    switch (message.type) {
      case 'ready':
      case 'refresh':
        await this.refresh();
        return;
      case 'select':
        this.selectedItemId = message.itemId || undefined;
        await this.context.workspaceState.update('baldr.console.selectedItemId', this.selectedItemId);
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
      case 'openLogs':
        this.output.show(true);
        return;
      case 'requestTrust':
        await vscode.commands.executeCommand('workbench.trust.manage');
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
    const pending = this.consumePendingContext();
    this.launchOperation('Creando y ejecutando la tarea…', async () => {
      const result = await this.runtime.runFacade('run', {
        workspaceRoot: root,
        task: value,
        workItemAction: 'execute',
        extraContext: pending.extraContext,
        attachments: pending.attachments,
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
      case 'help':
        await this.post({ type: 'showHelp' });
        return;
      default:
        void vscode.window.showWarningMessage(`Baldr no reconoce el comando /${command}. Usá /help para ver las opciones.`);
    }
  }

  private requireWorkspace(): string {
    const root = workspaceRoot();
    if (!root) throw new Error('Abrí una carpeta antes de crear una tarea.');
    if (!vscode.workspace.isTrusted) throw new Error('Autorizá esta carpeta antes de usar Baldr.');
    return root;
  }

  private launchOperation(label: string, operation: () => Promise<JsonRecord>): void {
    this.operationCount += 1;
    void this.post({ type: 'operation', busy: true, label });
    void operation()
      .then(async (result) => {
        const item = record(result.work_item);
        if (item.id) {
          this.selectedItemId = String(item.id);
          await this.context.workspaceState.update('baldr.console.selectedItemId', this.selectedItemId);
        }
        if (result.ok === false && !await this.handlePolicyBlock(result)) {
          const reason = text(result.reason, text(record(result.error).message, 'Baldr no pudo completar la operación.'));
          void vscode.window.showErrorMessage(reason);
        }
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
      'La tarea quedó guardada, pero esta opción necesita un repositorio Git. Podés volver a la protección automática, abrir otra carpeta o trabajar sin protección.',
      { modal: true },
      'Protección automática',
      'Sin protección',
      'Abrir otra carpeta…',
    );
    if (choice === 'Abrir otra carpeta…') {
      await vscode.commands.executeCommand('workbench.action.files.openFolder');
      return true;
    }
    if (choice === 'Protección automática') {
      const configured = await this.setSafetyMode('automatic');
      if (configured && item.id) {
        this.launchOperation('Iniciando la tarea con protección automática…', () => this.runtime.startWorkItem(
          this.requireWorkspace(),
          String(item.id),
        ));
      }
      return true;
    }
    if (choice === 'Sin protección') {
      const configured = await this.setSafetyMode('non-git');
      if (configured && item.id) {
        this.launchOperation('Iniciando la tarea sin protección…', () => this.runtime.startWorkItem(
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

  private async itemAction(action: string, itemId = this.selectedItemId): Promise<void> {
    if (!itemId) {
      void vscode.window.showInformationMessage('Elegí una tarea primero.');
      return;
    }
    const root = this.requireWorkspace();
    if (action === 'start') {
      this.launchOperation('Iniciando la tarea…', () => this.runtime.startWorkItem(root, itemId));
      return;
    }
    if (action === 'cancel') {
      const confirm = await vscode.window.showWarningMessage(
        '¿Querés cancelar esta tarea y detener el trabajo en curso?',
        { modal: true },
        'Cancelar tarea',
      );
      if (confirm !== 'Cancelar tarea') return;
      this.launchOperation('Cancelando la tarea…', () => this.runtime.cancelWorkItem(root, itemId));
      return;
    }
    if (action === 'archive') {
      this.launchOperation('Archivando la tarea…', () => this.runtime.archiveWorkItem(root, itemId));
      return;
    }
    if (action === 'reconcile') {
      await this.chooseReconciliation(itemId);
      return;
    }
  }

  private async createDraft(task?: string): Promise<void> {
    const root = this.requireWorkspace();
    const value = task ?? await vscode.window.showInputBox({
      title: 'Guardar una tarea para después',
      prompt: 'Escribí qué necesitás',
      ignoreFocusOut: true,
    });
    if (!value?.trim()) return;
    const pending = this.consumePendingContext();
    const result = await this.withProgress('Guardando la tarea…', () => this.runtime.createWorkItem(
      root,
      value.trim(),
      { extraContext: pending.extraContext, attachments: pending.attachments },
    ));
    const item = record(result.work_item);
    if (item.id) this.selectedItemId = String(item.id);
    await this.post({ type: 'clearInput' });
    await this.refresh();
  }

  private async openPlusMenu(): Promise<void> {
    const choice = await vscode.window.showQuickPick([
      { id: 'draft', label: '$(add) Guardar para después', description: 'Crear una tarea sin empezarla' },
      { id: 'file', label: '$(file) Agregar el archivo abierto', description: 'Usarlo como referencia para la tarea' },
      { id: 'selection', label: '$(selection) Agregar el texto seleccionado', description: 'Usar solamente la parte marcada' },
      { id: 'path', label: '$(folder-opened) Agregar archivos o carpetas', description: 'Sumar material útil para la tarea' },
      { id: 'git', label: '$(shield) Protección de cambios', description: 'Elegir cómo guardar y recuperar el trabajo' },
      { id: 'preset', label: '$(dashboard) Nivel de detalle', description: 'Rápido, estándar, detallado o a medida' },
      { id: 'roles', label: '$(organization) Equipo de Baldr', description: 'Elegir cómo se reparte el trabajo' },
      { id: 'profile-create', label: '$(tools) Crear una configuración avanzada', description: 'Elegir proveedor y modelo paso a paso' },
      { id: 'context', label: '$(sparkle) Ayuda adicional', description: 'Buscar información útil cuando haga falta' },
      { id: 'status', label: '$(refresh) Actualizar', description: 'Volver a cargar las tareas y su estado' },
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
      case 'git': await this.chooseSafetyMode(); break;
      case 'preset': await this.choosePreset(); break;
      case 'roles': await this.chooseRoleProfiles(); break;
      case 'profile-create': await this.createExecutionProfile(); break;
      case 'context': await this.chooseContextMode(); break;
      case 'status': await this.refresh(); break;
      case 'logs': this.output.show(true); break;
      default: break;
    }
  }

  private async openChip(chip: string): Promise<void> {
    if (chip === 'git') await this.chooseSafetyMode();
    else if (chip === 'preset') await this.choosePreset();
    else if (chip === 'context') await this.chooseContextMode();
    else if (chip === 'roles') await this.chooseRoleProfiles();
  }

  private async chooseSafetyMode(): Promise<void> {
    const preference = record(record(this.lastStatus.workbench).preferences);
    const current = text(preference.safety_mode, 'automatic');
    const selected = await vscode.window.showQuickPick([
      { id: 'automatic', label: '$(shield) Protección automática', description: 'Recomendada y predeterminada: trabaja en una copia protegida y recuperable' },
      { id: 'current', label: 'Trabajar directamente', description: 'Modifica esta carpeta y usa su repositorio Git para revisar los cambios' },
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
      { id: 'on', label: 'Siempre activa', description: 'Busca información adicional para cada tarea' },
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
        id: 'codex-models',
        label: '$(sparkle) Elegir modelos y variantes de Codex',
        description: 'Sol, Terra, Luna y los niveles disponibles para cada uno',
      },
      {
        id: 'saved',
        label: '$(bookmark) Usar una configuración guardada',
        description: 'Elegir entre las opciones que ya tenés preparadas',
      },
      {
        id: 'advanced',
        label: '$(settings-gear) Crear una configuración avanzada',
        description: 'Elegir otro proveedor o escribir los datos manualmente',
      },
    ], {
      title: 'Equipo de Baldr',
      placeHolder: 'Elegí cómo querés configurar el equipo',
      ignoreFocusOut: true,
    });
    if (choice?.id === 'codex-models') await this.chooseCodexTeamModels();
    else if (choice?.id === 'saved') await this.chooseSavedRoleProfiles();
    else if (choice?.id === 'advanced') await this.createExecutionProfile();
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
        description: [text(value.provider), text(value.model), text(value.reasoning_effort)].filter(Boolean).join(' · '),
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
    await this.withProgress('Guardando la configuración…', () => this.runtime.upsertExecutionProfile(workspaceRoot(), {
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
      { id: 'mark_failed', label: 'Dar la tarea por fallida', description: 'Detener la recuperación y conservar los detalles' },
    ].filter((option) => actions.includes(option.id));
    if (!options.length) {
      void vscode.window.showInformationMessage('Esta tarea no necesita ninguna acción de recuperación.');
      return;
    }
    const selected = await vscode.window.showQuickPick(options, {
      title: 'Recuperar la tarea',
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
    this.launchOperation('Recuperando la tarea…', () => this.runtime.reconcileWorkItem(root, itemId, selected.id));
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
    const root = workspaceRoot();
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
:root { color-scheme: light dark; }
* { box-sizing: border-box; }
body { margin: 0; height: 100vh; overflow: hidden; color: var(--vscode-foreground); background: var(--vscode-sideBar-background); font: 13px/1.4 var(--vscode-font-family); }
button, textarea { font: inherit; }
button { color: inherit; -webkit-tap-highlight-color: transparent; }
button:focus-visible { outline: 1px solid var(--vscode-focusBorder); outline-offset: 1px; }
#root { height: 100%; display: grid; grid-template-rows: auto minmax(0, 1fr) auto; }
.header { padding: 12px 16px 8px; border-bottom: 1px solid var(--vscode-sideBar-border, transparent); }
.header-row { display: flex; align-items: center; justify-content: space-between; gap: 8px; }
.header-actions { display: flex; align-items: center; justify-content: center; gap: 2px; }
.heading { font-size: 11px; font-weight: 700; letter-spacing: .08em; text-transform: uppercase; opacity: .8; }
.icon-button { display: inline-grid; place-items: center; border: 0; background: transparent; border-radius: 6px; width: 28px; height: 28px; padding: 0; cursor: pointer; color: var(--vscode-icon-foreground, var(--vscode-foreground)); opacity: .82; }
.icon-button:hover { background: var(--vscode-toolbar-hoverBackground); opacity: 1; }
.button-icon { display: block; width: 16px; height: 16px; fill: currentColor; pointer-events: none; }
.task-list { margin-top: 5px; max-height: 168px; overflow: auto; }
.view-all { width: 100%; border: 0; background: transparent; color: var(--vscode-textLink-foreground); text-align: left; padding: 5px 4px; cursor: pointer; font-size: 11px; }
.view-all:hover { text-decoration: underline; }
.task-empty { padding: 8px 4px; color: var(--vscode-descriptionForeground); }
.task { width: 100%; display: grid; grid-template-columns: 13px minmax(0,1fr) auto; gap: 7px; align-items: center; text-align: left; border: 0; padding: 7px 4px; background: transparent; border-radius: 5px; cursor: pointer; }
.task:hover, .task.selected { background: var(--vscode-list-hoverBackground); }
.task.selected { outline: 1px solid var(--vscode-focusBorder); }
.task-title { white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.dot { width: 7px; height: 7px; border-radius: 50%; background: var(--vscode-descriptionForeground); }
.dot.running { background: var(--vscode-progressBar-background); box-shadow: 0 0 0 2px color-mix(in srgb, var(--vscode-progressBar-background) 25%, transparent); }
.dot.completed { background: var(--vscode-testing-iconPassed); }
.dot.failed, .dot.needs_attention { background: var(--vscode-testing-iconFailed); }
.dot.draft, .dot.cancelled { background: var(--vscode-descriptionForeground); }
.status-text { font-size: 10px; opacity: .65; }
.content { min-height: 0; overflow: auto; padding: 16px; position: relative; }
.empty { min-height: 100%; display: grid; place-items: center; text-align: center; color: var(--vscode-descriptionForeground); }
.empty-inner { max-width: 330px; line-height: 1.5; }
.empty-mark { display: grid; place-items: center; margin-bottom: 14px; color: var(--vscode-descriptionForeground); opacity: .72; }
.empty-logo { display: block; width: 34px; height: 34px; fill: none; stroke: currentColor; stroke-width: 1.5; stroke-linecap: round; stroke-linejoin: round; }
.empty-title { color: var(--vscode-foreground); font-weight: 600; margin-bottom: 3px; }
.empty-detail { color: var(--vscode-descriptionForeground); }
.item-title { font-size: 15px; font-weight: 600; margin: 0 0 3px; }
.item-meta { display: flex; flex-wrap: wrap; gap: 6px; font-size: 11px; color: var(--vscode-descriptionForeground); }
.task-body { margin: 12px 0; white-space: pre-wrap; line-height: 1.45; }
.timeline { border-left: 1px solid var(--vscode-widget-border); margin: 14px 0 10px 6px; padding-left: 14px; }
.phase { position: relative; padding: 0 0 14px; }
.phase::before { content: ''; position: absolute; width: 8px; height: 8px; border-radius: 50%; left: -19px; top: 3px; background: var(--vscode-descriptionForeground); }
.phase.approved::before, .phase.succeeded::before, .phase.completed::before { background: var(--vscode-testing-iconPassed); }
.phase.running::before { background: var(--vscode-progressBar-background); }
.phase.failed::before, .phase.blocked::before, .phase.needs_changes::before { background: var(--vscode-testing-iconFailed); }
.phase-name { font-weight: 600; text-transform: capitalize; }
.phase-detail { font-size: 11px; color: var(--vscode-descriptionForeground); margin-top: 2px; }
.notice { border: 1px solid var(--vscode-inputValidation-warningBorder); background: var(--vscode-inputValidation-warningBackground); padding: 9px; border-radius: 6px; margin: 10px 0; }
.actions { display: flex; flex-wrap: wrap; align-items: center; justify-content: flex-start; gap: 8px; margin-top: 14px; }
.action { display: inline-flex; min-height: 28px; align-items: center; justify-content: center; border: 1px solid var(--vscode-button-border, transparent); border-radius: 6px; padding: 4px 10px; cursor: pointer; background: var(--vscode-button-secondaryBackground); color: var(--vscode-button-secondaryForeground); }
.action:hover { background: var(--vscode-button-secondaryHoverBackground, var(--vscode-toolbar-hoverBackground)); }
.action.primary { background: var(--vscode-button-background); color: var(--vscode-button-foreground); }
.composer { position: relative; padding: 10px 16px 12px; background: var(--vscode-sideBar-background); }
.input-shell { border: 1px solid var(--vscode-input-border, var(--vscode-widget-border)); background: var(--vscode-input-background); border-radius: 16px; padding: 10px 11px 9px; box-shadow: 0 3px 12px rgba(0,0,0,.12); transition: border-color 120ms ease, box-shadow 120ms ease; }
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
.attachment { position: relative; display: grid; grid-template-columns: 30px minmax(0, 1fr); align-items: center; width: min(170px, calc(50% - 4px)); min-width: 120px; min-height: 48px; gap: 8px; padding: 7px 24px 7px 8px; border: 1px solid var(--vscode-widget-border); border-radius: 10px; background: color-mix(in srgb, var(--vscode-sideBar-background) 58%, var(--vscode-input-background)); }
.attachment-icon { display: grid; place-items: center; width: 30px; height: 30px; border-radius: 7px; background: var(--vscode-toolbar-hoverBackground); color: var(--vscode-descriptionForeground); font-size: 15px; }
.attachment-label { min-width: 0; overflow: hidden; color: var(--vscode-foreground); font-size: 11px; line-height: 1.3; text-overflow: ellipsis; white-space: nowrap; }
.attachment-kind { color: var(--vscode-descriptionForeground); font-size: 10px; }
.remove-attachment { position: absolute; top: 4px; right: 4px; display: grid; place-items: center; width: 18px; height: 18px; padding: 0; border: 0; border-radius: 50%; background: transparent; color: var(--vscode-descriptionForeground); cursor: pointer; }
.remove-attachment:hover { color: var(--vscode-foreground); background: var(--vscode-toolbar-hoverBackground); }
.plus-menu { position: absolute; left: 16px; right: 16px; bottom: calc(100% + 8px); display: none; max-height: min(520px, calc(100vh - 150px)); overflow: auto; padding: 10px; border: 1px solid var(--vscode-widget-border); border-radius: 12px; background: var(--vscode-quickInput-background); box-shadow: 0 -3px 18px var(--vscode-widget-shadow); z-index: 20; }
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
.slash { position: absolute; left: 10px; right: 10px; bottom: 77px; max-height: 220px; overflow: auto; border: 1px solid var(--vscode-widget-border); background: var(--vscode-quickInput-background); box-shadow: 0 5px 18px var(--vscode-widget-shadow); border-radius: 7px; z-index: 10; display: none; }
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
  .plus-menu { left: 10px; right: 10px; }
  .composer-row { gap: 5px; }
  .chip { padding-left: 5px; padding-right: 5px; }
  .chip[data-chip="context"] { display: none; }
}
@media (max-width: 300px) {
  .chip[data-chip="preset"] { display: none; }
}
@media (prefers-reduced-motion: reduce) {
  .loading { animation: none; }
  .input-shell { transition: none; }
}
</style>
</head>
<body>
<div id="root">
  <section class="header">
    <div class="header-row">
      <div class="heading">Tus tareas</div>
      <div class="header-actions">
        <button type="button" class="icon-button" id="refresh" title="Actualizar" aria-label="Actualizar tareas"><svg class="button-icon" viewBox="0 0 16 16" aria-hidden="true"><path fill="none" stroke="currentColor" stroke-width="1.2" stroke-linecap="round" stroke-linejoin="round" d="M13 8a5 5 0 1 1-1.46-3.54L13 5.92M13 3v3h-3"/></svg></button>
        <button type="button" class="icon-button" id="configure" title="Opciones de Baldr" aria-label="Opciones de Baldr"><svg class="button-icon" viewBox="0 0 16 16" aria-hidden="true"><path fill="none" stroke="currentColor" stroke-width="1.15" stroke-linejoin="round" d="M6.8 2h2.4l.42 1.55 1.25.72 1.55-.42 1.2 2.08-1.13 1.13v1.45l1.13 1.13-1.2 2.08-1.55-.42-1.25.72L9.2 13.6H6.8l-.42-1.58-1.25-.72-1.55.42-1.2-2.08 1.13-1.13V7.06L2.38 5.93l1.2-2.08 1.55.42 1.25-.72L6.8 2Z"/><circle cx="8" cy="7.79" r="2.15" fill="none" stroke="currentColor" stroke-width="1.15"/></svg></button>
      </div>
    </div>
    <div class="task-list" id="tasks"></div>
  </section>
  <main class="content" id="content"><div class="loading" id="loading"></div></main>
  <section class="composer">
    <div class="input-shell">
      <div class="attachments" id="attachments"></div>
      <textarea id="input" rows="2" placeholder="Escribí qué necesitás…" aria-label="Nueva tarea para Baldr"></textarea>
      <div class="composer-row">
        <button type="button" class="plus" id="plus" title="Agregar archivos u opciones" aria-label="Agregar archivos u opciones" aria-expanded="false" aria-controls="plusMenu"><svg class="button-icon" viewBox="0 0 16 16" aria-hidden="true"><path fill="none" stroke="currentColor" stroke-width="1.35" stroke-linecap="round" d="M8 3v10M3 8h10"/></svg></button>
        <div class="chips" aria-label="Opciones de la tarea">
          <button type="button" class="chip" data-chip="git" id="gitChip"><svg class="chip-icon" viewBox="0 0 16 16" aria-hidden="true"><path fill="none" stroke="currentColor" stroke-width="1.15" stroke-linejoin="round" d="M8 1.8 12.8 3.4v3.45c0 3.15-1.9 5.65-4.8 7.35-2.9-1.7-4.8-4.2-4.8-7.35V3.4L8 1.8Z"/></svg><span id="gitChipLabel">Protección automática</span></button>
          <button type="button" class="chip" data-chip="preset" id="presetChip"><svg class="chip-icon" viewBox="0 0 16 16" aria-hidden="true"><path fill="none" stroke="currentColor" stroke-width="1.15" stroke-linecap="round" d="M3 4h10M3 8h10M3 12h10M6 2.8v2.4M10 6.8v2.4M7 10.8v2.4"/></svg><span id="presetChipLabel">Estándar</span></button>
          <button type="button" class="chip" data-chip="roles" id="rolesChip"><svg class="chip-icon" viewBox="0 0 16 16" aria-hidden="true"><path fill="none" stroke="currentColor" stroke-width="1.15" stroke-linecap="round" stroke-linejoin="round" d="M6.2 7a2.2 2.2 0 1 0 0-4.4A2.2 2.2 0 0 0 6.2 7ZM2.5 13.2c.25-2.55 1.5-3.8 3.7-3.8s3.45 1.25 3.7 3.8M10.2 3.2a2 2 0 0 1 0 3.8M10.9 9.5c1.55.25 2.4 1.45 2.6 3.4"/></svg><span id="rolesChipLabel">Equipo estándar</span></button>
          <button type="button" class="chip" data-chip="context" id="contextChip"><svg class="chip-icon" viewBox="0 0 16 16" aria-hidden="true"><path fill="none" stroke="currentColor" stroke-width="1.15" stroke-linecap="round" stroke-linejoin="round" d="M8 1.8c.35 2.65 1.55 3.85 4.2 4.2C9.55 6.35 8.35 7.55 8 10.2 7.65 7.55 6.45 6.35 3.8 6 6.45 5.65 7.65 4.45 8 1.8ZM12.2 10.2c.18 1.35.85 2.02 2.2 2.2-1.35.18-2.02.85-2.2 2.2-.18-1.35-.85-2.02-2.2-2.2 1.35-.18 2.02-.85 2.2-2.2Z"/></svg><span id="contextChipLabel">Ayuda automática</span></button>
        </div>
        <button type="button" class="send" id="send" title="Enviar tarea" aria-label="Enviar tarea" disabled><svg class="button-icon" viewBox="0 0 16 16" aria-hidden="true"><path fill="none" stroke="currentColor" stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round" d="M8 12.8V3.2M4.4 6.8 8 3.2l3.6 3.6"/></svg></button>
      </div>
    </div>
    <div class="slash" id="slash"></div>
    <div class="plus-menu" id="plusMenu" role="dialog" aria-label="Agregar detalles u opciones de Baldr" aria-hidden="true">
      <div class="plus-menu-heading" data-plus-heading="add">Agregar</div>
      <button type="button" class="plus-option" data-plus-action="path" data-plus-group="add"><span class="plus-option-icon">⌕</span><span><span class="plus-option-label">Archivos y carpetas</span><span class="plus-option-detail">Sumá material útil para la tarea</span></span></button>
      <button type="button" class="plus-option" data-plus-action="file" data-plus-group="add"><span class="plus-option-icon">▣</span><span><span class="plus-option-label">Archivo abierto</span><span class="plus-option-detail">Usalo como referencia</span></span></button>
      <button type="button" class="plus-option" data-plus-action="selection" data-plus-group="add"><span class="plus-option-icon">≡</span><span><span class="plus-option-label">Texto seleccionado</span><span class="plus-option-detail">Sumá solo la parte marcada</span></span></button>
      <button type="button" class="plus-option" data-plus-action="draft" data-plus-group="add"><span class="plus-option-icon">＋</span><span><span class="plus-option-label">Guardar para después</span><span class="plus-option-detail">Creá una tarea sin empezarla todavía</span></span></button>
      <div class="plus-menu-group" data-plus-heading="preferences">Preferencias</div>
      <button type="button" class="plus-option" data-plus-action="git" data-plus-group="preferences"><span class="plus-option-icon">⌘</span><span><span class="plus-option-label">Protección de cambios</span><span class="plus-option-detail">Elegí cómo guardar y recuperar el trabajo</span></span></button>
      <button type="button" class="plus-option" data-plus-action="preset" data-plus-group="preferences"><span class="plus-option-icon">◈</span><span><span class="plus-option-label">Nivel de detalle</span><span class="plus-option-detail">Rápido, estándar o detallado</span></span></button>
      <button type="button" class="plus-option" data-plus-action="roles" data-plus-group="preferences"><span class="plus-option-icon">◌</span><span><span class="plus-option-label">Equipo de Baldr</span><span class="plus-option-detail">Elegí modelos y cómo se reparte el trabajo</span></span></button>
      <button type="button" class="plus-option" data-plus-action="context" data-plus-group="preferences"><span class="plus-option-icon">?</span><span><span class="plus-option-label">Ayuda adicional</span><span class="plus-option-detail">Buscá información útil cuando haga falta</span></span></button>
      <div class="plus-empty" id="plusEmpty" hidden>No encontramos una opción con ese nombre.</div>
      <input class="plus-filter" id="plusFilter" type="search" placeholder="Buscar opciones" aria-label="Buscar opciones de Baldr">
    </div>
  </section>
</div>
<script nonce="${nonce}">
const vscode = acquireVsCodeApi();
const els = Object.fromEntries(['tasks','content','input','send','plus','configure','refresh','gitChip','gitChipLabel','presetChip','presetChipLabel','rolesChip','rolesChipLabel','contextChip','contextChipLabel','attachments','slash','plusMenu','plusFilter','plusEmpty','loading'].map(id => [id, document.getElementById(id)]));
let state = {}; let slashIndex = 0; let showAllTasks = false; let plusMenuOpen = false;
const escapeHtml = value => String(value ?? '').replace(/[&<>"']/g, ch => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[ch]));
const post = message => vscode.postMessage(message);
const workbench = () => state.workbench || {};
const selected = () => workbench().selected || null;
const commands = () => ((workbench().options || {}).slash_commands || [
 {id:'setup',usage:'/setup',description:'Abrir las opciones de Baldr.'},{id:'new',usage:'/new <tarea>',description:'Guardar una tarea para después.'},{id:'run',usage:'/run [tarea]',description:'Empezar la tarea seleccionada.'},{id:'status',usage:'/status',description:'Actualizar el estado.'},{id:'profile',usage:'/profile <nivel>',description:'Cambiar el nivel de detalle.'},{id:'git',usage:'/git <modo>',description:'Cambiar la protección de cambios.'},{id:'context',usage:'/context <modo>',description:'Configurar la ayuda adicional.'},{id:'roles',usage:'/roles',description:'Configurar el equipo de Baldr.'},{id:'cancel',usage:'/cancel',description:'Cancelar la tarea seleccionada.'},{id:'resume',usage:'/resume',description:'Continuar una tarea interrumpida.'},{id:'archive',usage:'/archive',description:'Archivar la tarea seleccionada.'},{id:'help',usage:'/help',description:'Ver los comandos disponibles.'}
]);
function statusClass(status){ return String(status || 'draft').replace(/[^a-z_]/g,'_'); }
function statusLabel(status){ const labels={draft:'Pendiente',queued:'En espera',running:'En curso',cancelling:'Cancelando',completed:'Lista',archived:'Archivada',failed:'Necesita atención',cancelled:'Cancelada',needs_attention:'Necesita atención'}; return labels[String(status || 'draft')] || String(status || 'Pendiente'); }
function presetLabel(value){ return ({fast:'Rápido',balanced:'Estándar',deep:'Detallado',custom:'A medida'}[value]||'Estándar'); }
function safetyLabel(value){ return ({automatic:'Protección automática',worktree:'Protección automática',current:'Trabajar directamente','non-git':'Sin protección'}[value]||'Protección automática'); }
function contextLabel(value){ return ({auto:'Ayuda automática',on:'Ayuda activa',off:'Ayuda desactivada'}[value]||'Ayuda automática'); }
function itemErrorMessage(item){ if(item.error_code==='workspace_reconciliation_required'&&item.safety_mode==='non-git')return 'La tarea se detuvo al intentar crear un respaldo Git que esta carpeta no usa. Tus archivos siguen en la carpeta: revisá las opciones para continuar con ellos.'; return item.error_reason||item.error_code||''; }
function phaseLabel(value){ return ({architecture:'Planificación',architect:'Planificación',implementation:'Ejecución',implementer:'Ejecución',review:'Revisión',reviewer:'Revisión'}[String(value||'').toLowerCase()]||String(value||'Etapa')); }
function emptyMark(){ return '<div class="empty-mark"><svg class="empty-logo" viewBox="0 0 24 24" aria-hidden="true"><path d="M6 20V5.5h6.1c3 0 5 1.5 5 4 0 1.8-1 3-2.5 3.6 2 .5 3.4 1.8 3.4 3.8 0 2.4-2 3.1-5.4 3.1H6Z"/><path d="M9.3 8.5h2.8c1.2 0 1.8.4 1.8 1.2s-.6 1.3-1.8 1.3H9.3V8.5Zm0 5.5h3.3c1.4 0 2.1.5 2.1 1.4 0 1-.7 1.5-2.1 1.5H9.3V14Z"/></svg></div>'; }
function renderTasks(){
 const allItems = workbench().items || []; const items = showAllTasks ? allItems : allItems.slice(0,8); const current = selected()?.id;
 const rows = items.map(item => '<button class="task '+(item.id===current?'selected':'')+'" data-item="'+escapeHtml(item.id)+'"><span class="dot '+statusClass(item.status)+'"></span><span class="task-title">'+escapeHtml(item.title || item.task || 'Sin título')+'</span><span class="status-text">'+escapeHtml(statusLabel(item.status))+'</span></button>').join('');
 const more = allItems.length > 8 ? '<button class="view-all" id="viewAll">'+(showAllTasks?'Ver los más recientes':'Ver todos ('+allItems.length+')')+'</button>' : '';
 els.tasks.innerHTML = allItems.length ? rows + more : '<div class="task-empty">Todavía no hay tareas</div>';
 els.tasks.querySelectorAll('[data-item]').forEach(node => node.addEventListener('click', () => post({type:'select', itemId:node.dataset.item})));
 document.getElementById('viewAll')?.addEventListener('click',()=>{showAllTasks=!showAllTasks;renderTasks();});
}
function phaseHtml(phase){
 const participants = (phase.participants || []).map(p => escapeHtml([p.profile,p.model||p.agent,p.status].filter(Boolean).join(' · '))).join('<br>');
 return '<div class="phase '+statusClass(phase.status)+'"><div class="phase-name">'+escapeHtml(phaseLabel(phase.phase || phase.key))+'</div><div class="phase-detail">'+escapeHtml(statusLabel(phase.status))+(participants?'<br>'+participants:'')+'</div></div>';
}
function actionButtons(item){ const actions = item.allowed_actions || []; const rows=[];
 if(actions.includes('start')) rows.push('<button class="action primary" data-action="start">Empezar</button>');
 if(actions.includes('cancel')) rows.push('<button class="action" data-action="cancel">Cancelar</button>');
 if(actions.some(a => ['inspect_shadow','continue_from_shadow','apply_shadow_changes','discard_shadow','resume_from_checkpoint','accept_existing_changes','discard_worktree','mark_failed'].includes(a))) rows.push('<button class="action primary" data-action="reconcile">Revisar opciones</button>');
 if(actions.includes('archive')) rows.push('<button class="action" data-action="archive">Archivar</button>');
 rows.push('<button class="action" data-action="logs">Ver detalles</button>'); return rows.join(''); }
function renderContent(){
 const item=selected();
 if(state.emptyWorkspace){ els.content.innerHTML='<div class="empty"><div class="empty-inner">'+emptyMark()+'<div class="empty-title">Abrí una carpeta para empezar</div><div class="empty-detail">Baldr necesita una carpeta donde guardar el trabajo.</div></div></div>'; return; }
 if(state.trusted===false){ els.content.innerHTML='<div class="empty"><div class="empty-inner">'+emptyMark()+'<div class="empty-title">Falta tu permiso</div><div class="empty-detail">Autorizá esta carpeta para que Baldr pueda trabajar.</div><p><button type="button" class="action primary" id="trust">Revisar permisos</button></p></div></div>'; document.getElementById('trust')?.addEventListener('click',()=>post({type:'requestTrust'})); return; }
 if(!item){ els.content.innerHTML='<div class="empty"><div class="empty-inner">'+emptyMark()+'<div class="empty-title">¿Qué querés hacer?</div><div class="empty-detail">Escribí lo que necesitás. Baldr lo organiza y te muestra el avance.</div></div></div>'; return; }
 const error = itemErrorMessage(item); const phases=(item.phases||[]).map(phaseHtml).join('');
 els.content.innerHTML='<h2 class="item-title">'+escapeHtml(item.title || 'Sin título')+'</h2><div class="item-meta"><span>'+escapeHtml(statusLabel(item.status))+'</span><span>'+escapeHtml(presetLabel(item.preset))+'</span><span>'+escapeHtml(safetyLabel(item.safety_mode))+'</span></div>'+(error?'<div class="notice">'+escapeHtml(error)+'</div>':'')+'<div class="task-body">'+escapeHtml(item.task || '')+'</div>'+(phases?'<div class="timeline">'+phases+'</div>':'')+'<div class="actions">'+actionButtons(item)+'</div><div class="loading '+(state.busy?'visible':'')+'" id="loading"></div>';
 els.content.querySelectorAll('[data-action]').forEach(node => node.addEventListener('click',()=>{ const action=node.dataset.action; if(action==='logs')post({type:'openLogs'}); else post({type:'itemAction',action,itemId:item.id}); }));
}
function shortModelLabel(value){ const raw=String(value||'').trim(); const named=raw.match(/^gpt-[0-9]+(?:[.][0-9]+)*-(sol|terra|luna|spark)$/i); if(named)return named[1].charAt(0).toUpperCase()+named[1].slice(1).toLowerCase(); const version=raw.match(/^gpt-([0-9]+(?:[.][0-9]+)*)(?:-(mini))?$/i); if(version)return 'GPT-'+version[1]+(version[2]?' Mini':''); return raw||''; }
function effortChipLabel(value){ return ({minimal:'Mínimo',low:'Bajo',medium:'Medio',high:'Alto',xhigh:'Muy alto',max:'Máximo',ultra:'Ultra'}[String(value||'').toLowerCase()]||String(value||'')); }
function configuredRole(role){ const wb=workbench(); const pref=wb.preferences||{}; const profiles=wb.profiles||{}; const selected=(((pref.role_profiles||{})[role]||[])[0]); if(selected&&profiles.execution_profiles&&profiles.execution_profiles[selected])return profiles.execution_profiles[selected]; return (((profiles.resolved_roles||{})[role]||[])[0])||{}; }
function renderChips(){ const pref=workbench().preferences||{}; const safety=safetyLabel(pref.safety_mode); const preset=presetLabel(pref.preset); const context=contextLabel(pref.context_mode); const roleNames={architect:'Planificación',implementer:'Ejecución',reviewer:'Revisión'}; const roleConfigurations=['architect','implementer','reviewer'].map(role=>{const config=configuredRole(role);const raw=config.model||config.agent||config.provider||'';return {role,label:shortModelLabel(raw),effort:effortChipLabel(config.reasoning_effort||config.effort)};}); const modelNames=[...new Set(roleConfigurations.map(item=>item.label).filter(Boolean))]; const team=modelNames.length?modelNames.join(' · '):'Equipo estándar'; const teamDetail=roleConfigurations.filter(item=>item.label).map(item=>roleNames[item.role]+': '+item.label+(item.effort?' ('+item.effort+')':'')).join(' · '); els.gitChipLabel.textContent=safety; els.gitChip.title='Uso de Git y protección: '+safety; els.gitChip.setAttribute('aria-label',els.gitChip.title); els.presetChipLabel.textContent=preset; els.presetChip.title='Nivel de detalle: '+preset; els.presetChip.setAttribute('aria-label',els.presetChip.title); els.rolesChipLabel.textContent=team; els.rolesChip.title='Equipo de Baldr: '+(teamDetail||team); els.rolesChip.setAttribute('aria-label',els.rolesChip.title); els.contextChipLabel.textContent=context; els.contextChip.title='Ayuda adicional: '+context; els.contextChip.setAttribute('aria-label',els.contextChip.title); }
function renderPending(){ const items=(state.pending||{}).attachments||[]; const kindLabels={file:'Archivo',folder:'Carpeta',selection:'Selección'}; els.attachments.innerHTML=items.map((item,index)=>'<div class="attachment"><div class="attachment-icon" aria-hidden="true">'+({file:'▤',folder:'◇',selection:'≡'}[item.kind]||'▤')+'</div><div><div class="attachment-label" title="'+escapeHtml(item.label||'Archivo')+'">'+escapeHtml(item.label||'Archivo')+'</div><div class="attachment-kind">'+escapeHtml(kindLabels[item.kind]||'Archivo')+'</div></div><button type="button" class="remove-attachment" data-remove-pending="'+index+'" title="Quitar" aria-label="Quitar '+escapeHtml(item.label||'archivo')+'">×</button></div>').join(''); els.attachments.querySelectorAll('[data-remove-pending]').forEach(node=>node.addEventListener('click',()=>post({type:'removePending',index:Number(node.dataset.removePending)}))); }
function updateSendState(){ els.send.disabled=Boolean(state.busy)||!els.input.value.trim(); }
function render(){ renderTasks(); renderContent(); renderChips(); renderPending(); updateSendState(); }
function updateState(next){ state=next||{}; render(); }
function setPlusMenu(open){ plusMenuOpen=open; els.plusMenu.classList.toggle('visible',open); els.plusMenu.setAttribute('aria-hidden',String(!open)); els.plus.setAttribute('aria-expanded',String(open)); if(open){els.plusFilter.value='';filterPlusActions();els.plusMenu.scrollTop=0;} }
function normalizeSearch(value){ return String(value||'').normalize('NFD').replace(/[̀-ͯ]/g,'').toLowerCase(); }
function visiblePlusActions(){ return [...els.plusMenu.querySelectorAll('[data-plus-action]')].filter(node=>!node.hidden); }
function filterPlusActions(){ const query=normalizeSearch(els.plusFilter.value.trim()); const actions=[...els.plusMenu.querySelectorAll('[data-plus-action]')]; actions.forEach(node=>{node.hidden=Boolean(query)&&!normalizeSearch(node.textContent).includes(query);}); els.plusMenu.querySelectorAll('[data-plus-heading]').forEach(heading=>{const group=heading.dataset.plusHeading;heading.hidden=!actions.some(node=>node.dataset.plusGroup===group&&!node.hidden);}); els.plusEmpty.hidden=actions.some(node=>!node.hidden); }
function renderSlash(){ const value=els.input.value.trim(); if(!value.startsWith('/')){els.slash.classList.remove('visible');return;} const query=value.slice(1).toLowerCase(); const list=commands().filter(c=>c.id.startsWith(query.split(/\s/)[0])).slice(0,8); if(!list.length){els.slash.classList.remove('visible');return;} slashIndex=Math.min(slashIndex,list.length-1); els.slash.innerHTML=list.map((c,i)=>'<div class="slash-item '+(i===slashIndex?'active':'')+'" data-command="'+escapeHtml(c.id)+'"><span class="slash-command">/'+escapeHtml(c.id)+'</span><span class="slash-description">'+escapeHtml(c.description)+'</span></div>').join(''); els.slash.classList.add('visible'); els.slash.querySelectorAll('[data-command]').forEach(n=>n.addEventListener('click',()=>{els.input.value='/'+n.dataset.command+' ';els.input.focus();els.slash.classList.remove('visible');})); }
function submit(){ const value=els.input.value; if(!value.trim()||state.busy)return; setPlusMenu(false); post({type:'submit',value}); }
els.send.addEventListener('click',submit); els.plus.addEventListener('click',()=>setPlusMenu(!plusMenuOpen)); els.configure.addEventListener('click',()=>post({type:'configure'})); els.refresh.addEventListener('click',()=>post({type:'refresh'})); els.plusMenu.querySelectorAll('[data-plus-action]').forEach(node=>{node.addEventListener('click',()=>{setPlusMenu(false);post({type:'plusAction',action:node.dataset.plusAction});});node.addEventListener('keydown',event=>{if(event.key!=='ArrowDown'&&event.key!=='ArrowUp')return;event.preventDefault();const actions=visiblePlusActions();const index=actions.indexOf(node);const next=(index+(event.key==='ArrowDown'?1:-1)+actions.length)%actions.length;actions[next]?.focus();});}); els.plusFilter.addEventListener('input',filterPlusActions); els.plusFilter.addEventListener('keydown',event=>{if(event.key!=='Enter'&&event.key!=='ArrowDown'&&event.key!=='ArrowUp')return;const actions=visiblePlusActions();if(!actions.length)return;event.preventDefault();if(event.key==='Enter')actions[0].click();else if(event.key==='ArrowDown')actions[0].focus();else actions[actions.length-1].focus();}); document.querySelectorAll('[data-chip]').forEach(n=>n.addEventListener('click',()=>post({type:'chip',value:n.dataset.chip})));
els.input.addEventListener('input',()=>{els.input.style.height='auto';els.input.style.height=Math.min(150,els.input.scrollHeight)+'px';slashIndex=0;renderSlash();updateSendState();});
els.input.addEventListener('keydown',event=>{if(event.key==='Enter'&&!event.shiftKey&&!els.slash.classList.contains('visible')){event.preventDefault();submit();}else if(els.slash.classList.contains('visible')&&(event.key==='ArrowDown'||event.key==='ArrowUp')){event.preventDefault();slashIndex=Math.max(0,slashIndex+(event.key==='ArrowDown'?1:-1));renderSlash();}else if(els.slash.classList.contains('visible')&&event.key==='Tab'){event.preventDefault();const active=els.slash.querySelector('.active');if(active){els.input.value='/'+active.dataset.command+' ';els.slash.classList.remove('visible');}}});
document.addEventListener('keydown',event=>{if(event.key==='Escape'&&plusMenuOpen){event.preventDefault();setPlusMenu(false);els.plus.focus();}});
document.addEventListener('pointerdown',event=>{if(plusMenuOpen&&!els.plusMenu.contains(event.target)&&!els.plus.contains(event.target))setPlusMenu(false);});
window.addEventListener('message',event=>{const msg=event.data||{};if(msg.type==='state')updateState(msg.state);else if(msg.type==='loading')els.loading?.classList.toggle('visible',Boolean(msg.value));else if(msg.type==='operation'){state.busy=Boolean(msg.busy);render();}else if(msg.type==='pending'){state.pending=msg.pending;renderPending();}else if(msg.type==='clearInput'){els.input.value='';els.input.style.height='auto';renderSlash();updateSendState();}else if(msg.type==='prefill'){els.input.value=msg.value||'';els.input.focus();renderSlash();updateSendState();}else if(msg.type==='showHelp'){els.input.value='/';els.input.focus();renderSlash();updateSendState();}else if(msg.type==='error'){state.error=msg.message;render();}});
post({type:'ready'});
</script>
</body>
</html>`;
  }
}

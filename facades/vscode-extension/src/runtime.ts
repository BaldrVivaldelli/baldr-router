import { spawn, type ChildProcess } from 'node:child_process';
import * as fs from 'node:fs';
import * as path from 'node:path';
import * as vscode from 'vscode';

import { redactSensitive } from './redaction.js';
import { describeRuntimeInvocation } from './runtimeLog.js';

export const EXTENSION_VERSION = '0.19.0';
export const CONTEXT7_SECRET_KEY = 'baldr.context7ApiKey';
const PROVIDER_MODELS_CACHE_TTL_MS = 5 * 60 * 1000;

export type JsonRecord = Record<string, unknown>;

export interface FacadeOptions {
  workspaceRoot?: string;
  task?: string;
  recentLimit?: number;
  workItemLimit?: number;
  workItemId?: string;
  includeArchived?: boolean;
  dryRun?: boolean;
  action?: string;
  title?: string;
  extraContext?: string;
  attachments?: JsonRecord[];
  safetyMode?: 'automatic' | 'worktree' | 'current' | 'non-git';
  preset?: 'fast' | 'balanced' | 'deep' | 'custom';
  contextMode?: 'auto' | 'on' | 'off';
  roleProfiles?: Record<string, string[]>;
  allowNonGit?: boolean;
  rememberWorkspace?: boolean;
  reconciliationAction?: string;
  cancelReason?: string;
  profileDefinition?: JsonRecord;
  itemConfig?: JsonRecord;
  trustWorkspace?: boolean;
  phaseStage?: 'planning' | 'execution' | 'review';
  phaseRound?: number;
  phaseRunOrdinal?: number;
  phaseCursor?: string;
  phasePageSize?: number;
  deliverableCursor?: string;
  deliverablePageSize?: number;
}

function wslDistroFromPath(value: string | undefined): string {
  const match = String(value ?? '').match(/^\\\\wsl(?:\.localhost|\$)\\([^\\/]+)[\\/]/i);
  return match?.[1] ?? '';
}

function hostName(): string {
  if (vscode.env.remoteName === 'wsl') return 'wsl';
  if (process.platform === 'win32') return 'windows';
  if (process.platform === 'darwin') return 'macos';
  return 'linux';
}

async function terminateChildTree(child: ChildProcess): Promise<void> {
  if (!child.pid || child.exitCode !== null || child.killed) return;
  if (process.platform === 'win32') {
    await new Promise<void>((resolve) => {
      const killer = spawn('taskkill.exe', ['/PID', String(child.pid), '/T', '/F'], {
        windowsHide: true,
        stdio: 'ignore',
      });
      killer.once('error', () => {
        try { child.kill('SIGKILL'); } catch { /* best effort */ }
        resolve();
      });
      killer.once('close', () => resolve());
    });
    return;
  }
  try {
    process.kill(-child.pid, 'SIGTERM');
  } catch {
    try { child.kill('SIGTERM'); } catch { /* best effort */ }
  }
  await new Promise((resolve) => setTimeout(resolve, 350));
  if (child.exitCode === null) {
    try {
      process.kill(-child.pid, 'SIGKILL');
    } catch {
      try { child.kill('SIGKILL'); } catch { /* best effort */ }
    }
  }
}

function pushFlag(args: string[], flag: string, value: unknown): void {
  if (value === undefined || value === null || value === '') return;
  args.push(flag, String(value));
}

export class BaldrRuntime {
  private readonly bootstrapPath: string;
  private readonly wheelPath: string;
  private readonly providerModelsCache = new Map<
    string,
    { expiresAt: number; value: JsonRecord }
  >();
  private readonly providerModelsRequests = new Map<string, Promise<JsonRecord>>();

  constructor(
    private readonly context: vscode.ExtensionContext,
    private readonly output: vscode.OutputChannel,
  ) {
    this.bootstrapPath = context.asAbsolutePath(path.join('runtime', 'baldr-bootstrap.mjs'));
    this.wheelPath = this.findBundledWheel();
  }

  private findBundledWheel(): string {
    const runtimeDir = this.context.asAbsolutePath(path.join('resources', 'runtime'));
    const candidates = fs.existsSync(runtimeDir)
      ? fs.readdirSync(runtimeDir).filter((name) => /^baldr_router-.*\.whl$/i.test(name)).sort()
      : [];
    if (candidates.length === 0) {
      return path.join(runtimeDir, 'baldr_router-0.19.0-py3-none-any.whl');
    }
    return path.join(runtimeDir, candidates.at(-1)!);
  }

  private workspaceRoots(): string[] {
    if (!vscode.workspace.isTrusted) return [];
    return (vscode.workspace.workspaceFolders ?? []).map((folder) => folder.uri.fsPath);
  }

  private requireTrustedWorkspace(workspaceRoot: string | undefined): void {
    if (workspaceRoot && !vscode.workspace.isTrusted) {
      throw new Error(
        'VS Code Workspace Trust is required before Baldr may trust or execute providers in this workspace.',
      );
    }
  }

  private async knownSecrets(): Promise<string[]> {
    const secret = await this.context.secrets.get(CONTEXT7_SECRET_KEY);
    return secret ? [secret] : [];
  }

  private async customEnvironment(workspaceRoot?: string): Promise<Record<string, string>> {
    const configuration = vscode.workspace.getConfiguration('baldr');
    let mode = configuration.get<string>('runtime.mode', 'auto');
    let distro = configuration.get<string>('runtime.wslDistro', '').trim();
    const activeFolder = vscode.window.activeTextEditor
      ? vscode.workspace.getWorkspaceFolder(vscode.window.activeTextEditor.document.uri)
      : undefined;
    const folders = vscode.workspace.workspaceFolders ?? [];
    const workspacePath = workspaceRoot
      ?? activeFolder?.uri.fsPath
      ?? (folders.length === 1 ? folders[0].uri.fsPath : undefined);
    const detectedDistro = process.platform === 'win32' ? wslDistroFromPath(workspacePath) : '';
    if (mode === 'auto' && detectedDistro) mode = 'wsl';
    if (!distro && detectedDistro) distro = detectedDistro;
    const secret = await this.context.secrets.get(CONTEXT7_SECRET_KEY);

    const env: Record<string, string> = {
      ELECTRON_RUN_AS_NODE: '1',
      BALDR_BUNDLED_WHEEL: this.wheelPath,
      BALDR_VSCODE_RUNTIME_DIR: this.context.globalStorageUri.fsPath,
      BALDR_ROUTER_AUTO_INSTALL: '1',
      BALDR_ROUTER_PREFER_MANAGED: '1',
      BALDR_ROUTER_LAUNCHER_MODE: mode,
      BALDR_TRUSTED_WORKSPACE_ROOTS_JSON: JSON.stringify(this.workspaceRoots()),
      BALDR_CLIENT_ID: 'vscode-extension',
      BALDR_CLIENT_VERSION: EXTENSION_VERSION,
    };
    if (distro) env.BALDR_ROUTER_WSL_DISTRO = distro;
    if (secret) env.CONTEXT7_API_KEY = secret;
    return env;
  }

  private async processEnvironment(workspaceRoot?: string): Promise<NodeJS.ProcessEnv> {
    return { ...process.env, ...(await this.customEnvironment(workspaceRoot)) };
  }

  async mcpDefinition(): Promise<vscode.McpStdioServerDefinition> {
    const definition = new vscode.McpStdioServerDefinition(
      'Baldr Router',
      process.execPath,
      [this.bootstrapPath, 'mcp'],
      await this.customEnvironment(),
      EXTENSION_VERSION,
    );
    definition.cwd = this.context.globalStorageUri;
    return definition;
  }

  async resolveMcpDefinition(
    definition: vscode.McpStdioServerDefinition,
    token: vscode.CancellationToken,
  ): Promise<vscode.McpStdioServerDefinition> {
    if (token.isCancellationRequested) throw new vscode.CancellationError();
    const ensured = await this.ensure(token);
    if (!ensured.ok) {
      throw new Error(String(ensured.reason ?? 'Baldr Router runtime could not be prepared.'));
    }
    definition.env = await this.customEnvironment();
    definition.version = EXTENSION_VERSION;
    return definition;
  }

  async ensure(token?: vscode.CancellationToken): Promise<JsonRecord> {
    return this.invokeBootstrapJson(['ensure'], token);
  }

  async detect(token?: vscode.CancellationToken): Promise<JsonRecord> {
    return this.invokeBootstrapJson(['detect'], token);
  }

  async runRouterJson(
    routerArgs: string[],
    token?: vscode.CancellationToken,
    allowFailure = false,
    quiet = false,
    workspaceRoot?: string,
  ): Promise<JsonRecord> {
    return this.invokeBootstrapJson(
      ['exec', '--', ...routerArgs],
      token,
      allowFailure,
      quiet,
      workspaceRoot,
    );
  }

  async runFacade(
    intent: 'setup' | 'status' | 'run',
    options: {
      workspaceRoot?: string;
      task?: string;
      recentLimit?: number;
      workItemLimit?: number;
      workbenchOnly?: boolean;
      dryRun?: boolean;
      workItemId?: string;
      workItemAction?: string;
      title?: string;
      workspaceMode?: 'automatic' | 'worktree' | 'current' | 'non-git';
      executionPreset?: 'fast' | 'balanced' | 'deep' | 'custom';
      contextMode?: 'auto' | 'on' | 'off';
      roleProfiles?: Record<string, string[]>;
      rememberWorkspace?: boolean;
      allowNonGit?: boolean;
      includeArchived?: boolean;
      attachments?: JsonRecord[];
      extraContext?: string;
      reconciliationAction?: string;
      cancelReason?: string;
      itemConfig?: JsonRecord;
      profileDefinition?: JsonRecord;
      phaseStage?: 'planning' | 'execution' | 'review';
      phaseRound?: number;
      phaseRunOrdinal?: number;
      phaseCursor?: string;
      phasePageSize?: number;
      deliverableCursor?: string;
      deliverablePageSize?: number;
    } = {},
    token?: vscode.CancellationToken,
    allowFailure = false,
  ): Promise<JsonRecord> {
    this.requireTrustedWorkspace(options.workspaceRoot);
    const args = ['facade', intent];
    if (intent === 'setup') {
      if (options.workspaceRoot) args.push(options.workspaceRoot);
      if (options.workspaceRoot && !options.workspaceMode) args.push('--trust-workspace');
      if (options.workspaceMode) args.push('--workspace-safety-mode', options.workspaceMode);
      if (options.executionPreset) args.push('--execution-preset', options.executionPreset);
      if (options.contextMode) args.push('--context-mode', options.contextMode);
      this.appendRoleProfiles(args, options.roleProfiles);
      if (options.allowNonGit) args.push('--allow-non-git');
      if (options.profileDefinition) {
        args.push('--profile-definition-json', JSON.stringify(options.profileDefinition));
      }
    } else if (intent === 'status') {
      if (options.workspaceRoot) args.push(options.workspaceRoot);
      args.push('--recent-limit', String(options.recentLimit ?? 5));
      args.push('--work-item-limit', String(options.workItemLimit ?? 100));
      if (options.workItemId) args.push('--work-item-id', options.workItemId);
      if (options.includeArchived) args.push('--include-archived');
      if (options.workbenchOnly) args.push('--workbench-only');
    } else {
      if (!options.workspaceRoot) throw new Error('An open workspace is required for /run.');
      args.push(options.workspaceRoot, options.task?.trim() ?? '');
      if (options.workItemAction) args.push('--work-item-action', options.workItemAction);
      if (options.workItemId) args.push('--work-item-id', options.workItemId);
      if (options.title) args.push('--title', options.title);
      if (options.workspaceMode) args.push('--workspace-mode', options.workspaceMode);
      if (options.executionPreset) args.push('--execution-preset', options.executionPreset);
      if (options.contextMode) args.push('--context-mode', options.contextMode);
      this.appendRoleProfiles(args, options.roleProfiles);
      if (options.rememberWorkspace) args.push('--remember-workspace');
      if (options.allowNonGit) args.push('--allow-non-git');
      if (options.attachments) args.push('--attachments-json', JSON.stringify(options.attachments));
      if (options.itemConfig) args.push('--item-config-json', JSON.stringify(options.itemConfig));
      if (options.extraContext) args.push('--extra-context', options.extraContext);
      if (options.reconciliationAction) {
        args.push('--reconciliation-action', options.reconciliationAction);
      }
      if (options.cancelReason) args.push('--cancel-reason', options.cancelReason);
      if (options.phaseStage) args.push('--phase-stage', options.phaseStage);
      if (options.phaseRound !== undefined) args.push('--phase-round', String(options.phaseRound));
      if (options.phaseRunOrdinal !== undefined) args.push('--phase-run-ordinal', String(options.phaseRunOrdinal));
      if (options.phaseCursor) args.push('--phase-cursor', options.phaseCursor);
      if (options.phasePageSize !== undefined) args.push('--phase-page-size', String(options.phasePageSize));
      if (options.deliverableCursor) args.push('--deliverable-cursor', options.deliverableCursor);
      if (options.deliverablePageSize !== undefined) args.push('--deliverable-page-size', String(options.deliverablePageSize));
      if (options.dryRun) args.push('--dry-run');
    }
    args.push('--client', 'vscode-extension');
    return this.runRouterJson(
      args,
      token,
      allowFailure,
      intent === 'status' && options.workbenchOnly === true,
      options.workspaceRoot,
    );
  }

  private appendRoleProfiles(
    args: string[],
    profiles: Record<string, string[]> | undefined,
  ): void {
    if (!profiles) return;
    for (const role of ['architect', 'implementer', 'reviewer']) {
      const selected = profiles[role]?.filter(Boolean);
      if (selected?.length) args.push('--role-profile', `${role}=${selected.join(',')}`);
    }
  }

  async workbenchStatus(
    workspaceRoot: string,
    workItemId?: string,
    token?: vscode.CancellationToken,
    includeArchived = false,
  ): Promise<JsonRecord> {
    return this.runFacade('status', {
      workspaceRoot,
      workItemId,
      recentLimit: 5,
      workItemLimit: 100,
      workbenchOnly: true,
      includeArchived,
    }, token, true);
  }

  async configureWorkbench(
    workspaceRoot: string,
    options: {
      workspaceMode?: 'automatic' | 'worktree' | 'current' | 'non-git';
      executionPreset?: 'fast' | 'balanced' | 'deep' | 'custom';
      contextMode?: 'auto' | 'on' | 'off';
      roleProfiles?: Record<string, string[]>;
      allowNonGit?: boolean;
      profileDefinition?: JsonRecord;
    },
    token?: vscode.CancellationToken,
  ): Promise<JsonRecord> {
    return this.runFacade('setup', { workspaceRoot, ...options }, token, true);
  }

  async createWorkItem(
    workspaceRoot: string,
    task: string,
    options: {
      title?: string;
      workspaceMode?: 'automatic' | 'worktree' | 'current' | 'non-git';
      executionPreset?: 'fast' | 'balanced' | 'deep' | 'custom';
      contextMode?: 'auto' | 'on' | 'off';
      roleProfiles?: Record<string, string[]>;
      allowNonGit?: boolean;
      rememberWorkspace?: boolean;
      attachments?: JsonRecord[];
      extraContext?: string;
    } = {},
    token?: vscode.CancellationToken,
  ): Promise<JsonRecord> {
    return this.runFacade('run', {
      workspaceRoot,
      task,
      workItemAction: 'create-item',
      ...options,
    }, token, true);
  }

  async startWorkItem(
    workspaceRoot: string,
    workItemId: string,
    token?: vscode.CancellationToken,
  ): Promise<JsonRecord> {
    return this.runFacade('run', {
      workspaceRoot,
      workItemId,
      workItemAction: 'start-item',
    }, token, true);
  }

  async continueWorkItem(
    workspaceRoot: string,
    workItemId: string,
    task: string,
    options: { attachments?: JsonRecord[]; extraContext?: string } = {},
    token?: vscode.CancellationToken,
  ): Promise<JsonRecord> {
    return this.runFacade('run', {
      workspaceRoot,
      workItemId,
      task,
      workItemAction: 'continue-item',
      attachments: options.attachments,
      extraContext: options.extraContext,
    }, token, true);
  }

  async cancelWorkItem(
    workspaceRoot: string,
    workItemId: string,
    reason = 'Cancellation requested from the Baldr Console.',
    token?: vscode.CancellationToken,
  ): Promise<JsonRecord> {
    return this.runFacade('run', {
      workspaceRoot,
      workItemId,
      workItemAction: 'cancel-item',
      cancelReason: reason,
    }, token, true);
  }

  async reconcileWorkItem(
    workspaceRoot: string,
    workItemId: string,
    action: string,
    token?: vscode.CancellationToken,
  ): Promise<JsonRecord> {
    return this.runFacade('run', {
      workspaceRoot,
      workItemId,
      workItemAction: 'reconcile-item',
      reconciliationAction: action,
    }, token, true);
  }

  async archiveWorkItem(
    workspaceRoot: string,
    workItemId: string,
    token?: vscode.CancellationToken,
  ): Promise<JsonRecord> {
    return this.runFacade('run', {
      workspaceRoot,
      workItemId,
      workItemAction: 'archive-item',
    }, token, true);
  }

  async restoreWorkItem(
    workspaceRoot: string,
    workItemId: string,
    token?: vscode.CancellationToken,
  ): Promise<JsonRecord> {
    return this.runFacade('run', {
      workspaceRoot,
      workItemId,
      workItemAction: 'restore-item',
    }, token, true);
  }

  async deleteWorkItem(
    workspaceRoot: string,
    workItemId: string,
    token?: vscode.CancellationToken,
  ): Promise<JsonRecord> {
    return this.runFacade('run', {
      workspaceRoot,
      workItemId,
      workItemAction: 'delete-item',
    }, token, true);
  }

  async inspectWorkItemPhase(
    workspaceRoot: string,
    workItemId: string,
    stage: 'planning' | 'execution' | 'review',
    round: number,
    options: { runOrdinal?: number; cursor?: string; pageSize?: number } = {},
    token?: vscode.CancellationToken,
  ): Promise<JsonRecord> {
    return this.runFacade('run', {
      workspaceRoot,
      workItemId,
      workItemAction: 'inspect-item-phase',
      phaseStage: stage,
      phaseRound: round,
      phaseRunOrdinal: options.runOrdinal,
      phaseCursor: options.cursor,
      phasePageSize: options.pageSize ?? 30,
    }, token, true);
  }

  async listWorkItemDeliverables(
    workspaceRoot: string,
    workItemId: string,
    options: { cursor?: string; pageSize?: number } = {},
    token?: vscode.CancellationToken,
  ): Promise<JsonRecord> {
    return this.runFacade('run', {
      workspaceRoot,
      workItemId,
      workItemAction: 'list-item-deliverables',
      deliverableCursor: options.cursor,
      deliverablePageSize: options.pageSize ?? 50,
    }, token, true);
  }


  async consoleStatus(
    workspaceRoot: string,
    workItemId?: string,
    token?: vscode.CancellationToken,
  ): Promise<JsonRecord> {
    return this.workbenchStatus(workspaceRoot, workItemId, token, true);
  }

  async setWorkspacePreferences(
    workspaceRoot: string,
    options: {
      safetyMode?: 'automatic' | 'worktree' | 'current' | 'non-git';
      preset?: 'fast' | 'balanced' | 'deep' | 'custom';
      contextMode?: 'auto' | 'on' | 'off';
      roleProfiles?: Record<string, string[]>;
      allowNonGit?: boolean;
    },
    token?: vscode.CancellationToken,
  ): Promise<JsonRecord> {
    return this.configureWorkbench(workspaceRoot, {
      workspaceMode: options.safetyMode,
      executionPreset: options.preset,
      contextMode: options.contextMode,
      roleProfiles: options.roleProfiles,
      allowNonGit: options.allowNonGit,
    }, token);
  }

  async upsertExecutionProfile(
    workspaceRoot: string | undefined,
    definition: JsonRecord,
    token?: vscode.CancellationToken,
  ): Promise<JsonRecord> {
    if (!workspaceRoot) throw new Error('Open a workspace before configuring Baldr profiles.');
    return this.configureWorkbench(workspaceRoot, {
      profileDefinition: definition,
    }, token);
  }

  async providerModels(
    provider = 'codex',
    token?: vscode.CancellationToken,
  ): Promise<JsonRecord> {
    const normalizedProvider = provider.trim().toLowerCase() || 'codex';
    const cached = this.providerModelsCache.get(normalizedProvider);
    if (cached && cached.expiresAt > Date.now()) return cached.value;
    if (cached) this.providerModelsCache.delete(normalizedProvider);

    const pending = this.providerModelsRequests.get(normalizedProvider);
    if (pending) return pending;

    const request = (async (): Promise<JsonRecord> => {
      try {
        const result = await this.runRouterJson(
          ['provider-models', normalizedProvider],
          token,
          true,
        );
        if (result.ok === true) {
          this.providerModelsCache.set(normalizedProvider, {
            expiresAt: Date.now() + PROVIDER_MODELS_CACHE_TTL_MS,
            value: result,
          });
        }
        return result;
      } finally {
        this.providerModelsRequests.delete(normalizedProvider);
      }
    })();
    this.providerModelsRequests.set(normalizedProvider, request);
    return request;
  }

  qualificationProfile(detection?: JsonRecord): string {
    if (vscode.env.remoteName === 'wsl') return 'vscode-remote-wsl';
    const target = record(detection?.target);
    if (process.platform === 'win32') {
      return target.kind === 'wsl' ? 'vscode-windows-wsl' : 'vscode-windows-native';
    }
    if (process.platform === 'linux') return 'vscode-linux-native';
    if (process.platform === 'darwin') return 'vscode-macos-native';
    throw new Error(`No signed qualification profile exists for platform ${process.platform}.`);
  }

  private qualificationDirectory(profile: string): string {
    return path.join(this.context.globalStorageUri.fsPath, 'qualification', profile);
  }

  async recordClientReceipt(token?: vscode.CancellationToken): Promise<JsonRecord> {
    const detection = await this.detect(token);
    const target = record(detection.target);
    const configuration = vscode.workspace.getConfiguration('baldr');
    const facts = {
      extension_host: hostName(),
      remote_name: vscode.env.remoteName ?? '',
      router_runtime: target.kind === 'wsl' ? 'wsl' : 'host',
      runtime_source: text(target.source),
      configured_mode: configuration.get<string>('runtime.mode', 'auto'),
      wsl_distro: text(target.distro),
      workspace_trusted: vscode.workspace.isTrusted,
      workspace_count: vscode.workspace.workspaceFolders?.length ?? 0,
      private_runtime: true,
      mcp_definition_provider: 'baldr.router',
      console_view: 'baldr.console',
    };
    return this.runRouterJson([
      'qualification', 'client-receipt',
      '--client', 'vscode-extension',
      '--client-version', EXTENSION_VERSION,
      '--facts-json', JSON.stringify(facts),
    ], token);
  }

  async runQualification(
    workspaceRoot: string,
    options: {
      includeProviderSmoke?: boolean;
      clientAssertions?: string;
      canaryResults?: string;
    } = {},
    token?: vscode.CancellationToken,
  ): Promise<JsonRecord> {
    this.requireTrustedWorkspace(workspaceRoot);
    const detection = await this.detect(token);
    const profile = this.qualificationProfile(detection);
    await this.recordClientReceipt(token);
    const templateDir = this.qualificationDirectory(profile);
    await fs.promises.mkdir(templateDir, { recursive: true });
    const assertionsPath = options.clientAssertions
      ?? path.join(templateDir, 'client-assertions.json');
    const canariesPath = options.canaryResults
      ?? path.join(templateDir, 'canary-results.json');
    if (!fs.existsSync(assertionsPath) || !fs.existsSync(canariesPath)) {
      await this.runRouterJson([
        'qualification', 'template',
        '--profile', profile,
        '--output-dir', templateDir,
        '--workspace-root', workspaceRoot,
      ], token);
    }
    const args = [
      'qualification', 'run',
      '--profile', profile,
      '--workspace-root', workspaceRoot,
      '--client-assertions', assertionsPath,
      '--canary-results', canariesPath,
      '--repeat', '3',
      '--client', 'vscode-extension',
    ];
    if (!options.includeProviderSmoke) args.push('--no-provider-smoke');
    const result = await this.runRouterJson(args, token);
    result.template_dir = templateDir;
    result.client_assertions_path = assertionsPath;
    result.canary_results_path = canariesPath;
    return result;
  }

  async configureContext7FromSecret(token?: vscode.CancellationToken): Promise<JsonRecord> {
    return this.runRouterJson(
      ['enable-context7-env', '--mode', 'hybrid', '--env-name', 'CONTEXT7_API_KEY', '--install-codex-mcp'],
      token,
      true,
    );
  }

  async configureContext7FromEnvironment(token?: vscode.CancellationToken): Promise<JsonRecord> {
    return this.configureContext7FromSecret(token);
  }

  async disableContext7(token?: vscode.CancellationToken): Promise<JsonRecord> {
    return this.runRouterJson(['disable-context7', '--remove-codex-mcp'], token, true);
  }

  private async invokeBootstrapJson(
    args: string[],
    token?: vscode.CancellationToken,
    allowFailure = false,
    quiet = false,
    workspaceRoot?: string,
  ): Promise<JsonRecord> {
    await fs.promises.mkdir(this.context.globalStorageUri.fsPath, { recursive: true });
    const result = await this.spawnCapture(args, token, quiet, workspaceRoot);
    const stdout = result.stdout.trim();
    if (!stdout) throw new Error(result.stderr.trim() || 'Baldr returned no JSON output.');
    let parsed: JsonRecord;
    try {
      const value: unknown = JSON.parse(stdout);
      if (!isRecord(value)) throw new Error('JSON root is not an object.');
      parsed = value;
    } catch (error) {
      const secrets = await this.knownSecrets();
      this.output.appendLine(`[parse-error] stdout: ${redactSensitive(stdout.slice(0, 4000), secrets)}`);
      throw new Error(`Baldr returned invalid JSON: ${error instanceof Error ? error.message : String(error)}`);
    }
    if (result.code !== 0 && !allowFailure) {
      throw new Error(
        text(parsed.reason)
        || text(record(parsed.error).message)
        || result.stderr.trim()
        || `Baldr exited with code ${result.code}.`,
      );
    }
    parsed.process_exit_code = result.code;
    return parsed;
  }

  private async spawnCapture(
    bootstrapArgs: string[],
    token?: vscode.CancellationToken,
    quiet = false,
    workspaceRoot?: string,
  ): Promise<{ stdout: string; stderr: string; code: number }> {
    const env = await this.processEnvironment(workspaceRoot);
    const secrets = await this.knownSecrets();
    if (!quiet) this.output.appendLine(describeRuntimeInvocation(bootstrapArgs));

    return new Promise((resolve, reject) => {
      const child = spawn(process.execPath, [this.bootstrapPath, ...bootstrapArgs], {
        env,
        windowsHide: true,
        detached: process.platform !== 'win32',
        cwd: this.context.globalStorageUri.fsPath,
        stdio: ['ignore', 'pipe', 'pipe'],
      });
      let stdout = '';
      let stderr = '';
      let settled = false;
      let cancellationRequested = false;
      const cancellation = token?.onCancellationRequested(() => {
        cancellationRequested = true;
        void terminateChildTree(child);
      });

      child.stdout.setEncoding('utf8');
      child.stderr.setEncoding('utf8');
      child.stdout.on('data', (chunk: string) => { stdout += chunk; });
      child.stderr.on('data', (chunk: string) => {
        stderr += chunk;
        this.output.append(redactSensitive(chunk, secrets));
      });
      child.on('error', (error) => {
        if (settled) return;
        settled = true;
        cancellation?.dispose();
        reject(new Error(redactSensitive(error.message, secrets)));
      });
      child.on('close', (code) => {
        if (settled) return;
        settled = true;
        cancellation?.dispose();
        stdout = redactSensitive(stdout, secrets);
        stderr = redactSensitive(stderr, secrets);
        if (cancellationRequested || token?.isCancellationRequested) {
          reject(new vscode.CancellationError());
          return;
        }
        resolve({ stdout, stderr, code: code ?? -1 });
      });
    });
  }
}

export function isRecord(value: unknown): value is JsonRecord {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

export function record(value: unknown): JsonRecord {
  return isRecord(value) ? value : {};
}

export function text(value: unknown, fallback = ''): string {
  if (typeof value === 'string') return value;
  if (value === undefined || value === null) return fallback;
  return String(value);
}

export function boolean(value: unknown): boolean {
  return value === true;
}

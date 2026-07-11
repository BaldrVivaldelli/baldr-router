import { spawn, type ChildProcess } from 'node:child_process';
import * as fs from 'node:fs';
import * as path from 'node:path';
import * as vscode from 'vscode';

import { redactSensitive } from './redaction.js';

export const EXTENSION_VERSION = '0.16.1';
export const CONTEXT7_SECRET_KEY = 'baldr.context7ApiKey';

type JsonRecord = Record<string, unknown>;

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

export class BaldrRuntime {
  private readonly bootstrapPath: string;
  private readonly wheelPath: string;

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
      return path.join(runtimeDir, 'baldr_router-0.16.1-py3-none-any.whl');
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

  private async customEnvironment(): Promise<Record<string, string>> {
    const configuration = vscode.workspace.getConfiguration('baldr');
    let mode = configuration.get<string>('runtime.mode', 'auto');
    let distro = configuration.get<string>('runtime.wslDistro', '').trim();
    const workspacePath = vscode.workspace.workspaceFolders?.[0]?.uri.fsPath;
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

  private async processEnvironment(): Promise<NodeJS.ProcessEnv> {
    return { ...process.env, ...(await this.customEnvironment()) };
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

  async runRouterJson(routerArgs: string[], token?: vscode.CancellationToken): Promise<JsonRecord> {
    return this.invokeBootstrapJson(['exec', '--', ...routerArgs], token);
  }

  async runFacade(
    intent: 'setup' | 'status' | 'run',
    options: {
      workspaceRoot?: string;
      task?: string;
      recentLimit?: number;
      dryRun?: boolean;
    } = {},
    token?: vscode.CancellationToken,
  ): Promise<JsonRecord> {
    this.requireTrustedWorkspace(options.workspaceRoot);
    const args = ['facade', intent];
    if (intent === 'setup') {
      if (options.workspaceRoot) {
        args.push(options.workspaceRoot, '--trust-workspace');
      }
    } else if (intent === 'status') {
      if (options.workspaceRoot) args.push(options.workspaceRoot);
      args.push('--recent-limit', String(options.recentLimit ?? 5));
    } else {
      if (!options.workspaceRoot) throw new Error('An open workspace is required for /run.');
      if (!options.task?.trim()) throw new Error('A non-empty task is required for /run.');
      args.push(options.workspaceRoot, options.task.trim());
      if (options.dryRun) args.push('--dry-run');
    }
    args.push('--client', 'vscode-extension');
    return this.runRouterJson(args, token);
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
    );
  }

  async configureContext7FromEnvironment(token?: vscode.CancellationToken): Promise<JsonRecord> {
    return this.configureContext7FromSecret(token);
  }

  async disableContext7(token?: vscode.CancellationToken): Promise<JsonRecord> {
    return this.runRouterJson(['disable-context7', '--remove-codex-mcp'], token);
  }

  private async invokeBootstrapJson(args: string[], token?: vscode.CancellationToken): Promise<JsonRecord> {
    await fs.promises.mkdir(this.context.globalStorageUri.fsPath, { recursive: true });
    const result = await this.spawnCapture(args, token);
    const stdout = result.stdout.trim();
    if (!stdout) throw new Error(result.stderr.trim() || 'Baldr returned no JSON output.');
    try {
      const parsed: unknown = JSON.parse(stdout);
      if (!isRecord(parsed)) throw new Error('JSON root is not an object.');
      return parsed;
    } catch (error) {
      const secrets = await this.knownSecrets();
      this.output.appendLine(`[parse-error] stdout: ${redactSensitive(stdout.slice(0, 4000), secrets)}`);
      throw new Error(`Baldr returned invalid JSON: ${error instanceof Error ? error.message : String(error)}`);
    }
  }

  private async spawnCapture(
    bootstrapArgs: string[],
    token?: vscode.CancellationToken,
  ): Promise<{ stdout: string; stderr: string }> {
    const env = await this.processEnvironment();
    const secrets = await this.knownSecrets();
    this.output.appendLine(`[runtime] ${bootstrapArgs.join(' ')}`);

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
        if (code !== 0) {
          reject(new Error(stderr.trim() || stdout.trim() || `Baldr exited with code ${code}.`));
          return;
        }
        resolve({ stdout, stderr });
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

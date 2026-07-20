import * as vscode from 'vscode';
import * as path from 'node:path';

import { BaldrConsoleProvider } from './console.js';
import { renderError, renderRun, renderSetup, renderStatus } from './render.js';
import {
  BaldrRuntime,
  EXTENSION_VERSION,
  record,
} from './runtime.js';
import type { BaldrIntentId } from './generated/intents.js';
import {
  captureWorkspaceContext,
  contextualWorkspaceRoot,
  resolveWorkspaceRoot,
} from './workspaceContext.js';

type Intent = BaldrIntentId;
type JsonRecord = Record<string, unknown>;
let activeRuntime: BaldrRuntime | undefined;

export async function activate(context: vscode.ExtensionContext): Promise<void> {
  const output = vscode.window.createOutputChannel('Baldr Router', { log: true });
  const runtime = new BaldrRuntime(context, output);
  activeRuntime = runtime;
  const changed = new vscode.EventEmitter<void>();
  const consoleProvider = new BaldrConsoleProvider(context, runtime, output);
  context.subscriptions.push(runtime, output, changed, consoleProvider);

  const mcpProvider: vscode.McpServerDefinitionProvider<vscode.McpStdioServerDefinition> = {
    onDidChangeMcpServerDefinitions: changed.event,
    provideMcpServerDefinitions: async () => [await runtime.mcpDefinition()],
    resolveMcpServerDefinition: async (server, token) => runtime.resolveMcpDefinition(server, token),
  };
  context.subscriptions.push(vscode.lm.registerMcpServerDefinitionProvider('baldr.router', mcpProvider));

  context.subscriptions.push(
    vscode.window.registerWebviewViewProvider(
      BaldrConsoleProvider.viewType,
      consoleProvider,
      { webviewOptions: { retainContextWhenHidden: true } },
    ),
  );

  context.subscriptions.push(
    vscode.commands.registerCommand('baldr.open', async () => {
      await consoleProvider.reveal();
    }),
  );

  context.subscriptions.push(
    vscode.commands.registerCommand('baldr.qualification.cancelCanary', async () => {
      if (!vscode.workspace.isTrusted) {
        throw new Error('Trust this VS Code workspace before running qualification canaries.');
      }
      const result = await runtime.runExtensionHostCancellationCanary();
      await runtime.recordClientReceipt(undefined, {
        extension_host_cancellation: result,
      });
      return result;
    }),
  );

  const participant = vscode.chat.createChatParticipant(
    'baldr.vscode',
    createChatHandler(runtime, output, consoleProvider),
  );
  participant.iconPath = new vscode.ThemeIcon('git-merge');
  participant.followupProvider = {
    provideFollowups(result) {
      const intent = record(result.metadata).intent;
      if (intent === 'setup') return [{ prompt: '/status', label: 'Check Baldr status' }];
      if (intent === 'status') return [{ prompt: '/run ', label: 'Create a Baldr task' }];
      return [{ prompt: '/status', label: 'Show Baldr status' }];
    },
  };
  context.subscriptions.push(participant);

  void warmRuntime(context, runtime, output, changed, consoleProvider);
}

export async function deactivate(): Promise<void> {
  const runtime = activeRuntime;
  activeRuntime = undefined;
  await runtime?.shutdown();
}

function createChatHandler(
  runtime: BaldrRuntime,
  output: vscode.LogOutputChannel,
  consoleProvider: BaldrConsoleProvider,
): vscode.ChatRequestHandler {
  return async (request, chatContext, stream, token) => {
    const intent = normalizeIntent(request.command);
    const prior = latestBaldrConversation(chatContext);
    try {
      if (intent === 'setup') {
        stream.progress('Preparing Baldr…');
        await runtime.ensure(token);
        const result = await runtime.runFacade(
          'setup',
          { workspaceRoot: contextualWorkspaceRoot(prior?.workspaceRoot) },
          token,
        );
        stream.markdown(renderSetup(result));
        stream.button({ command: 'baldr.open', title: 'Open Baldr Console' });
        return { metadata: { intent, ok: true } };
      }

      if (intent === 'status') {
        stream.progress('Checking Baldr status…');
        await runtime.ensure(token);
        const result = await runtime.runFacade(
          'status',
          { workspaceRoot: contextualWorkspaceRoot(prior?.workspaceRoot), recentLimit: 5 },
          token,
        );
        stream.markdown(renderStatus(result));
        stream.button({ command: 'baldr.open', title: 'Open Baldr Console' });
        return { metadata: { intent, ok: result.ok === true } };
      }

      const task = request.prompt.trim();
      if (!task) {
        stream.markdown('Write a task after `@baldr`, use `@baldr /run <task>`, or open the dedicated Baldr view.');
        stream.button({ command: 'baldr.open', title: 'Open Baldr Console' });
        return { metadata: { intent, ok: false } };
      }

      const workspaceRoot = await resolveWorkspaceRoot({
        references: request.references,
        preferredRoot: prior?.workspaceRoot,
        promptIfAmbiguous: true,
      });
      requireWorkspaceRoot(workspaceRoot);
      const captured = captureWorkspaceContext(workspaceRoot, request.references);
      const capturedOptions = {
        attachments: captured.attachments,
        extraContext: captured.extraContext,
      };
      const canContinue = Boolean(
        prior?.workItemId
        && prior.workspaceRoot
        && sameWorkspace(prior.workspaceRoot, workspaceRoot),
      );
      let itemId = prior?.workItemId ?? '';
      let result: JsonRecord;
      if (canContinue) {
        stream.progress('Continuing the durable Baldr conversation…');
        result = await runtime.continueWorkItem(workspaceRoot, itemId, task, capturedOptions, token);
      } else {
        stream.progress('Creating a durable Baldr conversation…');
        const created = await runtime.createWorkItem(workspaceRoot, task, capturedOptions, token);
        const item = record(created.work_item);
        itemId = String(item.id ?? '');
        if (!itemId) throw new Error('Baldr did not return a work item id.');
        stream.progress('Baldr is planning, implementing, and reviewing…');
        result = await runtime.startWorkItem(workspaceRoot, itemId, {}, token);
      }
      const completed = record(result.work_item);
      output.info(`Chat work item completed: ${JSON.stringify({
        id: String(completed.id ?? itemId),
        status: String(completed.status ?? result.status ?? 'unknown'),
        error_code: completed.error_code ? String(completed.error_code) : undefined,
      })}`);
      stream.markdown(renderRun(result));
      stream.button({ command: 'baldr.open', title: 'Open Baldr Console' });
      void consoleProvider.refresh(false);
      return {
        metadata: {
          intent,
          ok: result.ok === true,
          workItemId: itemId,
          workspaceRoot,
        },
      };
    } catch (error) {
      if (error instanceof vscode.CancellationError) {
        stream.markdown('Baldr operation cancelled.');
        return { metadata: { intent, cancelled: true } };
      }
      output.error(error instanceof Error ? error : new Error(String(error)));
      stream.markdown(renderError(error));
      stream.button({ command: 'baldr.open', title: 'Open Baldr Console' });
      return { metadata: { intent, ok: false } };
    }
  };
}

function normalizeIntent(command: string | undefined): Intent {
  return command === 'setup' || command === 'status' || command === 'run' ? command : 'run';
}

function latestBaldrConversation(context: vscode.ChatContext): { workItemId: string; workspaceRoot: string } | undefined {
  for (const turn of [...context.history].reverse()) {
    const metadata = record(record(turn).result).metadata;
    const value = record(metadata);
    const workItemId = String(value.workItemId ?? '');
    const workspaceRoot = String(value.workspaceRoot ?? '');
    if (workItemId && workspaceRoot) return { workItemId, workspaceRoot };
  }
  return undefined;
}

function sameWorkspace(left: string, right: string): boolean {
  const normalize = (value: string): string => {
    const resolved = path.resolve(value).replace(/[\\/]+$/, '');
    return process.platform === 'win32' ? resolved.toLowerCase() : resolved;
  };
  return normalize(left) === normalize(right);
}

function requireWorkspaceRoot(root: string | undefined): asserts root is string {
  if (!root) throw new Error('Open a workspace folder before running a Baldr task.');
  if (!vscode.workspace.isTrusted) {
    throw new Error('Trust this VS Code workspace before Baldr may run local providers.');
  }
}

async function warmRuntime(
  context: vscode.ExtensionContext,
  runtime: BaldrRuntime,
  output: vscode.LogOutputChannel,
  changed: vscode.EventEmitter<void>,
  consoleProvider: BaldrConsoleProvider,
): Promise<void> {
  try {
    const target = await runtime.ensure();
    await runtime.recordClientReceipt();
    output.info(`Runtime ready: ${JSON.stringify(target)}`);
    changed.fire();
    if (vscode.workspace.isTrusted) {
      for (const folder of vscode.workspace.workspaceFolders ?? []) {
        const settled = await runtime.settleWorkItems(folder.uri.fsPath);
        const settledCount = Number(settled.settled_count ?? 0);
        const requiresInput = Number(settled.requires_input_count ?? 0);
        const failedSettlements = Array.isArray(settled.settled)
          ? settled.settled.map(record).filter((item) => item.ok === false)
          : [];
        const processValidation = record(settled.process_validation);
        if (settledCount > 0 || requiresInput > 0 || failedSettlements.length > 0) {
          output.info(`Automatic recovery: ${JSON.stringify({
            workspace: folder.uri.fsPath,
            settled_count: settledCount,
            requires_input_count: requiresInput,
            failed_count: failedSettlements.length,
            orphan_processes: Number(processValidation.orphan_processes ?? 0),
          })}`);
        }
        if (failedSettlements.length > 0 || processValidation.ok === false) {
          output.warn(`Automatic recovery needs attention: ${JSON.stringify(failedSettlements)}`);
          void vscode.window.showWarningMessage(
            'Baldr no pudo cerrar o recuperar todas las sesiones. El trabajo sigue guardado; abrí Baldr para revisar la acción recomendada.',
            'Abrir Baldr',
          ).then((action) => action === 'Abrir Baldr' ? consoleProvider.reveal() : undefined);
        }
      }
    }
    await consoleProvider.refresh(false);
  } catch (error) {
    output.error(error instanceof Error ? error : new Error(String(error)));
    return;
  }

  const configuration = vscode.workspace.getConfiguration('baldr');
  const shouldOffer = configuration.get<boolean>('onboarding.openAfterInstall', true);
  const alreadyOffered = context.globalState.get<boolean>('baldr.onboarding.offered', false);
  if (!shouldOffer || alreadyOffered) return;

  await context.globalState.update('baldr.onboarding.offered', true);
  const action = await vscode.window.showInformationMessage(
    `Baldr Router ${EXTENSION_VERSION} is ready in its own Activity Bar view.`,
    'Open Baldr',
    'Later',
  );
  if (action === 'Open Baldr') await consoleProvider.reveal();
}

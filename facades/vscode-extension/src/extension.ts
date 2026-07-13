import * as vscode from 'vscode';

import { BaldrConsoleProvider } from './console.js';
import { renderError, renderRun, renderSetup, renderStatus } from './render.js';
import {
  BaldrRuntime,
  EXTENSION_VERSION,
  record,
} from './runtime.js';
import type { BaldrIntentId } from './generated/intents.js';

type Intent = BaldrIntentId;
type JsonRecord = Record<string, unknown>;

export async function activate(context: vscode.ExtensionContext): Promise<void> {
  const output = vscode.window.createOutputChannel('Baldr Router', { log: true });
  const runtime = new BaldrRuntime(context, output);
  const changed = new vscode.EventEmitter<void>();
  const consoleProvider = new BaldrConsoleProvider(context, runtime, output);
  context.subscriptions.push(output, changed, consoleProvider);

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

export function deactivate(): void {
  // VS Code disposes all registered resources from context.subscriptions.
}

function createChatHandler(
  runtime: BaldrRuntime,
  output: vscode.LogOutputChannel,
  consoleProvider: BaldrConsoleProvider,
): vscode.ChatRequestHandler {
  return async (request, _chatContext, stream, token) => {
    const intent = normalizeIntent(request.command);
    try {
      if (intent === 'setup') {
        stream.progress('Preparing Baldr…');
        await runtime.ensure(token);
        const result = await runtime.runFacade(
          'setup',
          { workspaceRoot: currentWorkspaceRoot() },
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
          { workspaceRoot: currentWorkspaceRoot(), recentLimit: 5 },
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

      const workspaceRoot = requireWorkspaceRoot();
      stream.progress('Creating a durable Baldr work item…');
      const created = await runtime.createWorkItem(workspaceRoot, task, {}, token);
      const item = record(created.work_item);
      const itemId = String(item.id ?? '');
      if (!itemId) throw new Error('Baldr did not return a work item id.');
      stream.markdown(`Created durable item **${String(item.title ?? 'Baldr task')}**. Execution continues in the Baldr Console.`);
      stream.button({ command: 'baldr.open', title: 'Open Baldr Console' });
      void runtime.startWorkItem(workspaceRoot, itemId).then((result) => {
        const completed = record(result.work_item);
        output.info(`Chat-created work item completed: ${JSON.stringify({
          id: String(completed.id ?? itemId),
          status: String(completed.status ?? 'unknown'),
          error_code: completed.error_code ? String(completed.error_code) : undefined,
        })}`);
      }).catch((error) => {
        output.error(error instanceof Error ? error : new Error(String(error)));
      }).finally(() => void consoleProvider.refresh(false));
      void consoleProvider.refresh(false);
      return { metadata: { intent, ok: true, workItemId: itemId } };
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

function currentWorkspaceRoot(): string | undefined {
  return vscode.workspace.workspaceFolders?.[0]?.uri.fsPath;
}

function requireWorkspaceRoot(): string {
  const root = currentWorkspaceRoot();
  if (!root) throw new Error('Open a workspace folder before running a Baldr task.');
  if (!vscode.workspace.isTrusted) {
    throw new Error('Trust this VS Code workspace before Baldr may run local providers.');
  }
  return root;
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

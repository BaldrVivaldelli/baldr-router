import * as vscode from 'vscode';
import {
  BaldrRuntime,
  CONTEXT7_SECRET_KEY,
  EXTENSION_VERSION,
  record,
  text,
} from './runtime.js';
import { renderError, renderQualification, renderRun, renderSetup, renderStatus } from './render.js';
import { BALDR_INTENTS, type BaldrIntentId } from './generated/intents.js';

type Intent = BaldrIntentId;
type OpenAction = Intent | 'profiles' | 'qualification';
type JsonRecord = Record<string, unknown>;
type RoleName = 'architect' | 'implementer' | 'reviewer';

const ROLE_NAMES: RoleName[] = ['architect', 'implementer', 'reviewer'];

interface SetupResult {
  setup: JsonRecord;
  context7?: JsonRecord;
  choice?: string;
  roles?: string;
}

export async function activate(context: vscode.ExtensionContext): Promise<void> {
  const output = vscode.window.createOutputChannel('Baldr Router', { log: true });
  const runtime = new BaldrRuntime(context, output);
  const changed = new vscode.EventEmitter<void>();
  context.subscriptions.push(output, changed);

  const provider: vscode.McpServerDefinitionProvider<vscode.McpStdioServerDefinition> = {
    onDidChangeMcpServerDefinitions: changed.event,
    provideMcpServerDefinitions: async () => [await runtime.mcpDefinition()],
    resolveMcpServerDefinition: async (server, token) => runtime.resolveMcpDefinition(server, token),
  };
  context.subscriptions.push(vscode.lm.registerMcpServerDefinitionProvider('baldr.router', provider));

  context.subscriptions.push(
    vscode.commands.registerCommand('baldr.open', async () => {
      await openBaldr(context, runtime, output, changed);
    }),
  );
  context.subscriptions.push(
    vscode.commands.registerCommand('baldr.configureProfiles', async () => {
      await configureProfiles(runtime, output, changed);
    }),
  );

  const participant = vscode.chat.createChatParticipant(
    'baldr.vscode',
    createChatHandler(context, runtime, output, changed),
  );
  participant.iconPath = new vscode.ThemeIcon('git-merge');
  participant.followupProvider = {
    provideFollowups(result) {
      const intent = record(result.metadata).intent;
      if (intent === 'setup') return [{ prompt: '/status', label: 'Check Baldr status' }];
      if (intent === 'status') return [{ prompt: '/run ', label: 'Run a task with Baldr' }];
      return [{ prompt: '/status', label: 'Show Baldr status' }];
    },
  };
  context.subscriptions.push(participant);

  void warmRuntime(context, runtime, output, changed);
}

export function deactivate(): void {
  // VS Code disposes all registered resources from context.subscriptions.
}

function createChatHandler(
  context: vscode.ExtensionContext,
  runtime: BaldrRuntime,
  output: vscode.LogOutputChannel,
  changed: vscode.EventEmitter<void>,
): vscode.ChatRequestHandler {
  return async (request, _chatContext, stream, token) => {
    const intent = normalizeIntent(request.command);
    try {
      if (intent === 'setup') {
        stream.progress('Preparing Baldr setup…');
        const result = await interactiveSetup(context, runtime, output, changed, token);
        stream.markdown(renderSetup(result.setup));
        if (result.choice) stream.markdown(`\n\n_Context7 choice: ${result.choice}._`);
        stream.button({ command: 'baldr.configureProfiles', title: 'Configure execution profiles' });
        return { metadata: { intent } };
      }
      if (intent === 'status') {
        stream.progress('Checking Baldr status…');
        const result = await status(runtime, token);
        stream.markdown(renderStatus(result));
        stream.button({ command: 'baldr.configureProfiles', title: 'Configure architect, implementer, and reviewer' });
        return { metadata: { intent } };
      }

      const task = request.prompt.trim();
      if (!task) {
        stream.markdown('Use `@baldr /run <task>` or write the task directly after `@baldr`.');
        return { metadata: { intent, ok: false } };
      }
      stream.progress('Baldr is coordinating architect, implementer, and reviewer…');
      const result = await run(runtime, task, token);
      stream.markdown(renderRun(result));
      return { metadata: { intent, ok: result.ok === true, runId: result.run_id } };
    } catch (error) {
      if (error instanceof vscode.CancellationError) {
        stream.markdown('Baldr operation cancelled.');
        return { metadata: { intent, cancelled: true } };
      }
      output.error(error instanceof Error ? error : new Error(String(error)));
      stream.markdown(renderError(error));
      stream.button({ command: 'baldr.open', title: 'Open Baldr' });
      return { metadata: { intent, ok: false } };
    }
  };
}

function normalizeIntent(command: string | undefined): Intent {
  return command === 'setup' || command === 'status' || command === 'run' ? command : 'run';
}

async function openBaldr(
  context: vscode.ExtensionContext,
  runtime: BaldrRuntime,
  output: vscode.LogOutputChannel,
  changed: vscode.EventEmitter<void>,
): Promise<void> {
  const icons: Record<Intent, string> = {
    setup: 'settings-gear',
    status: 'pulse',
    run: 'play',
  };
  const actions: Array<{ label: string; description: string; intent: OpenAction }> = [
    ...BALDR_INTENTS.map((intent) => ({
      label: `$(${icons[intent.id]}) ${intent.title}`,
      description: intent.description,
      intent: intent.id,
    })),
    {
      label: '$(settings-gear) Configure execution profiles',
      description: 'Choose the provider, model, and effort for architect, implementer, and reviewer',
      intent: 'profiles',
    },
    {
      label: '$(verified-filled) Qualification',
      description: 'Prove install, execute, cancel, restart, upgrade, and cleanup three times in this real environment',
      intent: 'qualification',
    },
  ];
  const selected = await vscode.window.showQuickPick(actions, {
    title: 'Baldr Router',
    placeHolder: 'Choose a Baldr action',
    ignoreFocusOut: true,
  });
  if (!selected) return;

  try {
    if (selected.intent === 'setup') {
      const result = await interactiveSetup(context, runtime, output, changed);
      await showMarkdown(renderSetup(result.setup), 'Baldr Setup');
      return;
    }
    if (selected.intent === 'status') {
      const result = await vscode.window.withProgress(
        { location: vscode.ProgressLocation.Notification, title: 'Checking Baldr status…' },
        () => status(runtime),
      );
      await showMarkdown(renderStatus(result), 'Baldr Status');
      return;
    }

    if (selected.intent === 'profiles') {
      await configureProfiles(runtime, output, changed);
      return;
    }

    if (selected.intent === 'qualification') {
      const workspaceRoot = currentWorkspaceRoot();
      if (!workspaceRoot) throw new Error('Open and trust a workspace before running qualification.');
      const evidenceChoice = await vscode.window.showQuickPick(
        [
          {
            label: '$(beaker) Prepare / update qualification',
            id: 'prepare',
            description: 'Run three lifecycle passes and create editable assertion/canary templates',
          },
          {
            label: '$(verified) Evaluate completed evidence',
            id: 'evaluate',
            description: 'Use completed assertion and canary JSON files to request qualification',
          },
        ],
        {
          title: 'Baldr real-environment qualification',
          placeHolder: 'Choose how to qualify this installation',
          ignoreFocusOut: true,
        },
      );
      if (!evidenceChoice) return;
      let clientAssertions: string | undefined;
      let canaryResults: string | undefined;
      if (evidenceChoice.id === 'evaluate') {
        const assertions = await vscode.window.showOpenDialog({
          title: 'Select completed client-assertions.json',
          canSelectMany: false,
          canSelectFiles: true,
          canSelectFolders: false,
          filters: { JSON: ['json'] },
          openLabel: 'Use assertions',
        });
        if (!assertions?.[0]) return;
        const canaries = await vscode.window.showOpenDialog({
          title: 'Select completed canary-results.json',
          canSelectMany: false,
          canSelectFiles: true,
          canSelectFolders: false,
          filters: { JSON: ['json'] },
          openLabel: 'Use canaries',
        });
        if (!canaries?.[0]) return;
        clientAssertions = assertions[0].fsPath;
        canaryResults = canaries[0].fsPath;
      }
      const providerChoice = await vscode.window.showQuickPick(
        [
          {
            label: 'Lifecycle only',
            includeProviderSmoke: false,
            description: 'Useful for preparation; cannot produce a final qualified receipt',
          },
          {
            label: 'Lifecycle + real provider smoke',
            includeProviderSmoke: true,
            description: 'Required for final qualification; performs an authenticated read-only provider call',
          },
        ],
        {
          title: 'Baldr real-environment qualification',
          placeHolder: 'Choose the qualification depth',
          ignoreFocusOut: true,
        },
      );
      if (!providerChoice) return;
      const result = await vscode.window.withProgress(
        {
          location: vscode.ProgressLocation.Notification,
          title: 'Baldr is qualifying this real environment (3 consecutive passes)…',
          cancellable: true,
        },
        (_progress, token) => runtime.runQualification(
          workspaceRoot,
          {
            includeProviderSmoke: providerChoice.includeProviderSmoke,
            clientAssertions,
            canaryResults,
          },
          token,
        ),
      );
      await showMarkdown(renderQualification(result), 'Baldr Qualification');
      return;
    }

    const task = await vscode.window.showInputBox({
      title: 'Run Baldr workflow',
      prompt: 'Describe the task for the architect, implementer, and reviewer',
      placeHolder: 'Implement refresh-token rotation and run the relevant tests',
      ignoreFocusOut: true,
    });
    if (!task?.trim()) return;
    const result = await vscode.window.withProgress(
      { location: vscode.ProgressLocation.Notification, title: 'Baldr is running the orchestration workflow…', cancellable: true },
      (_progress, token) => run(runtime, task, token),
    );
    await showMarkdown(renderRun(result), 'Baldr Run');
  } catch (error) {
    if (error instanceof vscode.CancellationError) return;
    output.error(error instanceof Error ? error : new Error(String(error)));
    await showMarkdown(renderError(error), 'Baldr Error');
  }
}

async function interactiveSetup(
  context: vscode.ExtensionContext,
  runtime: BaldrRuntime,
  output: vscode.LogOutputChannel,
  changed: vscode.EventEmitter<void>,
  token?: vscode.CancellationToken,
): Promise<SetupResult> {
  const workspaceRoot = currentWorkspaceRoot();
  const setup = await vscode.window.withProgress(
    { location: vscode.ProgressLocation.Notification, title: 'Preparing the Baldr private runtime…' },
    async (_progress, progressToken) => {
      const effectiveToken = token ?? progressToken;
      await runtime.ensure(effectiveToken);
      await runtime.recordClientReceipt(effectiveToken);
      return runtime.runFacade('setup', { workspaceRoot }, effectiveToken);
    },
  );

  const roles = await configureRoles(runtime, token);
  const storedSecret = await context.secrets.get(CONTEXT7_SECRET_KEY);
  const context7Current = record(record(setup.context7_onboarding).current);
  const choices = [
    { label: '$(circle-slash) Not now', description: 'Keep Context7 optional and disabled', id: 'not-now' },
    { label: '$(key) I have a Context7 API key', description: 'Store it securely in VS Code SecretStorage', id: 'secure-key' },
    { label: '$(terminal) Use existing CONTEXT7_API_KEY', description: 'Store only the environment-variable name in Baldr config', id: 'environment' },
    { label: '$(trash) Disable Context7', description: 'Disable enrichment and remove the VS Code-stored key', id: 'disable' },
  ];
  if (storedSecret || context7Current.enabled) {
    choices.push({
      label: '$(check) Keep current Context7 configuration',
      description: context7Current.enabled
        ? `Currently enabled in ${text(context7Current.mode, 'configured')} mode`
        : 'A secure key is already stored for the extension',
      id: 'keep',
    });
  }
  const choice = await vscode.window.showQuickPick(choices, {
    title: 'Optional Context7 setup',
    placeHolder: 'Do you want documentation enrichment?',
    ignoreFocusOut: true,
  });

  let context7: JsonRecord | undefined;
  if (!choice || choice.id === 'keep') {
    await context.globalState.update('baldr.onboarding.completed', true);
    setup.vscode_setup = { roles, context7: choice?.label ?? 'unchanged' };
    return { setup, choice: choice?.label ?? 'unchanged', roles };
  }

  if (choice.id === 'secure-key') {
    const key = await vscode.window.showInputBox({
      title: 'Context7 API key',
      prompt: 'Stored encrypted by VS Code. The value is never written to settings, the workspace, or chat.',
      password: true,
      ignoreFocusOut: true,
      validateInput: (value) => value.trim().length < 8 ? 'Enter a valid Context7 API key.' : undefined,
    });
    if (!key) return { setup, choice: 'cancelled', roles };
    await context.secrets.store(CONTEXT7_SECRET_KEY, key.trim());
    context7 = await runtime.configureContext7FromSecret(token);
    changed.fire();
  } else if (choice.id === 'environment') {
    context7 = await runtime.configureContext7FromEnvironment(token);
    changed.fire();
    if (context7.api_key_available !== true) {
      void vscode.window.showWarningMessage(
        'Baldr stored the environment-variable source, but CONTEXT7_API_KEY was not visible to the current process. Export it where Baldr runs and restart the MCP server.',
      );
    }
  } else {
    if (choice.id === 'disable') await context.secrets.delete(CONTEXT7_SECRET_KEY);
    context7 = await runtime.disableContext7(token);
    changed.fire();
  }

  await context.globalState.update('baldr.onboarding.completed', true);
  output.info(`Setup completed with Context7 choice: ${choice.id}`);
  const refreshed = await runtime.runFacade('setup', { workspaceRoot }, token);
  refreshed.vscode_setup = { roles, context7: choice.label };
  return { setup: refreshed, context7, choice: choice.label, roles };
}

async function configureRoles(
  runtime: BaldrRuntime,
  token?: vscode.CancellationToken,
  offerKeepCurrent = true,
): Promise<string> {
  if (offerKeepCurrent) {
    const decision = await vscode.window.showQuickPick(
      [
        { id: 'keep', label: '$(check) Keep current execution profiles', description: 'Recommended for the first setup' },
        { id: 'customize', label: '$(organization) Customize each phase', description: 'Choose provider, model, and effort for architect, implementer, and reviewer' },
      ],
      { title: 'Baldr execution profiles', placeHolder: 'Keep reusable profiles or customize each phase?', ignoreFocusOut: true },
    );
    if (!decision || decision.id === 'keep') return 'unchanged';
  }

  const [status, rolesStatus] = await Promise.all([
    runtime.runRouterJson(['provider-status'], token),
    runtime.runRouterJson(['roles'], token),
  ]);
  const providers = Object.entries(record(status.providers));
  if (providers.length === 0) throw new Error('Baldr Router reported no implemented providers.');

  const assignments: string[] = [];
  for (const role of ROLE_NAMES) {
    const current = resolvedRoleProfile(rolesStatus, role);
    const choices = providers.map(([name, raw]) => {
      const provider = record(raw);
      return {
        label: name,
        provider: name,
        description: provider.ok === true
          ? 'ready'
          : text(provider.reason, provider.found === true ? 'detected' : 'not currently ready'),
      };
    });
    const selected = await vscode.window.showQuickPick(choices, {
      title: `Provider for ${role}`,
      placeHolder: `Choose the ${role} provider`,
      ignoreFocusOut: true,
    });
    if (!selected) return assignments.length ? assignments.join(', ') : 'unchanged';
    const profile = await configureRoleProfile(role, selected.provider, current);
    if (!profile) return assignments.length ? assignments.join(', ') : 'unchanged';
    await runtime.runRouterJson([
      'set-role-provider', role, selected.provider,
      '--model', profile.model,
      '--reasoning-effort', profile.reasoningEffort,
      '--agent', profile.agent,
      '--effort', profile.effort,
    ], token);
    assignments.push(`${role}=${selected.provider}${profile.summary}`);
  }
  return assignments.join(', ');
}

async function configureProfiles(
  runtime: BaldrRuntime,
  output: vscode.LogOutputChannel,
  changed: vscode.EventEmitter<void>,
): Promise<void> {
  try {
    await runtime.ensure();
    const roles = await configureRoles(runtime, undefined, false);
    if (roles === 'unchanged') return;
    changed.fire();
    await showMarkdown(`# Baldr execution profiles\n\n✅ **Saved**\n\n${roles.split(', ').map((role) => `- ${role}`).join('\n')}`, 'Baldr execution profiles');
  } catch (error) {
    output.error(error instanceof Error ? error : new Error(String(error)));
    await showMarkdown(renderError(error), 'Baldr Error');
  }
}

interface RoleProfileValues {
  model: string;
  reasoningEffort: string;
  agent: string;
  effort: string;
}

function resolvedRoleProfile(rolesStatus: JsonRecord, role: RoleName): RoleProfileValues {
  const resolved = record(record(rolesStatus.resolved)[role]);
  const profiles = Array.isArray(resolved.profiles) ? resolved.profiles : [];
  const first = record(profiles[0]);
  return {
    model: text(first.model, ''),
    reasoningEffort: text(first.reasoning_effort, ''),
    agent: text(first.agent, ''),
    effort: text(first.effort, ''),
  };
}

async function configureRoleProfile(
  role: RoleName,
  provider: string,
  current: RoleProfileValues,
): Promise<(RoleProfileValues & { summary: string }) | undefined> {
  if (provider === 'codex') {
    const model = await chooseModel(role, current.model);
    if (model === undefined) return undefined;
    const reasoningEffort = await chooseEffort(role, 'Reasoning effort', current.reasoningEffort);
    if (reasoningEffort === undefined) return undefined;
    return {
      model,
      reasoningEffort,
      agent: '',
      effort: '',
      summary: ` (model: ${model || 'provider default'}, effort: ${reasoningEffort || 'provider default'})`,
    };
  }
  if (provider === 'kiro-cli') {
    const agent = await vscode.window.showInputBox({
      title: `Kiro agent for ${role}`,
      prompt: 'Leave empty to use the configured Kiro default agent.',
      value: current.agent,
      ignoreFocusOut: true,
    });
    if (agent === undefined) return undefined;
    const effort = await chooseEffort(role, 'Kiro effort', current.effort);
    if (effort === undefined) return undefined;
    return {
      model: '',
      reasoningEffort: '',
      agent: agent.trim(),
      effort,
      summary: ` (agent: ${agent.trim() || 'provider default'}, effort: ${effort || 'provider default'})`,
    };
  }
  return { model: '', reasoningEffort: '', agent: '', effort: '', summary: '' };
}

async function chooseModel(role: RoleName, current: string): Promise<string | undefined> {
  const choices = [
    { label: '$(circle-slash) Use provider default', value: '', description: 'Do not override the Codex model for this phase' },
    ...(current ? [{ label: `$(check) ${current}`, value: current, description: 'Keep the current model override' }] : []),
    { label: '$(edit) Enter a model name…', value: '__custom__', description: 'Use a model available to your Codex installation' },
  ];
  const selected = await vscode.window.showQuickPick(choices, {
    title: `Codex model for ${role}`,
    placeHolder: 'Choose the provider default or enter a model name',
    ignoreFocusOut: true,
  });
  if (!selected) return undefined;
  if (selected.value !== '__custom__') return selected.value;
  const model = await vscode.window.showInputBox({
    title: `Codex model for ${role}`,
    prompt: 'Enter the model name supported by your Codex installation.',
    value: current,
    ignoreFocusOut: true,
    validateInput: (value) => value.trim() ? undefined : 'Enter a model name or choose the provider default.',
  });
  return model?.trim();
}

async function chooseEffort(
  role: RoleName,
  label: string,
  current: string,
): Promise<string | undefined> {
  const choices = [
    { label: '$(circle-slash) Use provider default', value: '', description: 'Do not override this setting for the phase' },
    ...['low', 'medium', 'high', 'xhigh'].map((value) => ({
      label: value,
      value,
      description: value === current ? 'Current override' : undefined,
    })),
  ];
  const selected = await vscode.window.showQuickPick(choices, {
    title: `${label} for ${role}`,
    placeHolder: `Choose the ${label.toLowerCase()} for ${role}`,
    ignoreFocusOut: true,
  });
  return selected?.value;
}

async function status(runtime: BaldrRuntime, token?: vscode.CancellationToken): Promise<JsonRecord> {
  await runtime.ensure(token);
  return runtime.runFacade('status', { workspaceRoot: currentWorkspaceRoot(), recentLimit: 5 }, token);
}

async function run(runtime: BaldrRuntime, task: string, token?: vscode.CancellationToken): Promise<JsonRecord> {
  const workspaceRoot = currentWorkspaceRoot();
  if (!workspaceRoot) throw new Error('Open a workspace folder before running a Baldr task.');
  await runtime.ensure(token);
  return runtime.runFacade('run', { workspaceRoot, task }, token);
}

function currentWorkspaceRoot(): string | undefined {
  return vscode.workspace.workspaceFolders?.[0]?.uri.fsPath;
}

async function showMarkdown(markdown: string | vscode.MarkdownString, title: string): Promise<void> {
  const body = typeof markdown === 'string' ? markdown : markdown.value;
  const document = await vscode.workspace.openTextDocument({ language: 'markdown', content: `<!-- ${title} -->\n${body}\n` });
  await vscode.window.showTextDocument(document, { preview: true, viewColumn: vscode.ViewColumn.Beside });
}

async function warmRuntime(
  context: vscode.ExtensionContext,
  runtime: BaldrRuntime,
  output: vscode.LogOutputChannel,
  changed: vscode.EventEmitter<void>,
): Promise<void> {
  try {
    const target = await runtime.ensure();
    await runtime.recordClientReceipt();
    output.info(`Runtime ready: ${JSON.stringify(target)}`);
    const verification = await runtime.runFacade('status', { recentLimit: 1 });
    output.info(`Lifecycle verification: ${JSON.stringify(record(verification.verification))}`);
    changed.fire();
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
    `Baldr Router ${EXTENSION_VERSION} is installed and registered as an MCP server.`,
    'Open Baldr',
    'Later',
  );
  if (action === 'Open Baldr') await vscode.commands.executeCommand('baldr.open');
}

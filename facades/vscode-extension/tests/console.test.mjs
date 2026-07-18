import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const source = fs.readFileSync(path.join(root, 'src', 'console.ts'), 'utf8');
const runtimeSource = fs.readFileSync(path.join(root, 'src', 'runtime.ts'), 'utf8');
const presentationSource = fs.readFileSync(path.join(root, 'src', 'workItemPresentation.ts'), 'utf8');

function section(contents, start, end) {
  const startIndex = contents.indexOf(start);
  const endIndex = contents.indexOf(end, startIndex + start.length);
  assert.notEqual(startIndex, -1, `missing section start: ${start}`);
  assert.notEqual(endIndex, -1, `missing section end: ${end}`);
  return contents.slice(startIndex, endIndex);
}

test('embedded console script remains valid JavaScript', () => {
  const marker = '<script nonce="${nonce}">';
  const startIndex = source.indexOf(marker);
  const endIndex = source.indexOf('</script>', startIndex + marker.length);
  assert.notEqual(startIndex, -1);
  assert.notEqual(endIndex, -1);
  assert.doesNotThrow(() => new Function(source.slice(startIndex + marker.length, endIndex)));
});

test('console keeps the frozen setup/status/run facade contract', () => {
  assert.match(source, /runFacade\('run'/);
  assert.match(source, /consoleStatus/);
  assert.match(source, /setWorkspacePreferences/);
  assert.doesNotMatch(source, /sqlite3|better-sqlite3/);
});

test('console exposes session history, composer, inline plus menu, chips, and slash commands', () => {
  for (const marker of ['Tus sesiones', 'Escribí qué necesitás…', '¿Qué querés hacer?', 'Activas', 'Finalizadas', 'Archivadas', 'id="historySearch"', 'data-chip="git"', 'data-chip="preset"', 'data-chip="context"', 'id="plusMenu"', "type:'plusAction'", 'Archivos y carpetas', "case 'setup'", "case 'cancel'", "case 'resume'", "case 'archive'", "case 'restore'", "case 'delete'"]) {
    assert.ok(source.includes(marker), `missing console marker: ${marker}`);
  }
});

test('composer follows the aligned three-column toolbar and never truncates chip text', () => {
  assert.match(source, /\.composer-row\s*\{[^}]*grid-template-columns:\s*28px minmax\(0, 1fr\) 28px/);
  assert.match(source, /\.chip\s*\{[^}]*min-height:\s*26px/);
  assert.match(source, /@media \(max-width: 560px\)/);
  assert.doesNotMatch(source, /\.chip\s*\{[^}]*max-width:\s*100px/);
  assert.doesNotMatch(source, /\.composer-row\s*\{[^}]*justify-content:\s*center/);
});

test('composer uses stable SVG icons, focus states, and a disabled empty submit', () => {
  assert.match(source, /class="button-icon"/);
  assert.match(source, /\.input-shell:focus-within/);
  assert.match(source, /aria-controls="plusMenu"/);
  assert.match(source, /id="send"[^>]*disabled/);
  assert.match(source, /function updateSendState\(\)/);
  assert.match(source, /els\.plusMenu\.scrollTop=0/);
  assert.match(source, /prefers-reduced-motion/);
});

test('primary and secondary UI use plain Spanish wording', () => {
  for (const wording of ['Todavía no hay sesiones', 'Baldr lo organiza y te muestra el avance.', 'Protección de cambios', 'Nivel de detalle', 'Equipo de Baldr', 'Ayuda adicional', 'Pedir autorización', 'Trabajar directamente', 'Sin protección']) {
    assert.ok(source.includes(wording), `missing plain-language wording: ${wording}`);
  }
  for (const stale of ['No items yet', 'Git worktree', 'Context7 Auto', 'Baldr execution preset', 'durable draft', 'Con Git y respaldo', 'Con Git, en esta carpeta']) {
    assert.ok(!source.includes(stale), `stale technical wording remains: ${stale}`);
  }
});

test('direct work is the visible default while permission-gated and legacy modes remain available', () => {
  assert.match(source, /type SafetyMode = 'automatic' \| 'worktree' \| 'current' \| 'non-git'/);
  assert.match(source, /normalized === 'automatic' \|\| normalized === 'auto'/);
  assert.match(source, /automatic:\s*'Pedir autorización'/);
  assert.match(source, /worktree:\s*'Copia aislada'/);
  assert.match(source, /current:\s*'Trabajar directamente'/);
  assert.match(source, /'non-git':\s*'Sin protección'/);
  assert.match(source, /text\(preference\.safety_mode, 'current'\)/);
  assert.match(source, /id: 'current', label: '\$\(shield\) Trabajar directamente'/);
});

test('attachments render inside the composer and can be removed', () => {
  assert.match(source, /class="attachments" id="attachments"/);
  assert.match(source, /data-remove-pending/);
  assert.match(source, /case 'removePending'/);
  assert.match(source, /removePendingAttachment/);
});

test('work items narrate current activity, canonical stages, outcome, and attention', () => {
  for (const marker of [
    'card-eyebrow">Ahora',
    'Etapas y entregas',
    'Necesita tu atención',
    'Detalles técnicos de la sesión',
  ]) {
    assert.ok(source.includes(marker), `missing narrative progress marker: ${marker}`);
  }
  for (const wording of ['Entender y organizar', 'Hacer el trabajo', 'Comprobar el resultado', 'Trabajo listo']) {
    assert.ok(presentationSource.includes(wording), `missing stage wording: ${wording}`);
  }
  assert.match(source, /buildWorkItemPresentation\(selected\)/);
  assert.doesNotMatch(section(source, 'function stageHtml(', 'function stageStripHtml('), /participant|model|provider/);
});

test('final results render a Codex-style file change card with line totals', () => {
  assert.match(source, /function fileChangesHtml\(changes\)/);
  assert.match(source, /file-changes-title/);
  assert.match(source, /file-change-additions/);
  assert.match(source, /file-change-deletions/);
  assert.match(source, /fileChangesHtml\(outcome\.fileChanges\)/);
  assert.match(source, /archivos cambiados/);
});

test('file change rows open only real files contained by the active workspace', () => {
  const messageHandler = section(source, '  private async handleMessage(', '  private async submit(');
  const openHandler = section(source, '  private async openChangedFile(', '  private launchOperation(');
  assert.match(source, /data-open-changed-file/);
  assert.match(source, /type:'openChangedFile'/);
  assert.match(messageHandler, /case 'openChangedFile'/);
  assert.match(messageHandler, /this\.openChangedFile\(message\.path\)/);
  assert.match(openHandler, /isPathInsideRoot\(workspaceRoot, target\)/);
  assert.match(openHandler, /fs\.promises\.realpath/);
  assert.match(openHandler, /isPathInsideRoot\(canonicalRoot, canonicalTarget\)/);
  assert.match(openHandler, /vscode\.workspace\.openTextDocument/);
  assert.match(openHandler, /vscode\.window\.showTextDocument/);
  assert.match(openHandler, /probablemente fue eliminado/);
});

test('session detail leads with status and actions, then progressively reveals secondary content', () => {
  for (const marker of ['task-summary', 'task-meta-time', 'formatSessionWhen', 'Pedido original', 'Resultado final', 'Detalles técnicos del resultado']) {
    assert.ok(source.includes(marker), `missing P1 hierarchy marker: ${marker}`);
  }
  const render = section(source, 'function renderContent()', 'function shortModelLabel(');
  const now = render.indexOf('nowCardHtml');
  const actions = render.indexOf("'<div class=\"actions\">'");
  const request = render.indexOf('sessionRequestHtml');
  const progress = render.indexOf('sessionProgressHtml');
  const technical = render.indexOf('globalTechnicalHtml');
  assert.ok(now >= 0 && actions > now && request > actions && progress > request && technical > progress);
  assert.match(render, /presentation\?\.overallState==='complete'/);
  assert.match(render, /completed\?outcomeHtml\(presentation\.outcome,true\)/);
});

test('completed sessions continue as durable conversation turns with automatic editor context', () => {
  assert.match(source, /allowedActions\(selected\)\.includes\('continue'\)/);
  assert.match(source, /runtime\.continueWorkItem/);
  assert.match(source, /captureWorkspaceContext\(root\)/);
  assert.match(source, /Continuar esta conversación…/);
  assert.match(source, /Conversación \('/);
  assert.match(source, /data-plus-action="workspace"/);
  assert.match(source, /workspaceChoiceRequired/);
  assert.doesNotMatch(source, /workspaceFolders\?\.\[0\]\?\.uri\.fsPath/);
});

test('P2 preserves workspace, supports keyboard history, and constrains comfortable reading width', () => {
  for (const marker of [
    'id="historyToggle"',
    'id="historyPanel"',
    'id="historyStatus"',
    'data-clear-history',
    'data-start-request',
    'function moveHistoryFocus',
    'function focusHistorySearch',
    'historyExpanded',
    'draftText',
  ]) {
    assert.ok(source.includes(marker), `missing P2 continuity marker: ${marker}`);
  }
  assert.match(source, /--baldr-content-width:\s*720px/);
  assert.match(source, /\.content-background\s*\{[^}]*max-width:\s*var\(--baldr-content-width\)/);
  assert.match(source, /\.input-shell\s*\{[^}]*max-width:\s*var\(--baldr-content-width\)/);
  assert.match(source, /\(event\.ctrlKey\|\|event\.metaKey\).*key\)\.toLowerCase\(\)===['"]f['"]/);
});

test('phase deliverables are fetched only after explicit open or pagination actions', () => {
  const hostHandler = section(source, '  private async inspectDeliverable(', '  private async createDraft(');
  assert.match(hostHandler, /this\.runtime\.inspectWorkItemPhase\(/);
  assert.match(hostHandler, /stage as 'planning' \| 'execution' \| 'review'/);
  assert.match(hostHandler, /\{ runOrdinal, cursor: cursor \|\| undefined, pageSize: 30 \}/);
  assert.match(hostHandler, /append: Boolean\(cursor\)/);

  const openHandler = section(source, 'function openDeliverable(', 'function closeDeliverable(');
  const moreHandler = section(source, 'function loadMoreDeliverable(', 'function applyDeliverableResult(');
  assert.match(openHandler, /requestDeliverable\(descriptor(?:,''|,\s*['"]{2})?\)/);
  assert.match(moreHandler, /requestDeliverable\((?:deliverableView\.descriptor|descriptor),\s*cursor\)/);
  assert.equal((source.match(/type:'inspectDeliverable'/g) || []).length, 1);

  const render = section(source, 'function renderContent()', 'function shortModelLabel(');
  assert.doesNotMatch(render, /requestDeliverable\(/);
  assert.match(render, /\[data-deliverable-stage\]/);
  assert.match(render, /\[data-deliverable-more\]/);
});

test('phase deliverable failures expose fixed safe copy and never raw runtime errors', () => {
  const hostHandler = section(source, '  private async inspectDeliverable(', '  private async createDraft(');
  assert.match(hostHandler, /catch \{/);
  assert.match(hostHandler, /Baldr could not load the requested public phase deliverable/);
  assert.match(hostHandler, /No pudimos abrir la entrega\. Probá nuevamente\./);
  assert.doesNotMatch(hostHandler, /catch \(error\)|String\(error\)|error\.message/);
  assert.match(source, /msg\.type==='deliverableError'/);
  assert.match(source, /function applyDeliverableError\(/);
});

test('deliverable requests and results are correlated and the modal makes background controls inert', () => {
  const hostHandler = section(source, '  private async inspectDeliverable(', '  private async loadDeliverableIndex(');
  assert.match(hostHandler, /const responseContext = \{ itemId, descriptorDigest, requestId \}/);
  assert.match(hostHandler, /descriptorDigest !== returnedDigest/);
  assert.match(hostHandler, /\.\.\.responseContext/);
  assert.match(source, /Number\(message\?\.requestId\)===deliverableView\.requestId/);
  assert.match(source, /String\(message\?\.itemId\|\|''\)===deliverableView\.itemId/);
  assert.match(source, /String\(message\?\.descriptorDigest\|\|''\)===deliverableView\.descriptorDigest/);
  assert.match(source, /incomingItemId!==deliverableView\.itemId/);
  assert.match(source, /node\.inert=open/);
  assert.match(source, /node\.setAttribute\('aria-hidden','true'\)/);
  assert.match(source, /currentDeliverableBody\.scrollTop/);
  assert.match(source, /nextDeliverableBody\.scrollTop=deliverableScrollTop/);
});

test('older deliverable descriptors are loaded only on demand and correlated per page', () => {
  const hostHandler = section(source, '  private async loadDeliverableIndex(', '  private async createDraft(');
  assert.match(hostHandler, /this\.runtime\.listWorkItemDeliverables\(/);
  assert.match(hostHandler, /baldr-phase-deliverable-index-page/);
  assert.match(hostHandler, /buildPhaseDeliverablePresentations\(result\.items\)/);
  assert.match(source, /Ver entregas anteriores/);
  assert.match(source, /index\.truncated!==true/);
  assert.match(source, /type:'loadDeliverableIndex'/);
  assert.match(source, /Number\(message\?\.requestId\)===deliverableIndexView\.requestId/);
  assert.match(source, /String\(message\?\.cursor\|\|''\)===deliverableIndexView\.requestCursor/);
  assert.match(source, /new Map\(\)/);
});

test('failed task retry is offered only with explicit durable retryability evidence', () => {
  const primary = section(source, 'function attentionPrimaryAction(', 'function attentionHtml(');
  const actions = section(source, 'function actionButtons(', 'function renderContent(');
  assert.match(primary, /attention\.retryable===true/);
  assert.match(actions, /!attention\|\|attention\.retryable===true/);
  assert.match(actions, /attention\?'Volver a intentar':'Empezar'/);
});

test('starting or retrying a saved task refreshes its frozen team from current preferences', () => {
  const profiles = section(source, '  private currentRoleProfiles(', '  private async itemAction(');
  const action = section(source, '  private async itemAction(', '  private async inspectDeliverable(');
  const runtimeStart = section(runtimeSource, '  async startWorkItem(', '  async continueWorkItem(');
  assert.match(profiles, /preferences\.role_profiles/);
  assert.match(profiles, /for \(const role of BALDR_ROLES\)/);
  assert.match(action, /roleProfiles: this\.currentRoleProfiles\(\)/);
  assert.match(runtimeStart, /roleProfiles: options\.roleProfiles/);
});

test('stage disclosure is accessible and survives polling refreshes', () => {
  assert.match(source, /id="liveStatus"[^>]*aria-live="polite"/);
  assert.match(source, /class="stage-toggle"[^>]*aria-expanded/);
  assert.match(source, /vscode\.getState\(\)/);
  assert.match(source, /vscode\.setState\(\{expandedByItem,activeStageByItem,openDisclosuresByItem,historyFilter,historySearch,historyExpanded,draftText\}\)/);
  assert.match(source, /data-stage-status/);
  assert.match(source, /data-disclosure/);
  assert.match(source, /data-focus-key/);
  assert.match(source, /presentation\?\.revision/);
  assert.match(source, /if\(contentKey===lastContentKey\)return/);
  assert.match(source, /focus\(\{preventScroll:true\}\)/);
  assert.match(source, /aria-current="true"/);
  assert.match(source, /class="attention-card" role="alert"/);
  assert.match(source, /aria-busy/);
  assert.match(source, /state\.operationLabel/);
  assert.match(source, /role="status" aria-live="polite"/);
});

test('attention states retain readable foregrounds instead of relying on themed warning backgrounds', () => {
  assert.match(source, /--baldr-surface:/);
  assert.match(source, /\.attention-card, \.result-card\.warning \{[^}]*border-left-width:\s*4px/);
  assert.match(source, /\.report-section\.warning, \.report-section\.danger \{ color: var\(--vscode-foreground\); \}/);
  assert.doesNotMatch(source, /\.attention-card \{[^}]*inputValidation-warningBackground/);
  assert.doesNotMatch(source, /\.result-card\.warning \{[^}]*inputValidation-warningBackground/);
});

test('session history exposes archived work and lifecycle actions without hiding permanent deletion', () => {
  const consoleStatus = section(runtimeSource, '  async consoleStatus(', '  async setWorkspacePreferences(');
  assert.match(consoleStatus, /workbenchStatus\(workspaceRoot, workItemId, token, true\)/);
  assert.match(source, /function isHistoryItem\(item\)/);
  assert.match(source, /data-history-filter="archived"/);
  assert.match(source, /data-history-action=/);
  assert.match(source, /Eliminar permanentemente/);
  assert.match(source, /this\.runtime\.restoreWorkItem/);
  assert.match(source, /this\.runtime\.deleteWorkItem/);
  assert.match(source, /\{ modal: true \}/);
});

test('visible running tasks use adaptive single-flight polling', () => {
  assert.match(source, /POLL_FAST_MS\s*=\s*2_500/);
  assert.match(source, /POLL_STABLE_MS\s*=\s*5_000/);
  assert.match(source, /POLL_IDLE_MS\s*=\s*10_000/);
  assert.match(source, /if \(!this\.view\?\.visible \|\| !this\.shouldPoll\(\)\) return/);
  assert.match(source, /const changed = before !== this\.pollingRevision\(\)/);
  assert.match(source, /summary\.last_event_at/);
  assert.match(source, /selectedProgress\.overall_state/);
  assert.match(source, /this\.refreshPromise = this\.drainRefreshes\(\)/);
  assert.match(source, /while \(this\.refreshCompleted < this\.refreshRequested\)/);
  assert.doesNotMatch(source, /if \(this\.refreshing\) return/);
  assert.doesNotMatch(source, /setInterval\(/);
});

test('polling rerenders keep list and attachment focus while operations disable mutations', () => {
  assert.match(source, /lastTasksKey/);
  assert.match(source, /lastPendingKey/);
  assert.match(source, /els\.tasks\.scrollTop=previousScroll/);
  assert.match(source, /focusTarget\?\.focus\(\{preventScroll:true\}\)/);
  assert.match(source, /const disabled=state\.busy\?' disabled':''/);
  assert.match(source, /control\.disabled=blocked/);
  assert.match(source, /\['submit', 'plusAction', 'chip', 'itemAction'\]/);
});

test('a stale selected id falls back to the newest item in the current workspace', () => {
  assert.match(source, /if \(!record\(workbench\.selected\)\.id\)/);
  assert.match(source, /items\.find\(\(item\) => text\(item\.status\) !== 'archived'\) \?\? items\[0\]/);
  assert.match(source, /selectedStatus = await this\.runtime\.consoleStatus/);
});

test('refresh failures render a retryable non-technical callout and clear on success', () => {
  assert.match(source, /class="refresh-error" role="alert"/);
  assert.match(source, /No pudimos actualizar esta vista/);
  assert.match(source, /Tu sesión sigue guardada\. Probá nuevamente\./);
  assert.match(source, /data-refresh-action/);
  assert.match(source, /post\(\{type:'refresh'\}\)/);
  assert.match(source, /function updateState\(next\)\{const incoming=\{\.\.\.\(next\|\|\{\}\),error:''\}/);
  assert.match(source, /refreshErrorHtml\(Boolean\(presentation\?\.attention\)\)/);
});

test('narrative rendering escapes provider-controlled report and technical text', () => {
  const stageRenderer = section(source, 'function stageHtml(', 'function stageStripHtml(');
  const sectionRenderer = section(source, 'function sectionsHtml(', 'function technicalRowsHtml(');
  const technicalRenderer = section(source, 'function technicalRowsHtml(', 'function historyHtml(');
  assert.match(stageRenderer, /escapeHtml\(stage\.summary\)/);
  assert.match(sectionRenderer, /escapeHtml\(item\)/);
  assert.match(technicalRenderer, /escapeHtml\(row\.value\)/);
});

test('narrative cards remain single-column at a 240px sidebar width', () => {
  assert.match(source, /@media \(max-width: 300px\)/);
  assert.match(source, /\.stage-card\s*\{[^}]*min-width:\s*0/);
  assert.match(source, /\.stage-body\s*\{[^}]*overflow-wrap:\s*anywhere/);
  assert.match(source, /\.stage-toggle\s*\{[^}]*grid-template-columns:\s*20px minmax\(0, 1fr\) auto/);
});


test('console turns legacy Git policy blocks into authorization, unprotected, or open-folder choices', () => {
  assert.match(source, /workspace_git_required/);
  assert.match(source, /Pedir autorización/);
  assert.match(source, /Sin protección/);
  assert.match(source, /Abrir otra carpeta/);
});

test('recovery only shows actions authorized by the durable workspace state', () => {
  const recovery = section(source, '  private async chooseReconciliation(', '  private async attachCurrentFile(');
  assert.match(recovery, /const actions = allowedActions\(item\)/);
  assert.match(recovery, /filter\(\(option\) => actions\.includes\(option\.id\)\)/);
  assert.match(recovery, /Continuar con los archivos actuales/);
  assert.match(recovery, /no hay un respaldo para volver atrás/);
});

test('write permission choices resume or close the durable session directly', () => {
  const handler = section(source, '  private async itemAction(', '  private async inspectDeliverable(');
  const operation = section(source, '  private launchOperation(', '  private async handlePolicyBlock(');
  assert.match(handler, /RECONCILIATION_ACTIONS\.has\(action\)/);
  assert.match(handler, /Autorizando los cambios y retomando la sesión/);
  assert.match(handler, /Cerrando la sesión sin modificar archivos/);
  assert.match(source, /Autorizar cambios y reintentar<\/button>/);
  assert.match(source, /No autorizar<\/button>/);
  assert.match(operation, /resultStatus === 'awaiting_reconciliation'/);
  assert.match(operation, /allowAttentionPause/);
  assert.match(handler, /undefined,\s*false/);
});

test('Codex models are loaded lazily through the provider catalog and cached only on success', () => {
  const runtimeCatalog = section(runtimeSource, '  async providerModels(', '  qualificationProfile(');
  assert.match(runtimeSource, /PROVIDER_MODELS_CACHE_TTL_MS\s*=\s*5 \* 60 \* 1000/);
  assert.match(runtimeSource, /providerModelsCache = new Map/);
  assert.match(runtimeSource, /providerModelsRequests = new Map/);
  assert.match(runtimeCatalog, /\['provider-models', normalizedProvider\]/);
  assert.match(runtimeCatalog, /if \(result\.ok === true\)/);
  assert.match(runtimeCatalog, /Date\.now\(\) \+ PROVIDER_MODELS_CACHE_TTL_MS/);
  assert.match(runtimeCatalog, /providerModelsRequests\.delete\(normalizedProvider\)/);

  const catalogAdapter = section(source, '  private codexCatalogOptions(', '  private async chooseCodexTeamModels(');
  const chooser = section(source, '  private async chooseCodexTeamModels(', '  private async pickCodexModel(');
  assert.match(chooser, /this\.runtime\.providerModels\('codex', token\)/);
  assert.match(catalogAdapter, /text\(value\.model, text\(value\.id\)\)/);
  assert.match(catalogAdapter, /value\.reasoning_efforts/);
  assert.match(catalogAdapter, /record\(rawEffort\)\.id/);
  assert.match(catalogAdapter, /value\.default_reasoning_effort/);
});

test('Codex team selector offers only each model supported efforts and saves every role', () => {
  const chooser = section(source, '  private async chooseCodexTeamModels(', '  private async pickCodexModel(');
  const picker = section(source, '  private async pickCodexModel(', '  private resultReason(');

  assert.match(chooser, /id: 'same'/);
  assert.match(chooser, /id: 'per-role'/);
  assert.match(chooser, /for \(const role of BALDR_ROLES\)/);
  assert.match(chooser, /roleProfiles\[role\] = \[name\]/);
  assert.match(chooser, /this\.runtime\.upsertExecutionProfile\(root, \{/);
  assert.match(chooser, /provider: 'codex'/);
  assert.match(chooser, /model: selected\.model/);
  assert.match(chooser, /reasoning_effort: selected\.effort/);
  assert.match(chooser, /this\.runtime\.setWorkspacePreferences\(root, \{/);
  assert.match(chooser, /preset: 'custom'/);
  assert.match(chooser, /roleProfiles,/);

  assert.match(picker, /const orderedEfforts = \[\.\.\.model\.efforts\]/);
  assert.match(picker, /orderedEfforts\.map\(\(value\) => \(\{/);
  assert.match(picker, /title: `Variante de \$\{model\.displayName\}`/);
  assert.match(picker, /effort: effort\.id/);
  assert.doesNotMatch(picker, /\['low',\s*'medium',\s*'high'/);

  const finalSelection = chooser.lastIndexOf('if (!selected) return;');
  const firstWrite = chooser.indexOf('this.runtime.upsertExecutionProfile');
  assert.ok(finalSelection >= 0 && firstWrite > finalSelection, 'all role choices must finish before persistence starts');
});

test('catalog failure and selection cancellation leave the current team untouched', () => {
  const chooser = section(source, '  private async chooseCodexTeamModels(', '  private async pickCodexModel(');
  const fallback = section(source, '  private async offerModelCatalogFallback(', '  private async chooseSavedRoleProfiles(');
  const beforeModeSelection = chooser.slice(0, chooser.indexOf('    const mode ='));

  assert.match(beforeModeSelection, /await this\.offerModelCatalogFallback\(\);\s*return;/);
  assert.doesNotMatch(beforeModeSelection, /upsertExecutionProfile|setWorkspacePreferences/);
  assert.match(chooser, /if \(!mode\) return;/);
  assert.match(chooser, /if \(!selected\) return;/);
  assert.match(fallback, /Tu configuración actual no se modificó/);
  assert.doesNotMatch(fallback, /upsertExecutionProfile|setWorkspacePreferences/);
});

test('team menu keeps automatic selection first and only two explicit alternatives', () => {
  const mainMenu = section(source, '  private async chooseRoleProfiles(', '  private async chooseExternalAgent(');

  assert.match(mainMenu, /id: 'automatic'/);
  assert.match(mainMenu, /Automático \(recomendado\)/);
  assert.match(mainMenu, /id: 'per-stage'/);
  assert.match(mainMenu, /Codex o Kiro normal/);
  assert.match(mainMenu, /teamMode: 'automatic'/);
  assert.match(mainMenu, /agentOverrides: \{\}/);
  assert.doesNotMatch(mainMenu, /codex-models|saved|external-agents|restore-provider|advanced|Administrar agentes|Configurar equipo/);
});

test('external agents are loaded explicitly and assigned by immutable reference', () => {
  const runtimeCatalog = section(runtimeSource, '  async agentCatalog(', '  qualificationProfile(');
  const chooser = section(source, '  private async chooseExternalAgent(', '  private async manageExternalAgents(');

  assert.match(runtimeCatalog, /\['agent-catalog'\]/);
  assert.match(chooser, /this\.runtime\.agentCatalog\(this\.requireWorkspace\(\), token\)/);
  assert.match(chooser, /capabilities\.includes\('workspace\.read'\)/);
  assert.match(chooser, /capabilities\.includes\('workspace\.write'\)/);
  assert.match(chooser, /text\(agent\.effect_mode\) === 'workspace-write'/);
  assert.match(chooser, /if \(agent\.enabled === false\) return false/);
  assert.match(chooser, /`Kiro\$\{agentName/);
  assert.match(chooser, /canWrite \? 'Lectura y escritura' : 'Solo lectura'/);
  assert.match(chooser, /AgentRef: \$\{text\(agent\.ref\)\}/);
  assert.match(chooser, /this\.runtime\.setWorkspacePreferences\(root, \{/);
  assert.match(chooser, /teamMode: 'automatic'/);
  assert.match(chooser, /agentOverrides\[role\.id\] = reference/);
  assert.doesNotMatch(chooser, /upsertExecutionProfile|profileName/);
  assert.match(chooser, /No pudimos consultar los agentes registrados\. Tu equipo no cambió\./);
  assert.doesNotMatch(chooser, /target\.|authorization_env|endpoint/);
});

test('external agent UX exposes health, lifecycle, local management, and safe provider restore', () => {
  const chooser = section(source, '  private async chooseExternalAgent(', '  private async manageExternalAgents(');
  const manager = section(source, '  private async manageExternalAgents(', '  private async registerExternalAgent(');
  const register = section(source, '  private async registerExternalAgent(', '  private async restoreStandardProvider(');
  const restore = section(source, '  private async restoreStandardProvider(', '  private currentRoleProfile(');
  const runtimeCatalog = section(runtimeSource, '  async agentCatalog(', '  qualificationProfile(');

  assert.match(chooser, /text\(agent\.version\)/);
  assert.match(chooser, /const stateLabel = agent\.enabled === false/);
  assert.match(chooser, /stateLabel,/);
  assert.match(chooser, /last_success/);
  assert.match(chooser, /selected\.agent\.ready === false/);
  assert.match(manager, /\['inspect', text\(agent\.ref\)\]/);
  assert.match(manager, /'disable' : 'enable'/);
  assert.match(manager, /id: 'remove'/);
  assert.match(register, /'publish', reference/);
  assert.match(register, /workspace\.write/);
  assert.match(restore, /provider-codex-default|profileName = `provider-/);
  assert.match(restore, /provider: provider\.id/);
  assert.match(restore, /roleProfiles\[role\.id\] = \[profileName\]/);
  assert.match(runtimeCatalog, /\['agent', \.\.\.args\]/);
  assert.match(runtimeCatalog, /--workspace/);
});

test('team chip resolves actual named or inline role models and exposes role details', () => {
  assert.match(source, /function shortModelLabel\(value\)/);
  assert.match(source, /\(sol\|terra\|luna\|spark\)/);
  assert.match(source, /function configuredRole\(role\)/);
  assert.match(source, /profiles\.execution_profiles\[selected\]/);
  assert.match(source, /profiles\.resolved_roles/);
  assert.match(source, /config\.agent_ref\|\|config\.model/);
  assert.match(source, /const modelNames=\[\.\.\.new Set\(/);
  assert.match(source, /modelNames\.join\(' · '\)/);
  assert.match(source, /Planificación/);
  assert.match(source, /Ejecución/);
  assert.match(source, /Revisión/);
  assert.match(source, /effortChipLabel\(config\.reasoning_effort\|\|config\.effort\)/);
  assert.match(source, /els\.rolesChip\.title='Equipo de Baldr: '/);
});

test('plus-menu search hides unmatched rows, folds accents, groups results, and reports no matches', () => {
  assert.match(source, /\.plus-option\[hidden\][^{]*\{\s*display:\s*none;/);
  assert.match(source, /data-plus-heading="add"/);
  assert.match(source, /data-plus-heading="preferences"/);
  assert.match(source, /data-plus-group="add"/);
  assert.match(source, /data-plus-group="preferences"/);
  assert.match(source, /id="plusEmpty" hidden/);
  assert.match(source, /No encontramos una opción con ese nombre/);
  assert.match(source, /function normalizeSearch\(value\)\{[^}]*normalize\('NFD'\)\.replace\(/);

  const filter = section(source, 'function filterPlusActions()', 'function renderSlash()');
  assert.match(filter, /node\.hidden=Boolean\(query\)/);
  assert.match(filter, /heading\.dataset\.plusHeading/);
  assert.match(filter, /node\.dataset\.plusGroup===group/);
  assert.match(filter, /els\.plusEmpty\.hidden=actions\.some\(node=>!node\.hidden\)/);
});

test('plus-menu keyboard navigation activates the first result and moves through visible results', () => {
  assert.match(source, /function visiblePlusActions\(\)/);
  assert.match(source, /node\.addEventListener\('keydown',event=>\{/);
  assert.match(source, /event\.key!==['"]ArrowDown['"]&&event\.key!==['"]ArrowUp['"]/);
  assert.match(source, /actions\[next\]\?\.focus\(\)/);
  assert.match(source, /els\.plusFilter\.addEventListener\('keydown',event=>\{/);
  assert.match(source, /event\.key!==['"]Enter['"]&&event\.key!==['"]ArrowDown['"]&&event\.key!==['"]ArrowUp['"]/);
  assert.match(source, /if\(event\.key===['"]Enter['"]\)actions\[0\]\.click\(\)/);
  assert.match(source, /actions\[actions\.length-1\]\.focus\(\)/);
});

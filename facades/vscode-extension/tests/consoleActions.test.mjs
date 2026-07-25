import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

import {
  GENERIC_WORK_ITEM_ERROR,
  PLUS_ACTIONS,
  PLUS_ACTION_IDS,
  UI_LABELS,
  WORK_ITEM_ERROR_COPY,
  contextModeLabel,
  plusMenuHtml,
  plusQuickPickItems,
  presetModeLabel,
  safetyModeLabel,
  webviewCopyPayload,
  workItemErrorMessage,
} from '../dist/consoleActions.js';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const consoleSource = fs.readFileSync(path.join(root, 'src', 'console.ts'), 'utf8');

test('the gear quick pick and the inline menu offer exactly the same actions', () => {
  const menu = plusMenuHtml();
  const inline = [...menu.matchAll(/data-plus-action="([^"]+)"/g)].map((match) => match[1]);
  const quickPick = plusQuickPickItems().map((item) => item.id);

  assert.deepEqual(inline, [...PLUS_ACTION_IDS]);
  assert.deepEqual(quickPick, [...PLUS_ACTION_IDS]);
  // Regression: the inline menu used to omit agents, profile-create, status and
  // logs while the gear offered them.
  for (const id of ['agents', 'profile-create', 'status', 'logs']) {
    assert.ok(inline.includes(id), `inline menu is missing ${id}`);
  }
});

test('every catalog action is handled by the console', () => {
  for (const id of PLUS_ACTION_IDS) {
    assert.ok(
      consoleSource.includes(`case '${id}':`),
      `handlePlusAction does not handle ${id}`,
    );
  }
});

test('catalog entries are complete and unique', () => {
  const ids = new Set();
  for (const action of PLUS_ACTIONS) {
    assert.ok(action.label.trim(), `missing label for ${action.id}`);
    assert.ok(action.detail.trim(), `missing detail for ${action.id}`);
    assert.ok(action.codicon.trim(), `missing codicon for ${action.id}`);
    assert.ok(action.glyph.trim(), `missing glyph for ${action.id}`);
    assert.ok(['add', 'preferences', 'tools'].includes(action.group));
    assert.ok(!ids.has(action.id), `duplicate action id ${action.id}`);
    ids.add(action.id);
  }
});

test('the search field precedes the options so tab order matches reading order', () => {
  const menu = plusMenuHtml();

  assert.ok(menu.indexOf('id="plusFilter"') < menu.indexOf('data-plus-action='));
});

test('menu markup escapes its own copy', () => {
  assert.doesNotMatch(plusMenuHtml(), /<script/i);
});

test('durable error codes never reach the user as raw identifiers', () => {
  for (const code of Object.keys(WORK_ITEM_ERROR_COPY)) {
    const message = workItemErrorMessage({ error_code: code });
    assert.ok(message.length > 20, `copy too short for ${code}`);
    assert.ok(!message.includes(code), `raw code leaked for ${code}`);
  }

  const unknown = workItemErrorMessage({ error_code: 'some_internal_state_code' });
  assert.equal(unknown, GENERIC_WORK_ITEM_ERROR);
  assert.ok(!unknown.includes('some_internal_state_code'));
});

test('a human reason wins over generic copy, and an empty failure says nothing', () => {
  assert.equal(
    workItemErrorMessage({ error_code: 'unknown_code', error_reason: 'El proveedor no respondió.' }),
    'El proveedor no respondió.',
  );
  assert.equal(workItemErrorMessage({}), '');
  assert.equal(
    workItemErrorMessage({ error_code: 'unknown_code', error_reason: 'unknown_code' }),
    GENERIC_WORK_ITEM_ERROR,
  );
});

test('the non-Git reconciliation case keeps its specific explanation', () => {
  const message = workItemErrorMessage({
    error_code: 'workspace_reconciliation_required',
    safety_mode: 'non-git',
  });

  assert.match(message, /respaldo Git que esta carpeta no usa/);
  assert.notEqual(message, WORK_ITEM_ERROR_COPY.workspace_reconciliation_required);
});

test('the webview payload carries the same wording as the host accessors', () => {
  const payload = JSON.parse(webviewCopyPayload());

  assert.deepEqual(payload.labels, JSON.parse(JSON.stringify(UI_LABELS)));
  assert.equal(payload.labels.safety.automatic, safetyModeLabel('automatic'));
  assert.equal(payload.labels.preset.balanced, presetModeLabel('balanced'));
  assert.equal(payload.labels.context.auto, contextModeLabel('auto'));
  assert.equal(payload.genericError, GENERIC_WORK_ITEM_ERROR);
});

test('preference labels fall back to the safest default', () => {
  assert.equal(safetyModeLabel('unheard-of'), 'Trabajar directamente');
  assert.equal(presetModeLabel('unheard-of'), 'Estándar');
  assert.equal(contextModeLabel('unheard-of'), 'Ayuda automática');
});

test('the console no longer declares its own copy of the shared wording', () => {
  assert.ok(!consoleSource.includes("balanced: 'Estándar'"));
  assert.ok(!consoleSource.includes("auto:'Ayuda automática'"));
  assert.match(consoleSource, /from '\.\/consoleActions\.js'/);
});

import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const source = fs.readFileSync(path.join(root, 'src', 'console.ts'), 'utf8');

class FakeClassList {
  values = new Set();
  add(...values) { for (const value of values) this.values.add(value); }
  remove(...values) { for (const value of values) this.values.delete(value); }
  contains(value) { return this.values.has(value); }
  toggle(value, force) {
    const enabled = force === undefined ? !this.values.has(value) : Boolean(force);
    if (enabled) this.values.add(value); else this.values.delete(value);
    return enabled;
  }
}

class FakeElement {
  constructor(id = '') { this.id = id; }
  id;
  ownerDocument = null;
  parentRoot = null;
  generated = new Map();
  _innerHTML = '';
  textContent = '';
  value = '';
  disabled = false;
  hidden = false;
  open = false;
  scrollTop = 0;
  scrollHeight = 24;
  style = {};
  dataset = {};
  classList = new FakeClassList();
  attributes = new Map();
  listeners = new Map();
  inert = false;
  get innerHTML() { return this._innerHTML; }
  set innerHTML(value) {
    if (this.ownerDocument?.activeElement?.parentRoot === this) this.ownerDocument.activeElement = null;
    this._innerHTML = String(value ?? '');
    this.generated = new Map();
    if (this.id === 'tasks') {
      const itemNodes = [];
      for (const match of this._innerHTML.matchAll(/data-item="([^"]+)"/g)) {
        const node = new FakeElement(`item:${match[1]}`);
        node.ownerDocument = this.ownerDocument;
        node.parentRoot = this;
        node.dataset.item = match[1];
        itemNodes.push(node);
      }
      this.generated.set('[data-item]', itemNodes);
      if (this._innerHTML.includes('data-clear-history')) {
        const clear = new FakeElement('clear-history');
        clear.ownerDocument = this.ownerDocument;
        clear.parentRoot = this;
        this.generated.set('[data-clear-history]', clear);
      }
      return;
    }
    if (this.id !== 'content') return;
    const create = (key) => {
      const node = new FakeElement(key);
      node.ownerDocument = this.ownerDocument;
      node.parentRoot = this;
      this.generated.set(key, node);
      return node;
    };
    if (this._innerHTML.includes('class="deliverable-body"')) create('.deliverable-body');
    if (this._innerHTML.includes('class="deliverable-panel"')) create('.deliverable-panel');
    if (this._innerHTML.includes('data-deliverable-background')) create('[data-deliverable-background]');
    if (this._innerHTML.includes('data-deliverable-technical')) {
      const technical = create('[data-deliverable-technical]');
      technical.open = /data-deliverable-technical\s+open/.test(this._innerHTML);
    }
    for (const selector of ['close', 'more', 'retry']) {
      if (this._innerHTML.includes(`data-deliverable-${selector}`)) {
        create(`[data-deliverable-${selector}]`);
      }
    }
    const focusNodes = [];
    for (const match of this._innerHTML.matchAll(/data-focus-key="([^"]+)"/g)) {
      const node = create(`focus:${match[1]}:${focusNodes.length}`);
      node.dataset.focusKey = match[1];
      focusNodes.push(node);
    }
    this.generated.set('[data-focus-key]', focusNodes);
  }
  addEventListener(type, listener) { this.listeners.set(type, listener); }
  setAttribute(name, value) { this.attributes.set(name, String(value)); }
  removeAttribute(name) { this.attributes.delete(name); }
  getAttribute(name) { return this.attributes.get(name) ?? null; }
  querySelectorAll(selector) {
    if (this.generated.has(selector)) {
      const value = this.generated.get(selector);
      return Array.isArray(value) ? value : [value];
    }
    if (selector.startsWith('button:not') && this.id === '.deliverable-panel') {
      return [...(this.parentRoot?.querySelectorAll('[data-focus-key]') ?? [])]
        .filter((node) => ['deliverable-close', 'deliverable-more', 'deliverable-retry', 'deliverable-technical'].includes(node.dataset.focusKey));
    }
    return [];
  }
  querySelector(selector) { return this.generated.get(selector) ?? null; }
  contains(value) {
    if (value === this || value?.parentRoot === this) return true;
    return this.id === '.deliverable-panel'
      && ['deliverable-close', 'deliverable-more', 'deliverable-retry', 'deliverable-technical'].includes(value?.dataset?.focusKey);
  }
  focus() { if (this.ownerDocument) this.ownerDocument.activeElement = this; }
}

function embeddedScript() {
  const marker = '<script nonce="${nonce}">';
  const start = source.indexOf(marker);
  const end = source.indexOf('</script>', start + marker.length);
  assert.notEqual(start, -1);
  assert.notEqual(end, -1);
  return source.slice(start + marker.length, end);
}

function createHarness(persistedState = {}) {
  const ids = [
    'header', 'tasks', 'content', 'composer', 'input', 'send', 'plus', 'configure', 'refresh',
    'gitChip', 'gitChipLabel', 'presetChip', 'presetChipLabel', 'rolesChip',
    'rolesChipLabel', 'contextChip', 'contextChipLabel', 'attachments', 'slash',
    'plusMenu', 'plusFilter', 'plusEmpty', 'loading', 'liveStatus', 'historySearch',
    'historyPanel', 'historyToggle', 'historyStatus',
  ];
  const elements = Object.fromEntries(ids.map((id) => [id, new FakeElement(id)]));
  const historyFilters = ['active', 'completed', 'archived'].map((value) => {
    const element = new FakeElement(`history-filter-${value}`);
    element.dataset.historyFilter = value;
    element.dataset.historyLabel = {
      active: 'Activas', completed: 'Finalizadas', archived: 'Archivadas',
    }[value];
    return element;
  });
  const documentListeners = new Map();
  const document = {
    activeElement: null,
    getElementById: (id) => elements[id] ?? null,
    querySelectorAll: (selector) => selector === '[data-history-filter]' ? historyFilters : [],
    addEventListener: (type, listener) => documentListeners.set(type, listener),
  };
  for (const element of Object.values(elements)) element.ownerDocument = document;
  for (const element of historyFilters) element.ownerDocument = document;
  const windowListeners = new Map();
  const window = {
    addEventListener: (type, listener) => windowListeners.set(type, listener),
  };
  const messages = [];
  const savedStates = [];
  const vscode = {
    getState: () => persistedState,
    setState: (value) => savedStates.push(value),
    postMessage: (value) => messages.push(value),
  };
  const run = new Function(
    'acquireVsCodeApi',
    'document',
    'window',
    `${embeddedScript()}\nreturn { openDeliverable, closeDeliverable, loadMoreDeliverable, requestDeliverableIndex, view: () => deliverableView, indexView: () => deliverableIndexView };`,
  );
  const hooks = run(() => vscode, document, window);
  const receive = (message) => windowListeners.get('message')?.({ data: message });
  const keydown = (key, options = {}) => {
    const event = { key, shiftKey: false, prevented: false, preventDefault() { this.prevented = true; }, ...options };
    documentListeners.get('keydown')?.(event);
    return event;
  };
  return { elements, historyFilters, hooks, keydown, messages, receive, savedStates };
}

function presentation(revision, summary) {
  const stages = [
    {
      id: 'planning', title: 'Planificación', subtitle: 'Entender y organizar',
      state: 'complete', statusLabel: 'Plan listo', purpose: 'El plan está listo.',
      summary: 'Alcance acordado.', facts: [], sections: [], milestones: [], history: [],
      technicalRows: [], technicalSections: [], deliverables: [{
        stage: 'planning', round: 0, runOrdinal: 1, itemRevision: 4,
        availability: 'available', reason: '', digest: 'a'.repeat(64),
        createdAt: '2026-07-12T10:02:00Z', preview: 'Plan disponible.', entryCount: 2,
      }],
    },
    {
      id: 'execution', title: 'Ejecución', subtitle: 'Hacer el trabajo',
      state: 'active', statusLabel: 'Trabajando ahora', purpose: 'Baldr está haciendo los cambios.',
      summary, facts: [], sections: [], history: [], technicalRows: [], technicalSections: [],
      milestones: [{ label: 'Comenzó a hacer los cambios', state: 'running', evidence: 'observed' }],
      deliverables: [],
    },
    {
      id: 'review', title: 'Revisión', subtitle: 'Comprobar el resultado',
      state: 'pending', statusLabel: 'Todavía no empezó', purpose: 'Baldr comprobará el resultado.',
      summary: '', facts: [], sections: [], milestones: [], history: [],
      technicalRows: [], technicalSections: [], deliverables: [],
    },
  ];
  return {
    revision,
    overallState: 'working',
    activeStage: 'execution',
    headline: 'Haciendo los cambios',
    explanation: 'Baldr está trabajando según el plan acordado.',
    stages,
    deliverableIndex: { total: 1, returned: 1, truncated: false, nextCursor: '' },
    milestones: stages[1].milestones,
    attention: null,
    outcome: null,
    technicalRows: [],
  };
}

function state(revision = 'r1', summary = 'Primera actualización.', busy = false, itemId = 'wi-1') {
  return {
    trusted: true,
    busy,
    operationLabel: busy ? 'Aplicando los cambios…' : '',
    pending: { attachments: [] },
    workbench: {
      items: [{
        id: itemId, title: 'Tarea <privada>', status: 'running',
        updated_at: '2026-07-12T10:02:00Z', allowed_actions: ['cancel'],
        progress_summary: {
          activity: 'Preparando cambios seguros',
          last_event_at: '2026-07-12T10:02:00Z',
        },
      }],
      preferences: {}, profiles: {}, options: {},
      selected: {
        id: itemId, title: 'Tarea <privada>', task: 'Corregí <script>alert(1)</script>',
        status: 'running', preset: 'balanced', safety_mode: 'automatic',
        allowed_actions: ['cancel'], presentation: presentation(revision, summary),
      },
    },
  };
}

test('real webview script renders escaped narrative stages and evidence', () => {
  const harness = createHarness();
  assert.deepEqual(harness.messages.at(-1), { type: 'ready' });

  harness.receive({ type: 'state', state: state() });
  const html = harness.elements.content.innerHTML;
  assert.match(html, /Ahora/);
  assert.match(html, /Planificación/);
  assert.match(html, /Ejecución/);
  assert.match(html, /Revisión/);
  assert.match(html, /Hitos/);
  assert.match(html, /Registrado por Baldr/);
  assert.match(html, /Tarea &lt;privada&gt;/);
  assert.match(html, /Corregí &lt;script&gt;alert\(1\)&lt;\/script&gt;/);
  assert.doesNotMatch(html, /<script>alert/);
  assert.match(html, /aria-label="Ejecución: Trabajando ahora\./);
  assert.match(harness.elements.tasks.innerHTML, /class="task-summary">Preparando cambios seguros/);
  assert.match(harness.elements.tasks.innerHTML, /class="task-meta-time">/);
  assert.match(harness.elements.tasks.innerHTML, /Tarea &lt;privada&gt;/);
});

test('webview restores the P2 draft and history view while shortcuts keep both reachable', () => {
  const harness = createHarness({
    draftText: 'Pedido todavía no enviado',
    historyExpanded: false,
    historyFilter: 'completed',
    historySearch: 'router',
  });

  assert.equal(harness.elements.input.value, 'Pedido todavía no enviado');
  assert.equal(harness.elements.send.disabled, false);
  assert.equal(harness.elements.historyPanel.hidden, true);
  assert.equal(harness.elements.historyToggle.getAttribute('aria-expanded'), 'false');

  harness.elements.historyToggle.listeners.get('click')();
  assert.equal(harness.elements.historyPanel.hidden, false);
  assert.equal(harness.savedStates.at(-1).historyExpanded, true);
  assert.equal(harness.savedStates.at(-1).draftText, 'Pedido todavía no enviado');

  harness.historyFilters[2].listeners.get('click')();
  assert.equal(harness.savedStates.at(-1).historyFilter, 'archived');
  assert.equal(harness.savedStates.at(-1).historySearch, 'router');

  harness.elements.input.value = 'Borrador actualizado';
  harness.elements.input.listeners.get('input')();
  assert.equal(harness.savedStates.at(-1).draftText, 'Borrador actualizado');

  const shortcut = harness.keydown('f', { ctrlKey: true });
  assert.equal(shortcut.prevented, true);
  assert.equal(harness.elements.content.ownerDocument.activeElement, harness.elements.historySearch);

  harness.receive({ type: 'clearInput' });
  assert.equal(harness.elements.input.value, '');
  assert.equal(harness.savedStates.at(-1).draftText, '');
});

test('history keyboard navigation and empty search recovery remain usable after rerenders', () => {
  const harness = createHarness();
  const initial = state();
  initial.workbench.items.push({
    ...initial.workbench.items[0],
    id: 'wi-2',
    title: 'Segunda sesión',
    updated_at: '2026-07-12T11:02:00Z',
  });
  harness.receive({ type: 'state', state: initial });

  const items = harness.elements.tasks.querySelectorAll('[data-item]');
  assert.equal(items.length, 2);
  items[0].focus();
  const down = {
    key: 'ArrowDown', prevented: false,
    preventDefault() { this.prevented = true; },
  };
  items[0].listeners.get('keydown')(down);
  assert.equal(down.prevented, true);
  assert.equal(harness.elements.content.ownerDocument.activeElement, items[1]);

  harness.elements.historySearch.value = 'sin coincidencias';
  harness.elements.historySearch.listeners.get('input')();
  assert.match(harness.elements.tasks.innerHTML, /No encontramos sesiones/);
  const clear = harness.elements.tasks.querySelector('[data-clear-history]');
  clear.listeners.get('click')();
  assert.equal(harness.elements.historySearch.value, '');
  assert.equal(
    harness.elements.content.ownerDocument.activeElement,
    harness.elements.historySearch,
  );
});

test('webview announces same-stage narrative revisions and disables mutations while busy', () => {
  const harness = createHarness();
  harness.receive({ type: 'state', state: state('r1', 'Primera actualización.') });
  assert.match(harness.elements.liveStatus.textContent, /Primera actualización/);

  harness.receive({ type: 'state', state: state('r2', 'Segundo avance comprobable.') });
  assert.match(harness.elements.liveStatus.textContent, /Segundo avance comprobable/);

  harness.receive({ type: 'operation', busy: true, label: 'Aplicando los cambios…' });
  assert.equal(harness.elements.content.attributes.get('aria-busy'), 'true');
  assert.match(harness.elements.content.innerHTML, /Aplicando los cambios…/);
  assert.match(harness.elements.content.innerHTML, /data-action="cancel"[^>]*disabled/);
  assert.equal(harness.elements.send.disabled, true);
  assert.equal(harness.elements.gitChip.disabled, true);
});

test('webview requests complete phase content only on open and explicit pagination', () => {
  const harness = createHarness();
  harness.receive({ type: 'state', state: state() });
  assert.deepEqual(harness.messages, [{ type: 'ready' }]);

  harness.hooks.openDeliverable({
    dataset: {
      deliverableStage: 'planning',
      deliverableRound: '0',
      deliverableRun: '1',
      focusKey: 'deliverable-planning-1-0',
    },
  });
  const firstRequest = harness.messages.at(-1);
  assert.deepEqual(firstRequest, {
    type: 'inspectDeliverable',
    itemId: 'wi-1',
    stage: 'planning',
    round: 0,
    runOrdinal: 1,
    cursor: undefined,
    descriptorDigest: 'a'.repeat(64),
    requestId: 1,
  });
  assert.equal(harness.elements.header.inert, true);
  assert.equal(harness.elements.composer.inert, true);
  assert.equal(harness.elements.header.getAttribute('aria-hidden'), 'true');
  assert.equal(harness.elements.send.disabled, true);
  assert.equal(harness.elements.content.style.overflow, 'hidden');
  assert.equal(harness.elements.content.querySelector('[data-deliverable-background]').inert, true);
  assert.equal(harness.elements.content.ownerDocument.activeElement?.dataset.focusKey, 'deliverable-close');

  harness.receive({
    type: 'deliverableResult',
    itemId: firstRequest.itemId,
    descriptorDigest: firstRequest.descriptorDigest,
    requestId: firstRequest.requestId,
    append: false,
    deliverable: {
      stage: 'planning', round: 0, run_ordinal: 1, availability: 'available',
      page: {
        entries: [{ section: 'summary', kind: 'value', value: 'Primer resultado seguro.', technical: false }],
        has_more: true,
        next_cursor: 'opaque-next-page',
      },
    },
  });
  assert.match(harness.elements.content.innerHTML, /Primer resultado seguro/);
  assert.equal(harness.elements.content.ownerDocument.activeElement?.dataset.focusKey, 'deliverable-more');
  const panel = harness.elements.content.querySelector('.deliverable-panel');
  const panelControls = panel.querySelectorAll('button:not([disabled]), summary, [href], input:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])');
  const closeControl = panelControls.find((node) => node.dataset.focusKey === 'deliverable-close');
  const moreControl = panelControls.find((node) => node.dataset.focusKey === 'deliverable-more');
  moreControl.focus();
  const forwardWrap = { key: 'Tab', shiftKey: false, prevented: false, preventDefault() { this.prevented = true; } };
  panel.listeners.get('keydown')(forwardWrap);
  assert.equal(forwardWrap.prevented, true);
  assert.equal(harness.elements.content.ownerDocument.activeElement?.dataset.focusKey, 'deliverable-close');
  closeControl.focus();
  const backwardWrap = { key: 'Tab', shiftKey: true, prevented: false, preventDefault() { this.prevented = true; } };
  panel.listeners.get('keydown')(backwardWrap);
  assert.equal(backwardWrap.prevented, true);
  assert.equal(harness.elements.content.ownerDocument.activeElement?.dataset.focusKey, 'deliverable-more');

  harness.elements.content.querySelector('.deliverable-body').scrollTop = 47;

  harness.hooks.loadMoreDeliverable();
  const secondRequest = harness.messages.at(-1);
  assert.deepEqual(secondRequest, {
    type: 'inspectDeliverable',
    itemId: 'wi-1',
    stage: 'planning',
    round: 0,
    runOrdinal: 1,
    cursor: 'opaque-next-page',
    descriptorDigest: 'a'.repeat(64),
    requestId: 2,
  });
  assert.equal(harness.elements.content.querySelector('.deliverable-body').scrollTop, 47);

  harness.receive({
    type: 'deliverableResult',
    itemId: secondRequest.itemId,
    descriptorDigest: secondRequest.descriptorDigest,
    requestId: secondRequest.requestId,
    append: true,
    deliverable: {
      stage: 'planning', round: 0, run_ordinal: 1, availability: 'available',
      page: {
        entries: [{ section: 'scope', kind: 'item', value: 'Segundo resultado seguro.', technical: false }],
        has_more: false,
        next_cursor: null,
      },
    },
  });
  assert.match(harness.elements.content.innerHTML, /Primer resultado seguro/);
  assert.match(harness.elements.content.innerHTML, /Segundo resultado seguro/);
  assert.equal(harness.elements.content.querySelector('.deliverable-body').scrollTop, 47);
  assert.equal(harness.elements.content.ownerDocument.activeElement?.dataset.focusKey, 'deliverable-close');

  const escape = harness.keydown('Escape');
  assert.equal(escape.prevented, true);
  assert.equal(harness.hooks.view().open, false);
  assert.equal(harness.elements.header.inert, false);
  assert.equal(harness.elements.composer.inert, false);
  assert.equal(harness.elements.header.getAttribute('aria-hidden'), null);
  assert.equal(harness.elements.content.style.overflow, '');
  assert.equal(
    harness.elements.content.ownerDocument.activeElement?.dataset.focusKey,
    'deliverable-planning-1-0',
  );
});

test('webview formats decision objects and preserves an open focused technical disclosure on refresh', () => {
  const harness = createHarness();
  harness.receive({ type: 'state', state: state() });
  harness.hooks.openDeliverable({
    dataset: {
      deliverableStage: 'planning', deliverableRound: '0', deliverableRun: '1',
      focusKey: 'deliverable-planning-1-0',
    },
  });
  const request = harness.messages.at(-1);
  harness.receive({
    type: 'deliverableResult',
    itemId: request.itemId,
    descriptorDigest: request.descriptorDigest,
    requestId: request.requestId,
    append: false,
    deliverable: {
      stage: 'planning', round: 0, run_ordinal: 1, availability: 'available',
      page: {
        entries: [
          {
            section: 'decisions', kind: 'value',
            value: { key: 'alcance', value: 'Consola' }, technical: false,
          },
          {
            section: 'commands_run', kind: 'item', value: 'npm test', technical: true,
          },
        ],
        has_more: false,
        next_cursor: null,
      },
    },
  });

  assert.match(harness.elements.content.innerHTML, /alcance: Consola/);
  assert.doesNotMatch(harness.elements.content.innerHTML, /\[object Object\]/);
  const technical = harness.elements.content.querySelector('[data-deliverable-technical]');
  assert.equal(technical.open, false);
  technical.open = true;
  technical.listeners.get('toggle')({ currentTarget: technical });
  const technicalSummary = harness.elements.content.querySelectorAll('[data-focus-key]')
    .find((node) => node.dataset.focusKey === 'deliverable-technical');
  technicalSummary.focus();

  harness.receive({
    type: 'state',
    state: state('r2', 'El contenido de la tarea se actualizó.'),
  });

  const refreshedTechnical = harness.elements.content.querySelector('[data-deliverable-technical]');
  assert.equal(refreshedTechnical.open, true);
  assert.equal(
    harness.elements.content.ownerDocument.activeElement?.dataset.focusKey,
    'deliverable-technical',
  );
  assert.match(harness.elements.content.innerHTML, /npm test/);
});

test('webview rejects stale deliverable responses across close, reopen, digest, and item changes', () => {
  const harness = createHarness();
  const trigger = {
    dataset: {
      deliverableStage: 'planning', deliverableRound: '0', deliverableRun: '1',
      focusKey: 'deliverable-planning-1-0',
    },
  };
  harness.receive({ type: 'state', state: state() });
  harness.hooks.openDeliverable(trigger);
  const staleRequest = harness.messages.at(-1);
  harness.hooks.closeDeliverable(true);
  harness.hooks.openDeliverable(trigger);
  const currentRequest = harness.messages.at(-1);
  assert.ok(currentRequest.requestId > staleRequest.requestId);

  const resultFor = (request, value, overrides = {}) => ({
    type: 'deliverableResult',
    itemId: request.itemId,
    descriptorDigest: request.descriptorDigest,
    requestId: request.requestId,
    append: false,
    deliverable: {
      stage: 'planning', round: 0, run_ordinal: 1, availability: 'available',
      page: {
        entries: [{ section: 'summary', kind: 'value', value, technical: false }],
        has_more: false, next_cursor: null,
      },
    },
    ...overrides,
  });

  harness.receive(resultFor(staleRequest, 'RESPUESTA VIEJA'));
  assert.doesNotMatch(harness.elements.content.innerHTML, /RESPUESTA VIEJA/);
  harness.receive(resultFor(currentRequest, 'DIGEST INCORRECTO', { descriptorDigest: 'b'.repeat(64) }));
  assert.doesNotMatch(harness.elements.content.innerHTML, /DIGEST INCORRECTO/);
  harness.receive(resultFor(currentRequest, 'RESPUESTA ACTUAL'));
  assert.match(harness.elements.content.innerHTML, /RESPUESTA ACTUAL/);

  harness.hooks.closeDeliverable(true);
  harness.hooks.openDeliverable(trigger);
  const requestBeforeSelectionChange = harness.messages.at(-1);
  harness.receive({ type: 'state', state: state('r2', 'Otra tarea.', false, 'wi-2') });
  assert.equal(harness.hooks.view().open, false);
  assert.equal(harness.elements.header.inert, false);
  assert.equal(harness.elements.content.ownerDocument.activeElement?.dataset.focusKey, undefined);
  harness.receive(resultFor(requestBeforeSelectionChange, 'RESPUESTA DE OTRA TAREA'));
  assert.doesNotMatch(harness.elements.content.innerHTML, /RESPUESTA DE OTRA TAREA/);
});

test('webview loads truncated historical descriptors on demand, deduplicates pages, and rejects stale index responses', () => {
  const harness = createHarness();
  const initial = state();
  initial.workbench.selected.presentation.deliverableIndex = {
    total: 3, returned: 1, truncated: true, nextCursor: 'older-page-1',
  };
  harness.receive({ type: 'state', state: initial });
  assert.match(harness.elements.content.innerHTML, /Ver entregas anteriores/);
  assert.equal(harness.messages.length, 1, 'polling state must not load historical descriptors');

  harness.hooks.requestDeliverableIndex();
  const first = harness.messages.at(-1);
  assert.deepEqual(
    { ...first, requestId: undefined },
    { type: 'loadDeliverableIndex', itemId: 'wi-1', cursor: 'older-page-1', requestId: undefined },
  );
  assert.ok(Number.isSafeInteger(first.requestId) && first.requestId > 0);
  harness.receive({
    type: 'deliverableIndexResult', itemId: 'wi-1', cursor: 'older-page-1', requestId: 999,
    items: [{ stage: 'planning', round: 4, runOrdinal: 1, digest: 'stale', availability: 'available', preview: 'Índice viejo.' }],
    page: { has_more: false, next_cursor: null },
  });
  assert.doesNotMatch(harness.elements.content.innerHTML, /Índice viejo/);

  const duplicate = {
    stage: 'planning', round: 0, runOrdinal: 1, itemRevision: 4,
    availability: 'available', reason: '', digest: 'a'.repeat(64),
    createdAt: '2026-07-12T10:02:00Z', preview: 'Plan disponible.', entryCount: 2,
  };
  harness.receive({
    type: 'deliverableIndexResult', itemId: first.itemId, cursor: first.cursor, requestId: first.requestId,
    items: [duplicate, {
      stage: 'execution', round: 0, runOrdinal: 1, itemRevision: 2,
      availability: 'summary_only', reason: 'Está disponible el resumen de esta entrega.',
      digest: '', createdAt: '2026-07-12T09:00:00Z', preview: 'Ejecución anterior.', entryCount: 1,
    }],
    page: { has_more: true, next_cursor: 'older-page-2' },
  });
  assert.equal((harness.elements.content.innerHTML.match(/Plan disponible\./g) || []).length, 1);
  assert.match(harness.elements.content.innerHTML, /Ejecución anterior/);
  assert.match(harness.elements.content.innerHTML, /Ver entregas anteriores/);

  harness.hooks.requestDeliverableIndex();
  const second = harness.messages.at(-1);
  assert.deepEqual(
    { ...second, requestId: undefined },
    { type: 'loadDeliverableIndex', itemId: 'wi-1', cursor: 'older-page-2', requestId: undefined },
  );
  assert.ok(second.requestId > first.requestId);
  harness.receive({
    type: 'deliverableIndexResult', itemId: second.itemId, cursor: second.cursor, requestId: second.requestId,
    items: [{
      stage: 'review', round: 0, runOrdinal: 1, itemRevision: 1,
      availability: 'available', reason: '', digest: 'c'.repeat(64),
      createdAt: '2026-07-12T08:00:00Z', preview: 'Revisión anterior.', entryCount: 2,
    }],
    page: { has_more: false, next_cursor: null },
  });
  assert.match(harness.elements.content.innerHTML, /Revisión anterior/);
  assert.match(harness.elements.content.innerHTML, /Ya estás viendo todas las entregas/);
  assert.equal(harness.hooks.indexView().items.length, 3);

  const stableRefresh = state('r2', 'Sigue trabajando.');
  stableRefresh.workbench.selected.presentation.deliverableIndex = {
    total: 3, returned: 1, truncated: true, nextCursor: 'older-page-1',
  };
  harness.receive({ type: 'state', state: stableRefresh });
  assert.equal(harness.hooks.indexView().items.length, 3);
  assert.equal(harness.hooks.indexView().nextCursor, '');

  const changing = state('r3', 'Se agregó una entrega.');
  changing.workbench.selected.presentation.deliverableIndex = {
    total: 4, returned: 1, truncated: true, nextCursor: 'new-old-page',
  };
  harness.receive({ type: 'state', state: changing });
  // A changed snapshot cursor invalidates pages collected from the older snapshot.
  assert.equal(harness.hooks.indexView().items.length, 0);
  assert.equal(harness.hooks.indexView().nextCursor, 'new-old-page');
  assert.match(harness.elements.content.innerHTML, /Ver entregas anteriores/);

  const other = state('r3', 'Otra tarea.', false, 'wi-2');
  other.workbench.selected.presentation.deliverableIndex = {
    total: 2, returned: 1, truncated: true, nextCursor: 'wi-2-page',
  };
  harness.receive({ type: 'state', state: other });
  harness.hooks.requestDeliverableIndex();
  const otherRequest = harness.messages.at(-1);
  harness.receive({ type: 'state', state: state('r4', 'Volvió.', false, 'wi-1') });
  harness.receive({
    type: 'deliverableIndexResult', itemId: otherRequest.itemId, cursor: otherRequest.cursor,
    requestId: otherRequest.requestId,
    items: [{ stage: 'planning', round: 99, runOrdinal: 9, availability: 'available', preview: 'Otra tarea.' }],
    page: { has_more: false, next_cursor: null },
  });
  assert.doesNotMatch(harness.elements.content.innerHTML, /Otra tarea\./);
});

test('responsive CSS covers narrow panels, reduced motion and forced colors', () => {
  assert.match(source, /@media \(max-width: 300px\)/);
  assert.match(source, /@media \(prefers-reduced-motion: reduce\)/);
  assert.match(source, /@media \(forced-colors: active\)/);
  assert.match(source, /\.task-body\s*\{[^}]*overflow-wrap:\s*anywhere/);
  assert.match(source, /\.actions\s*\{[^}]*justify-content:\s*center/);
});

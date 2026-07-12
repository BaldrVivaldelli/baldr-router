import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const source = fs.readFileSync(path.join(root, 'src', 'console.ts'), 'utf8');
const runtimeSource = fs.readFileSync(path.join(root, 'src', 'runtime.ts'), 'utf8');

function section(contents, start, end) {
  const startIndex = contents.indexOf(start);
  const endIndex = contents.indexOf(end, startIndex + start.length);
  assert.notEqual(startIndex, -1, `missing section start: ${start}`);
  assert.notEqual(endIndex, -1, `missing section end: ${end}`);
  return contents.slice(startIndex, endIndex);
}

test('console keeps the frozen setup/status/run facade contract', () => {
  assert.match(source, /runFacade\('run'/);
  assert.match(source, /consoleStatus/);
  assert.match(source, /setWorkspacePreferences/);
  assert.doesNotMatch(source, /sqlite3|better-sqlite3/);
});

test('console exposes tasks, composer, inline plus menu, chips, and slash commands', () => {
  for (const marker of ['Tus tareas', 'Escribí qué necesitás…', '¿Qué querés hacer?', 'Ver todos (', 'data-chip="git"', 'data-chip="preset"', 'data-chip="context"', 'id="plusMenu"', "type:'plusAction'", 'Archivos y carpetas', "case 'setup'", "case 'cancel'", "case 'resume'", "case 'archive'"]) {
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
  for (const wording of ['Todavía no hay tareas', 'Baldr lo organiza y te muestra el avance.', 'Protección de cambios', 'Nivel de detalle', 'Equipo de Baldr', 'Ayuda adicional', 'Continuar sin respaldo']) {
    assert.ok(source.includes(wording), `missing plain-language wording: ${wording}`);
  }
  for (const stale of ['No items yet', 'Git worktree', 'Context7 Auto', 'Baldr execution preset', 'durable draft']) {
    assert.ok(!source.includes(stale), `stale technical wording remains: ${stale}`);
  }
});

test('attachments render inside the composer and can be removed', () => {
  assert.match(source, /class="attachments" id="attachments"/);
  assert.match(source, /data-remove-pending/);
  assert.match(source, /case 'removePending'/);
  assert.match(source, /removePendingAttachment/);
});


test('console turns Git policy blocks into a guided non-Git or open-folder choice', () => {
  assert.match(source, /workspace_git_required/);
  assert.match(source, /Continuar sin respaldo/);
  assert.match(source, /Abrir otra carpeta/);
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

test('team chip resolves actual named or inline role models and exposes role details', () => {
  assert.match(source, /function shortModelLabel\(value\)/);
  assert.match(source, /\(sol\|terra\|luna\|spark\)/);
  assert.match(source, /function configuredRole\(role\)/);
  assert.match(source, /profiles\.execution_profiles\[selected\]/);
  assert.match(source, /profiles\.resolved_roles/);
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

import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const source = fs.readFileSync(path.join(root, 'src', 'runtime.ts'), 'utf8');

test('console polling requests the lightweight durable workbench view', () => {
  assert.match(source, /workbenchOnly\?: boolean/);
  assert.match(source, /if \(options\.workbenchOnly\) args\.push\('--workbench-only'\)/);
  assert.match(source, /workbenchOnly: true/);
  assert.match(source, /intent === 'status' && options\.workbenchOnly === true/);
  assert.match(source, /if \(!quiet\) this\.output\.appendLine/);
});

test('phase deliverable inspection sends every selector through the frozen facade CLI', () => {
  assert.match(source, /workItemAction:\s*'inspect-item-phase'/);
  assert.match(source, /phaseStage:\s*stage/);
  assert.match(source, /phaseRound:\s*round/);
  assert.match(source, /phaseRunOrdinal:\s*options\.runOrdinal/);
  assert.match(source, /phaseCursor:\s*options\.cursor/);
  assert.match(source, /phasePageSize:\s*options\.pageSize \?\? 30/);
  assert.match(source, /args\.push\('--phase-stage', options\.phaseStage\)/);
  assert.match(source, /args\.push\('--phase-round', String\(options\.phaseRound\)\)/);
  assert.match(source, /args\.push\('--phase-run-ordinal', String\(options\.phaseRunOrdinal\)\)/);
  assert.match(source, /args\.push\('--phase-cursor', options\.phaseCursor\)/);
  assert.match(source, /args\.push\('--phase-page-size', String\(options\.phasePageSize\)\)/);
});

test('historical deliverable index uses its own bounded cursor flags', () => {
  assert.match(source, /workItemAction:\s*'list-item-deliverables'/);
  assert.match(source, /deliverableCursor:\s*options\.cursor/);
  assert.match(source, /deliverablePageSize:\s*options\.pageSize \?\? 50/);
  assert.match(source, /args\.push\('--deliverable-cursor', options\.deliverableCursor\)/);
  assert.match(source, /args\.push\('--deliverable-page-size', String\(options\.deliverablePageSize\)\)/);
});

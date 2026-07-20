import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const runtime = fs.readFileSync(path.join(root, 'src', 'runtime.ts'), 'utf8');
const extension = fs.readFileSync(path.join(root, 'src', 'extension.ts'), 'utf8');

test('startup settles deterministic recovery before refreshing the workbench', () => {
  assert.match(runtime, /async settleWorkItems\([\s\S]*?workItemAction:\s*'settle-workspace'/);
  const startup = extension.slice(extension.indexOf('async function warmRuntime'));
  assert.ok(startup.indexOf('await runtime.settleWorkItems') >= 0);
  assert.ok(startup.indexOf('await runtime.settleWorkItems') < startup.indexOf('await consoleProvider.refresh(false)'));
  assert.match(startup, /requires_input_count/);
  assert.match(startup, /process_validation/);
  assert.match(startup, /orphan_processes/);
  assert.match(startup, /El trabajo sigue guardado/);
  assert.match(startup, /Abrir Baldr/);
});

test('startup recovery is restricted to trusted open workspace folders', () => {
  const startup = extension.slice(extension.indexOf('async function warmRuntime'));
  assert.match(startup, /if \(vscode\.workspace\.isTrusted\)/);
  assert.match(startup, /for \(const folder of vscode\.workspace\.workspaceFolders \?\? \[\]\)/);
  assert.match(startup, /folder\.uri\.fsPath/);
});

test('extension shutdown owns every captured process tree', () => {
  assert.match(runtime, /implements vscode\.Disposable/);
  assert.match(runtime, /private readonly activeChildren = new Set<ChildProcess>/);
  assert.match(runtime, /BALDR_INSTALL_SIGNAL_HANDLERS: '1'/);
  assert.match(runtime, /this\.activeChildren\.add\(child\)/);
  assert.match(runtime, /children\.map\(\(child\) => terminateChildTree\(child\)\)/);
  assert.match(extension, /context\.subscriptions\.push\(runtime,/);
  assert.match(extension, /export async function deactivate\(\): Promise<void>/);
  assert.match(extension, /await runtime\?\.shutdown\(\)/);
});

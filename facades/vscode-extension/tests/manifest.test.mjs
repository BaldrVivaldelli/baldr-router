import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const manifest = JSON.parse(fs.readFileSync(path.join(root, 'package.json'), 'utf8'));
const contract = JSON.parse(fs.readFileSync(path.join(root, 'resources', 'facade-v1.json'), 'utf8'));
const runtimeSource = fs.readFileSync(path.join(root, 'src', 'runtime.ts'), 'utf8');
const extensionSource = fs.readFileSync(path.join(root, 'src', 'extension.ts'), 'utf8');
const workspaceContextSource = fs.readFileSync(path.join(root, 'src', 'workspaceContext.ts'), 'utf8');

test('exposes one command palette command', () => {
  assert.deepEqual(manifest.contributes.commands.map((item) => item.command), ['baldr.open']);
  assert.equal(contract.commandPalette.maximumVisibleCommands, 1);
  assert.equal(manifest.contributes.commands[0].title, contract.commandPalette.primary);
});

test('chat participant exposes exactly the frozen intents', () => {
  const participant = manifest.contributes.chatParticipants[0];
  assert.equal(participant.name, 'baldr');
  assert.deepEqual(participant.commands.map((item) => item.name), Object.keys(contract.intents));
});

test('registers a single MCP definition provider', () => {
  assert.deepEqual(manifest.contributes.mcpServerDefinitionProviders, [
    { id: 'baldr.router', label: 'Baldr Router' },
  ]);
});

test('requires VS Code Workspace Trust before provider execution', () => {
  assert.equal(manifest.capabilities.untrustedWorkspaces.supported, false);
});

test('packages the v0.19.0 Baldr Console facade', () => {
  assert.equal(manifest.version, '0.19.0');
  const extensionVersion = runtimeSource.match(/export const EXTENSION_VERSION = '([^']+)'/)?.[1];
  assert.equal(extensionVersion, manifest.version);
  assert.match(contract.intents.setup.description, /lifecycle verification/);
});

test('contributes one Baldr Activity Bar webview', () => {
  assert.deepEqual(manifest.contributes.viewsContainers.activitybar, [
    { id: 'baldr', title: 'Baldr', icon: 'media/baldr.svg' },
  ]);
  assert.deepEqual(manifest.contributes.views.baldr, [
    { type: 'webview', id: 'baldr.console', name: 'Baldr', contextualTitle: 'Baldr Console' },
  ]);
  assert.ok(manifest.activationEvents.includes('onView:baldr.console'));
});

test('ships the form-free Baldr Console and Activity Bar icon', () => {
  const source = fs.readFileSync(path.join(root, 'src', 'console.ts'), 'utf8');
  assert.match(source, /\/profile/);
  assert.match(source, /\/git/);
  assert.match(source, /\/context/);
  assert.match(source, /type\s*:\s*'plusAction'/);
  assert.match(source, /id="plusMenu"/);
  assert.match(source, /class=\"composer\"/);
  assert.match(source, /class=\"task-list\"/);
  assert.ok(fs.existsSync(path.join(root, 'media', 'baldr.svg')));
});

test('chat completion logs only bounded work-item identity and status', () => {
  assert.doesNotMatch(extensionSource, /JSON\.stringify\(record\(result\.work_item\)\)/);
  assert.match(extensionSource, /error_code: completed\.error_code/);
  assert.doesNotMatch(extensionSource, /completed\.task|completed\.extra_context|completed\.workflow/);
});

test('chat resumes its durable item and resolves bounded VS Code context', () => {
  assert.match(extensionSource, /latestBaldrConversation\(chatContext\)/);
  assert.match(extensionSource, /runtime\.continueWorkItem/);
  assert.match(extensionSource, /stream\.markdown\(renderRun\(result\)\)/);
  assert.match(extensionSource, /workspaceRoot,/);
  assert.match(workspaceContextSource, /request\.references|ChatPromptReference/);
  assert.match(workspaceContextSource, /activeTextEditor/);
  assert.match(workspaceContextSource, /document\.isDirty/);
  assert.match(workspaceContextSource, /getDiagnostics/);
  assert.match(workspaceContextSource, /promptIfAmbiguous/);
  assert.doesNotMatch(extensionSource, /workspaceFolders\?\.\[0\]/);
  assert.doesNotMatch(runtimeSource, /workspaceFolders\?\.\[0\]/);
});

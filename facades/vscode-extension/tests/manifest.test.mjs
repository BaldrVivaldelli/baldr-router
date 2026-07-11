import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const manifest = JSON.parse(fs.readFileSync(path.join(root, 'package.json'), 'utf8'));
const contract = JSON.parse(fs.readFileSync(path.join(root, 'resources', 'facade-v1.json'), 'utf8'));

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

test('packages the v0.16 durable facade', () => {
  assert.equal(manifest.version, '0.16.1');
  assert.match(contract.intents.setup.description, /lifecycle verification/);
});

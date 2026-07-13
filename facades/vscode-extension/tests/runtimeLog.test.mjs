import test from 'node:test';
import assert from 'node:assert/strict';

import { describeRuntimeInvocation } from '../dist/runtimeLog.js';

test('runtime logs keep intent and flag names but omit private values', () => {
  const line = describeRuntimeInvocation([
    'exec', '--', 'facade', 'run', '/home/alice/private',
    'Revisá mi proyecto secreto', '--work-item-id', 'wi-secret',
    '--extra-context', 'password is synthetic-secret',
    '--attachments-json', '[{"path":"/home/alice/private.txt"}]',
    '--client', 'vscode-extension',
  ]);

  assert.equal(
    line,
    '[runtime] exec -- facade run --work-item-id --extra-context --attachments-json --client',
  );
  for (const privateValue of ['/home/alice', 'Revisá', 'wi-secret', 'password', 'private.txt', 'vscode-extension']) {
    assert.equal(line.includes(privateValue), false, privateValue);
  }
});

test('bootstrap lifecycle logs expose only the fixed command', () => {
  assert.equal(describeRuntimeInvocation(['ensure']), '[runtime] ensure');
  assert.equal(describeRuntimeInvocation(['detect']), '[runtime] detect');
});

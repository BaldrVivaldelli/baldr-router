import test from 'node:test';
import assert from 'node:assert/strict';

import { redactSensitive } from '../dist/redaction.js';

test('redacts Context7 secrets before output logging', () => {
  const secret = 'ctx7sk-synthetic-vscode-secret-1234567890';
  const result = redactSensitive(
    `Authorization: Bearer ${secret}\napi_key=${secret}\nraw=${secret}`,
    [secret],
  );
  assert.equal(result.includes(secret), false);
  assert.match(result, /<redacted>/);
});

test('does not alter ordinary diagnostic text', () => {
  assert.equal(redactSensitive('Baldr runtime ready'), 'Baldr runtime ready');
});

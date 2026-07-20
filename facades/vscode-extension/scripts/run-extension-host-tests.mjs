import fs from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

import { runTests } from '@vscode/test-electron';

const extensionDevelopmentPath = path.resolve(
  path.dirname(fileURLToPath(import.meta.url)),
  '..',
);
const extensionTestsPath = path.join(
  extensionDevelopmentPath,
  'dist',
  'test',
  'suite',
  'index.js',
);
const workspacePath = await fs.mkdtemp(path.join(os.tmpdir(), 'baldr-vscode-e2e-'));

const electronRunAsNode = process.env.ELECTRON_RUN_AS_NODE;
delete process.env.ELECTRON_RUN_AS_NODE;

try {
  await fs.writeFile(
    path.join(workspacePath, 'README.md'),
    'Baldr VS Code Extension Host qualification fixture\n',
    'utf8',
  );
  await runTests({
    version: process.env.BALDR_VSCODE_TEST_VERSION || '1.126.0',
    extensionDevelopmentPath,
    extensionTestsPath,
    launchArgs: [
      workspacePath,
      '--disable-extensions',
      '--disable-workspace-trust',
      '--skip-welcome',
      '--skip-release-notes',
    ],
  });
} finally {
  if (electronRunAsNode === undefined) {
    delete process.env.ELECTRON_RUN_AS_NODE;
  } else {
    process.env.ELECTRON_RUN_AS_NODE = electronRunAsNode;
  }
  await fs.rm(workspacePath, { recursive: true, force: true });
}

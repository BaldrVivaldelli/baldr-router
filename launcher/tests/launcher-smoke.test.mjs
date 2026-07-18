import { test } from 'node:test';
import assert from 'node:assert/strict';
import { spawn, spawnSync } from 'node:child_process';
import fs from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join, resolve } from 'node:path';

const __dirname = dirname(fileURLToPath(import.meta.url));
const launcherRoot = resolve(__dirname, '..');
const repoRoot = resolve(launcherRoot, '..');
const bin = join(launcherRoot, 'bin', 'baldr-router-launcher.mjs');

test('prints current launcher version', () => {
  const result = spawnSync(process.execPath, [bin, '--version'], { encoding: 'utf8' });
  assert.equal(result.status, 0);
  assert.match(result.stdout, /^0\.20\.0/);
});

test('detect emits the shared runtime descriptor shape', () => {
  const result = spawnSync(process.execPath, [bin, 'detect'], {
    encoding: 'utf8',
    env: { ...process.env, BALDR_ROUTER_LAUNCHER_MODE: 'host' },
  });
  assert.equal(result.status, 0);
  const parsed = JSON.parse(result.stdout);
  assert.equal(parsed.launcher.version, '0.20.0');
  assert.equal(parsed.target.mode, 'host');
  assert.ok(['host', 'missing'].includes(parsed.target.kind));
});

test('VS Code extension bundles the exact shared runtime bootstrap', () => {
  const launcher = fs.readFileSync(join(launcherRoot, 'lib', 'runtime-bootstrap.mjs'), 'utf8');
  const extension = fs.readFileSync(
    join(repoRoot, 'facades', 'vscode-extension', 'runtime', 'runtime-bootstrap.mjs'),
    'utf8',
  );
  assert.equal(extension, launcher);
});

import os from 'node:os';
import {
  managedRuntimeCurrent,
  pruneOldHostRuntimes,
  resolveRuntime,
  sha256File,
  terminateChildTree,
} from '../lib/runtime-bootstrap.mjs';

test('managed runtime manifest detects current wheel and upgrade mismatch', () => {
  const root = fs.mkdtempSync(join(os.tmpdir(), 'baldr-runtime-manifest-'));
  const executable = join(root, 'baldr-router');
  const wheel = join(root, 'baldr_router-0.20.0.whl');
  fs.writeFileSync(executable, 'binary');
  fs.writeFileSync(wheel, 'wheel-v1');
  fs.writeFileSync(join(root, 'runtime.json'), JSON.stringify({
    version: '0.20.0',
    wheelSha256: sha256File(wheel),
  }));

  assert.equal(managedRuntimeCurrent(root, executable, wheel), true);
  fs.writeFileSync(wheel, 'wheel-v2');
  assert.equal(managedRuntimeCurrent(root, executable, wheel), false);
  fs.rmSync(root, { recursive: true, force: true });
});

test('runtime upgrade pruning keeps current and newest prior version', () => {
  const runtimeDir = fs.mkdtempSync(join(os.tmpdir(), 'baldr-runtime-prune-'));
  const versions = ['0.13.0', '0.14.0', '0.15.0', '0.17.0', '0.18.0', '0.19.0', '0.20.0'];
  for (const [index, version] of versions.entries()) {
    const dir = join(runtimeDir, version);
    fs.mkdirSync(dir, { recursive: true });
    const date = new Date(Date.now() - (versions.length - index) * 1000);
    fs.utimesSync(dir, date, date);
  }

  const removed = pruneOldHostRuntimes(runtimeDir, {
    keepVersions: 2,
    currentVersion: '0.20.0',
  });

  assert.deepEqual(removed.sort(), ['0.13.0', '0.14.0', '0.15.0', '0.17.0', '0.18.0']);
  assert.equal(fs.existsSync(join(runtimeDir, '0.19.0')), true);
  assert.equal(fs.existsSync(join(runtimeDir, '0.20.0')), true);
  fs.rmSync(runtimeDir, { recursive: true, force: true });
});

test('Windows host automatically falls back to an existing WSL runtime', () => {
  const capture = (command, args) => {
    if (command === 'where.exe') return { status: 1, stdout: '', stderr: '' };
    if (command === 'wsl.exe' && args.includes('--status')) {
      return { status: 0, stdout: 'Default Distribution: Ubuntu', stderr: '' };
    }
    if (command === 'wsl.exe' && args.includes('--list')) {
      return { status: 0, stdout: 'Ubuntu\n', stderr: '' };
    }
    if (command === 'wsl.exe' && args.includes('bash')) {
      return { status: 0, stdout: '/home/test/.local/bin/baldr-router\n', stderr: '' };
    }
    return { status: 1, stdout: '', stderr: '' };
  };

  const target = resolveRuntime({
    platform: 'win32',
    mode: 'auto',
    autoInstall: false,
    capture,
    runtimeDir: join(os.tmpdir(), 'baldr-no-host-runtime'),
  });

  assert.equal(target.ok, true);
  assert.equal(target.kind, 'wsl');
  assert.equal(target.executable, '/home/test/.local/bin/baldr-router');
});

test('Remote WSL or Linux resolves the host-side router directly', () => {
  const capture = (command) => {
    if (command === 'bash') {
      return { status: 0, stdout: '/home/test/.local/bin/baldr-router\n', stderr: '' };
    }
    return { status: 1, stdout: '', stderr: '' };
  };

  const target = resolveRuntime({
    platform: 'linux',
    mode: 'auto',
    autoInstall: false,
    capture,
    runtimeDir: join(os.tmpdir(), 'baldr-remote-wsl-runtime'),
  });

  assert.equal(target.ok, true);
  assert.equal(target.kind, 'host');
  assert.equal(target.executable, '/home/test/.local/bin/baldr-router');
});

test('shared cancellation terminates a detached child process group', { skip: process.platform === 'win32' }, async () => {
  const root = fs.mkdtempSync(join(os.tmpdir(), 'baldr-cancel-tree-'));
  const childPidFile = join(root, 'child.pid');
  const code = `
    const { spawn } = require('node:child_process');
    const fs = require('node:fs');
    const child = spawn(process.execPath, ['-e', 'setTimeout(() => {}, 60000)'], { stdio: 'ignore' });
    fs.writeFileSync(process.argv[1], String(child.pid));
    setTimeout(() => {}, 60000);
  `;
  const parent = spawn(process.execPath, ['-e', code, childPidFile], {
    detached: true,
    stdio: 'ignore',
  });
  const deadline = Date.now() + 5000;
  while (!fs.existsSync(childPidFile) && Date.now() < deadline) {
    await new Promise((resolvePromise) => setTimeout(resolvePromise, 25));
  }
  assert.equal(fs.existsSync(childPidFile), true);
  const childPid = Number(fs.readFileSync(childPidFile, 'utf8'));

  await terminateChildTree(parent, { graceMs: 150 });
  await new Promise((resolvePromise) => setTimeout(resolvePromise, 150));

  const alive = (pid) => {
    try { process.kill(pid, 0); return true; } catch { return false; }
  };
  assert.equal(alive(parent.pid), false);
  if (alive(childPid)) {
    const stat = fs.readFileSync(`/proc/${childPid}/stat`, 'utf8').split(' ')[2];
    assert.equal(stat, 'Z');
  }
  fs.rmSync(root, { recursive: true, force: true });
});

test('managed WSL runtime is created at its final path with rollback semantics', () => {
  const root = fs.mkdtempSync(join(os.tmpdir(), 'baldr-wsl-install-'));
  const wheel = join(root, 'baldr_router-0.20.0-py3-none-any.whl');
  fs.writeFileSync(wheel, 'synthetic-wheel');
  let installScript = '';

  const capture = (command, args) => {
    if (command === 'where.exe') return { status: 1, stdout: '', stderr: '' };
    if (command === 'wsl.exe' && args.includes('--status')) {
      return { status: 0, stdout: 'Default Distribution: Ubuntu', stderr: '' };
    }
    if (command === 'wsl.exe' && args.includes('--list')) {
      return { status: 0, stdout: 'Ubuntu\n', stderr: '' };
    }
    if (command === 'wsl.exe') {
      const script = String(args.at(-1) ?? '');
      if (script.startsWith('wslpath -u ')) {
        return { status: 0, stdout: '/mnt/c/baldr_router-0.20.0-py3-none-any.whl\n', stderr: '' };
      }
      if (script.includes('set -e;')) {
        installScript = script;
        return {
          status: 0,
          stdout: '/home/test/.local/share/baldr-router-vscode/0.20.0/venv/bin/baldr-router\n',
          stderr: '',
        };
      }
      return { status: 127, stdout: '', stderr: 'not found' };
    }
    return { status: 1, stdout: '', stderr: '' };
  };

  const target = resolveRuntime({
    platform: 'win32',
    mode: 'auto',
    autoInstall: true,
    preferManaged: true,
    capture,
    wheelPath: wheel,
    wslDistro: 'Ubuntu',
    runtimeDir: join(root, 'host-runtime'),
  });

  assert.equal(target.ok, true);
  assert.equal(target.kind, 'wsl');
  assert.match(installScript, /root="\$HOME\/\.local\/share\/baldr-router-vscode\/0\.20\.0"/);
  assert.match(installScript, /python3 -m venv "\$root\/venv"/);
  assert.match(installScript, /rollback=/);
  assert.match(installScript, /receiptSchemaVersion/);
  assert.match(installScript, /private-wsl-runtime/);
  assert.doesNotMatch(installScript, /root='\$HOME/);
  fs.rmSync(root, { recursive: true, force: true });
});

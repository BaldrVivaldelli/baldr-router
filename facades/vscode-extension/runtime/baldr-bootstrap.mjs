#!/usr/bin/env node
import process from 'node:process';
import {
  VERSION,
  detectReport,
  proxyRuntime,
  resolveRuntime,
  sanitizedTarget,
} from './runtime-bootstrap.mjs';

function fail(message) {
  process.stderr.write(`[baldr-bootstrap] ${message}\n`);
  process.exit(127);
}

const [command, ...rest] = process.argv.slice(2);
if (!command || command === '--help' || command === '-h') {
  process.stdout.write(`Baldr Router VS Code bootstrap ${VERSION}\n`);
  process.stdout.write('Usage: baldr-bootstrap.mjs <mcp|ensure|detect|exec> [--] [router args...]\n');
} else if (command === '--version' || command === 'version') {
  process.stdout.write(`${VERSION}\n`);
} else if (command === 'detect') {
  process.stdout.write(`${JSON.stringify(detectReport(), null, 2)}\n`);
} else if (command === 'ensure') {
  const target = resolveRuntime({ autoInstall: true, preferManaged: true });
  process.stdout.write(`${JSON.stringify(sanitizedTarget(target), null, 2)}\n`);
  if (!target.ok) process.exitCode = 127;
} else if (command === 'mcp') {
  const target = resolveRuntime({ autoInstall: true, preferManaged: true });
  if (!target.ok) fail(target.reason || 'No Baldr Router runtime is available.');
  proxyRuntime(target, ['mcp']);
} else if (command === 'exec') {
  const routerArgs = rest[0] === '--' ? rest.slice(1) : rest;
  if (!routerArgs.length) fail('The exec command requires baldr-router arguments.');
  const target = resolveRuntime({ autoInstall: true, preferManaged: true });
  if (!target.ok) fail(target.reason || 'No Baldr Router runtime is available.');
  proxyRuntime(target, routerArgs);
} else {
  fail(`Unknown bootstrap command: ${command}`);
}

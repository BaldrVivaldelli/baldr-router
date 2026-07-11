#!/usr/bin/env node
import process from 'node:process';
import {
  VERSION,
  detectReport,
  proxyRuntime,
  resolveRuntime,
  sanitizedTarget,
} from '../lib/runtime-bootstrap.mjs';

function usage() {
  process.stdout.write(`baldr-router-launcher ${VERSION}\n\n`);
  process.stdout.write('Usage:\n');
  process.stdout.write('  baldr-router-launcher mcp\n');
  process.stdout.write('  baldr-router-launcher detect\n');
  process.stdout.write('  baldr-router-launcher ensure\n');
  process.stdout.write('  baldr-router-launcher <baldr-router arguments...>\n');
}

const [command, ...rest] = process.argv.slice(2);
if (!command || command === '--help' || command === '-h') {
  usage();
} else if (command === '--version' || command === 'version') {
  process.stdout.write(`${VERSION}\n`);
} else if (command === 'detect') {
  process.stdout.write(`${JSON.stringify(detectReport(), null, 2)}\n`);
} else if (command === 'ensure') {
  const target = resolveRuntime({ autoInstall: true });
  process.stdout.write(`${JSON.stringify(sanitizedTarget(target), null, 2)}\n`);
  if (!target.ok) process.exitCode = 127;
} else {
  const target = resolveRuntime({ autoInstall: true });
  if (!target.ok) {
    process.stderr.write(`[baldr-router-launcher] ${target.reason}\n`);
    process.exit(127);
  }
  proxyRuntime(target, [command, ...rest]);
}

import * as assert from 'node:assert/strict';
import * as vscode from 'vscode';

interface CancellationCanaryResult {
  ok?: boolean;
  status?: string;
  source?: string;
  durable_status?: string;
  orphan_processes?: number;
  process_tree_observed?: number;
  worker_stopped?: boolean;
  run_id?: string;
  evidence_id?: string;
}

export async function run(): Promise<void> {
  const extension = vscode.extensions.getExtension('baldr.baldr-router-vscode');
  assert.ok(extension, 'Baldr extension must be present in the Extension Host');
  await extension.activate();

  const result = await vscode.commands.executeCommand<CancellationCanaryResult>(
    'baldr.qualification.cancelCanary',
  );

  assert.equal(result?.ok, true);
  assert.equal(result?.status, 'passed');
  assert.equal(result?.source, 'vscode-extension-host');
  assert.equal(result?.durable_status, 'cancelled');
  assert.equal(result?.orphan_processes, 0);
  assert.ok((result?.process_tree_observed ?? 0) >= 2);
  assert.equal(result?.worker_stopped, true);
  assert.match(result?.run_id ?? '', /^workflow-/);
  assert.match(result?.evidence_id ?? '', /^br-workflow-/);
}

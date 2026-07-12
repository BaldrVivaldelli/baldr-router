import test from 'node:test';
import assert from 'node:assert/strict';

import { renderQualification, renderSetup, renderStatus } from '../dist/render.js';

test('setup renders lifecycle verification and workspace profile', () => {
  const markdown = renderSetup({
    ok: true,
    health: {
      router: { default_workflow: 'architect-implement-review', default_provider: 'codex' },
      codex: { found: true },
      context7: { enabled: false },
    },
    verification: { ok: true, status: 'cached' },
    workspace_profile: { ok: true, ecosystem: { package_managers: ['pnpm'] } },
    actions: [],
  });
  assert.match(markdown, /Lifecycle verification/);
  assert.match(markdown, /Workspace profile/);
  assert.match(markdown, /pnpm/);
});

test('status renders evidence id', () => {
  const markdown = renderStatus({
    ok: true,
    summary: {
      default_workflow: 'architect-implement-review',
      default_provider: 'codex',
      codex_found: true,
      codex_runner: 'exec-json',
      context7_enabled: false,
      warnings: [],
    },
    verification: { ok: true, evidence: { evidence_id: 'br-evidence-test' } },
    workspace_profile: {
      ok: true,
      ecosystem: { package_managers: ['uv'] },
      inventory: { languages: { Python: 12 } },
    },
    recent_runs: { runs: [] },
  });
  assert.match(markdown, /br-evidence-test/);
  assert.match(markdown, /uv/);
});


test('qualification renders provisional evidence gates and editable files', () => {
  const markdown = renderQualification({
    status: 'provisional',
    profile: 'vscode-linux-native',
    qualification_id: 'br-qualification-test',
    receipt_sha256: 'abc123',
    checks: {
      environment: { ok: true },
      lab: { consecutive_passes: 3, required_consecutive_passes: 3 },
      provider_smoke: { skipped: true },
      assertions: { passed_with_evidence: ['a'], required: ['a', 'b'] },
      canaries: { passed_with_evidence_count: 4, required_tasks: 10, repository_count: 1, required_repositories: 2 },
    },
    next_steps: ['Complete evidence'],
    template_dir: '/tmp/baldr-qualification',
    client_assertions_path: '/tmp/baldr-qualification/client-assertions.json',
    canary_results_path: '/tmp/baldr-qualification/canary-results.json',
    bundle: { path: '/tmp/evidence' },
  });
  assert.match(markdown, /PROVISIONAL/);
  assert.match(markdown, /4 \/ 10/);
  assert.match(markdown, /Complete evidence/);
  assert.match(markdown, /client-assertions\.json/);
});

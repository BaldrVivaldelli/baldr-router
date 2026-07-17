type JsonRecord = Record<string, unknown>;

function record(value: unknown): JsonRecord {
  return value && typeof value === 'object' && !Array.isArray(value) ? value as JsonRecord : {};
}

function text(value: unknown, fallback = 'unknown'): string {
  if (value === undefined || value === null || value === '') return fallback;
  if (typeof value === 'string') return value;
  if (typeof value === 'boolean' || typeof value === 'number') return String(value);
  return JSON.stringify(value);
}

function boolean(value: unknown): boolean { return value === true; }
function list(value: unknown): string[] {
  return Array.isArray(value) ? value.map((item) => text(item, '')).filter(Boolean) : [];
}
function truncate(value: string, limit = 1600): string {
  return value.length <= limit ? value : `${value.slice(0, limit)}…`;
}

export function renderSetup(result: JsonRecord): string {
  const health = record(result.health);
  const codex = record(health.codex);
  const router = record(health.router);
  const context7 = record(health.context7);
  const verification = record(result.verification ?? health.verification);
  const profile = record(result.workspace_profile ?? health.workspace_profile);
  const ecosystem = record(profile.ecosystem);
  const actions = Array.isArray(result.actions) ? result.actions : [];
  const lines = [
    '# Baldr Setup',
    '',
    boolean(result.ok) ? '✅ **Ready**' : '⚠️ **Attention required**',
    '',
    `- **Workflow:** ${text(router.default_workflow)}`,
    `- **Provider:** ${text(router.default_provider)}`,
    `- **Codex CLI:** ${codex.found ? 'detected' : 'not detected'}`,
    `- **Context7:** ${context7.enabled ? text(context7.mode, 'enabled') : 'optional / disabled'}`,
    `- **Lifecycle verification:** ${verification.ok === true ? text(verification.status, 'passed') : verification.available === false ? 'pending' : 'attention required'}`,
    `- **Workspace profile:** ${profile.ok === true ? `${list(ecosystem.package_managers).join(', ') || 'metadata ready'}` : 'not available'}`,
  ];
  const clientSetup = record(result.vscode_setup);
  if (Object.keys(clientSetup).length) {
    lines.push(`- **Role setup:** ${text(clientSetup.roles, 'unchanged')}`);
    lines.push(`- **Context7 setup:** ${text(clientSetup.context7, 'unchanged')}`);
  }
  if (actions.length) {
    lines.push('', '## Required actions');
    for (const raw of actions) {
      const action = record(raw);
      lines.push(`- ${text(action.message, text(action.id, 'Action required'))}`);
    }
  }
  lines.push('', 'Context7 is optional. Secrets entered through the extension are stored in VS Code SecretStorage and are never requested in chat.');
  return lines.join('\n');
}

export function renderStatus(result: JsonRecord): string {
  const summary = record(result.summary);
  const warnings = list(summary.warnings);
  const verification = record(result.verification);
  const evidence = record(verification.evidence);
  const profile = record(result.workspace_profile);
  const ecosystem = record(profile.ecosystem);
  const inventory = record(profile.inventory);
  const recent = result.recent_runs;
  const recentObject = record(recent);
  const recentCount = Array.isArray(recent)
    ? recent.length
    : Array.isArray(recentObject.runs)
      ? recentObject.runs.length
      : Array.isArray(recentObject.items)
        ? recentObject.items.length
        : 0;
  const lines = [
    '# Baldr Status',
    '',
    boolean(result.ok) ? '✅ **Ready**' : '⚠️ **Attention required**',
    '',
    `- **Workflow:** ${text(summary.default_workflow)}`,
    `- **Provider:** ${text(summary.default_provider)}`,
    `- **Codex:** ${summary.codex_found ? `detected (${text(summary.codex_runner)})` : 'not detected'}`,
    `- **Context7:** ${summary.context7_enabled ? text(summary.context7_mode, 'enabled') : 'disabled'}`,
    `- **Recent runs:** ${recentCount}`,
    `- **Verification:** ${verification.ok === true || evidence.ok === true ? 'passed' : verification.available === false ? 'pending' : 'attention required'}`,
    `- **Evidence:** ${text(evidence.evidence_id, text(verification.evidence_id, 'not generated yet'))}`,
    `- **Workspace profile:** ${profile.ok === true ? `${list(ecosystem.package_managers).join(', ') || 'metadata ready'}; ${Object.keys(record(inventory.languages)).length} language(s)` : 'not available'}`,
  ];
  if (warnings.length) {
    lines.push('', '## Warnings');
    for (const warning of warnings) lines.push(`- ${warning}`);
  }
  return lines.join('\n');
}

export function renderQualification(result: JsonRecord): string {
  const checks = record(result.checks);
  const environment = record(checks.environment);
  const lab = record(checks.lab);
  const providerSmoke = record(checks.provider_smoke);
  const assertions = record(checks.assertions);
  const canaries = record(checks.canaries);
  const bundle = record(result.bundle);
  const nextSteps = list(result.next_steps);
  const status = text(result.status, 'provisional').toUpperCase();
  const lines = [
    '# Baldr Real-Environment Qualification',
    '',
    result.status === 'qualified' ? '✅ **QUALIFIED**' : result.status === 'failed' ? '❌ **FAILED**' : '⚠️ **PROVISIONAL**',
    '',
    `- **Profile:** ${text(result.profile)}`,
    `- **Status:** ${status}`,
    `- **Consecutive lifecycle passes:** ${text(lab.consecutive_passes, '0')} / ${text(lab.required_consecutive_passes, '3')}`,
    `- **Environment match:** ${environment.ok === true ? 'passed' : 'pending / failed'}`,
    `- **Provider smoke:** ${providerSmoke.passed === true ? 'passed' : providerSmoke.skipped === true ? 'skipped' : 'pending / failed'}`,
    `- **Client assertions with evidence:** ${list(assertions.passed_with_evidence).length} / ${list(assertions.required).length}`,
    `- **Canary tasks with evidence:** ${text(canaries.passed_with_evidence_count, '0')} / ${text(canaries.required_tasks, '10')}`,
    `- **Real repositories:** ${text(canaries.repository_count, '0')} / ${text(canaries.required_repositories, '2')}`,
    `- **Qualification ID:** \`${text(result.qualification_id, 'n/a')}\``,
    `- **Receipt SHA-256:** \`${text(result.receipt_sha256, 'n/a')}\``,
  ];
  if (nextSteps.length) {
    lines.push('', '## Next steps');
    for (const item of nextSteps) lines.push(`- ${item}`);
  }
  const assertionsPath = text(result.client_assertions_path);
  const canariesPath = text(result.canary_results_path);
  const templateDir = text(result.template_dir);
  if (templateDir || assertionsPath || canariesPath) {
    lines.push('', '## Evidence files');
    if (templateDir) lines.push(`- Template directory: \`${templateDir}\``);
    if (assertionsPath) lines.push(`- Assertions: \`${assertionsPath}\``);
    if (canariesPath) lines.push(`- Canaries: \`${canariesPath}\``);
  }
  if (bundle.path) {
    lines.push('', `Evidence bundle: \`${text(bundle.path)}\``);
  }
  lines.push(
    '',
    '_A build-time or lifecycle-only run remains provisional by design. Final qualification requires a real provider smoke and ten evidenced canary tasks across two real repositories._',
  );
  return lines.join('\n');
}

export function renderRun(result: JsonRecord): string {
  const report = record(result.final_report ?? result.report);
  const status = text(report.status, text(result.status, boolean(result.ok) ? 'completed' : 'failed'));
  const statusLabel = ({
    approved: 'Trabajo listo y aprobado',
    completed: 'Trabajo completado',
    needs_changes: 'Hay cambios pendientes',
    blocked: 'El trabajo está bloqueado',
    failed: 'La ejecución necesita atención',
    cancelled: 'La ejecución fue cancelada',
  } as Record<string, string>)[status] ?? status;
  const lines = [
    '# Resultado de Baldr',
    '',
    `**${boolean(result.ok) ? '✅' : '⚠️'} ${statusLabel}**`,
    '',
    truncate(text(report.summary, text(result.reason, 'Baldr terminó la ejecución.'))),
  ];
  for (const [label, key] of [
    ['Trabajo realizado', 'work_completed'],
    ['Qué agregó', 'changes_added'],
    ['Qué modificó', 'changes_modified'],
    ['Qué quitó', 'changes_removed'],
    ['Archivos agregados', 'files_added'],
    ['Archivos modificados', 'files_modified'],
    ['Archivos eliminados', 'files_deleted'],
    ['Verificación', 'verification_evidence'],
    ['Pruebas ejecutadas', 'tests_run'],
    ['Pendientes', 'blockers'],
    ['Qué falta verificar', 'verification_needed'],
    ['Riesgos', 'risks'],
    ['Próximos pasos', 'follow_up'],
  ] as const) {
    const values = list(report[key]);
    if (values.length) {
      lines.push('', `## ${label}`);
      for (const value of values.slice(0, 20)) lines.push(`- ${truncate(value, 700)}`);
    }
  }
  lines.push(
    '',
    `Run: \`${text(result.run_id, 'n/a')}\` · Workflow: ${text(result.workflow, 'architect-implement-review')}`,
  );
  if (result.dry_run === true) lines.push('', '_Dry run: no provider was executed and no files were modified._');
  return lines.join('\n');
}

export function renderError(error: unknown): string {
  const message = error instanceof Error ? error.message : String(error);
  return `# Baldr Error\n\n\`\`\`text\n${truncate(message, 4000)}\n\`\`\`\n\nOpen **Baldr: Open** for recovery and logs.`;
}

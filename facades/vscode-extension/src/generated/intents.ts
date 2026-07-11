// Generated from contracts/facade-v1.json. Do not edit by hand.
export type BaldrIntentId = 'setup' | 'status' | 'run';

export interface BaldrIntentDefinition {
  readonly id: BaldrIntentId;
  readonly title: string;
  readonly description: string;
  readonly requiresWorkspace: boolean;
  readonly requiresTask: boolean;
  readonly mcpPrompt: BaldrIntentId;
  readonly cli: readonly string[];
  readonly instruction: string;
}

export const BALDR_INTENTS: readonly BaldrIntentDefinition[] = [
  {
    id: 'setup',
    title: 'Setup',
    description: 'Prepare the runtime, durable state store, lifecycle verification, trusted-workspace profile, execution profiles, providers, and optional Context7 without exposing secrets.',
    requiresWorkspace: false,
    requiresTask: false,
    mcpPrompt: 'setup',
    cli: ["facade", "setup"],
    instruction: 'Inspect Baldr Router with router_doctor, router_provider_status, router_workflow_status, router_extension_status, and context7_onboarding. Present a short guided setup, including SQLite durability/schema status, resolved execution profiles, the latest lifecycle evidence, and trusted-workspace profile when available. Never ask for API keys in chat. Keep current profiles unless the user explicitly changes them. Context7 is optional.',
  },
  {
    id: 'status',
    title: 'Status',
    description: 'Return a compact health report for runtime, durable runs/recovery, lifecycle evidence, workspace profile, providers, execution profiles, workflow, Context7, extensions, and recent runs.',
    requiresWorkspace: false,
    requiresTask: false,
    mcpPrompt: 'status',
    cli: ["facade", "status"],
    instruction: 'Inspect Baldr Router with router_doctor, router_provider_status, router_workflow_status, router_extension_status, and router_recent_runs. Include SQLite schema/nonterminal/recovery status, resolved execution profiles, the latest redacted lifecycle evidence, and workspace-profile status. Return a concise report with actionable warnings only. Do not modify files.',
  },
  {
    id: 'run',
    title: 'Run',
    description: 'Create, resume, or idempotently reuse the configured durable orchestration workflow for a task in the active workspace.',
    requiresWorkspace: true,
    requiresTask: true,
    mcpPrompt: 'run',
    cli: ["facade", "run"],
    instruction: 'Run the configured durable Baldr workflow for the supplied task and workspace. Prefer run_architect_implement_review. Baldr controls provider dialogue and frozen execution-profile snapshots; do not create uncontrolled provider-to-provider recursion. Return run_id, durable status, consolidated structured report, verification performed, blockers, evidence, and follow-up items.',
  }
] as const;

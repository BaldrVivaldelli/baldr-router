# Multi-agent workflows

Baldr Router can coordinate several provider-backed roles without letting them talk directly to each other.

```text
client
  -> baldr-router
      -> architect.plan
      -> implementer.implement
      -> reviewer.review
      -> implementer.fix_round?   max_rounds bounded
      -> reviewer.final_review?
```

## First workflow

The first implemented workflow is:

```text
architect-implement-review
```

It is designed for development tasks where planning, implementation, and review should be separate responsibilities.

## Roles and execution profiles

Roles define permissions and phase semantics. Named execution profiles define provider/model/agent execution. One profile may be shared by every role, or each phase may reference an independent list.

```toml
[execution_profiles.shared]
provider = "codex"
session_scope = "workspace"

[roles.architect]
profiles = ["shared"]
can_write = false
sandbox = "read-only"

[roles.implementer]
profiles = ["shared"]
can_write = true
sandbox = "workspace-write"

[roles.reviewer]
profiles = ["shared"]
can_write = false
sandbox = "read-only"
```

For n/m/l participation by phase:

```toml
[roles.architect]
profiles = ["arch-primary", "arch-fallback"]
strategy = "first-success"

[roles.implementer]
profiles = ["implementation"]
strategy = "first-success"

[roles.reviewer]
profiles = ["review-a", "review-b"]
strategy = "all"
min_successes = 2
```

A write-enabled role cannot use `strategy = "all"` with multiple profiles. See [`durable-orchestration.md`](durable-orchestration.md) for the full contract, session isolation and recovery semantics.

The simple CLI role command still creates one inline profile:

```bash
baldr-router set-role-provider architect kiro-cli --agent baldr-architect --effort high
baldr-router set-role-provider implementer codex
baldr-router set-role-provider reviewer kiro-cli --agent baldr-reviewer --effort high
```

## Dry run

Preview selected roles/providers without launching providers:

```bash
baldr-router run-workflow /path/to/repo "Implement auth refresh tokens" --dry-run
```

## MCP tools

```text
router_workflow_status
router_list_roles
router_list_workflows
router_set_role_provider
run_workflow
run_architect_implement_review
```

## Anti-recursion guard

Provider child processes receive:

```text
BALDR_ROUTER_RUN_ID
BALDR_ROUTER_WORKFLOW
BALDR_ROUTER_ACTIVE_ROLE
BALDR_ROUTER_PARENT_PROVIDER
BALDR_ROUTER_DEPTH
BALDR_ROUTER_DISABLE_REENTRY=1
```

If a child provider calls baldr-router again, workflow/delegation tools refuse the re-entry by default. This prevents loops like:

```text
baldr-router -> kiro-cli -> baldr-router -> kiro-cli -> ...
```

## Recommended provider roles

```text
Codex
  best as implementer; also useful as reviewer or architect if Kiro CLI is not configured.

Kiro CLI
  best as architect, reviewer, or second-opinion provider.

Context7
  docs/context provider, not an implementer.
```

## Provider registry boundary

The workflow engine never branches on concrete provider names. Every role step goes through `ProviderRegistry` and the `ProviderAdapter` protocol. Provider capability metadata is included in status/results, including whether a requested read-only or write boundary is technically enforced or advisory.

During the v0.16 feature freeze, this workflow and the built-in provider set are contract-tested and should not be expanded.

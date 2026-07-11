# Safety boundaries

Baldr Router should be useful without making the workspace unsafe.

## Secrets

Never ask the user to paste API keys, access tokens, passwords, private keys, or Codex auth files into chat.

Use local setup commands instead:

```bash
baldr-router setup-context7 --mode hybrid --install-codex-mcp
```

Secrets must stay outside the repo.

## Provider calls

Kiro must call providers through `baldr-router`, not directly.

Prefer:

```text
Kiro -> baldr-orchestrator Power -> baldr-router -> provider
```

Do not use by default:

```text
Kiro -> Codex direct
Kiro -> Context7 direct
```

## Sandboxing

Default implementation sandbox should be `workspace-write` when supported.

Review sandbox should be `read-only` when supported.

Do not use `danger-full-access` unless the user explicitly understands and approves the risk.

## Verification

Never mark a Kiro task complete only because a provider says it is done.

Always verify:

- diff
- tests
- lint/typecheck/build when relevant
- acceptance criteria
- risks/follow-ups from `final_report`

## Telemetry

Telemetry is for diagnosis and performance analysis. It is not correctness proof.

By default, the router should avoid storing raw prompts/events. If raw event logging is enabled, warn the user that it may contain repo context.


## Agentic recursion guard

Avoid loops like:

```text
Kiro -> baldr-router -> kiro-cli -> baldr-router -> kiro-cli -> ...
```

Baldr Router passes these to provider child processes:

```text
BALDR_ROUTER_RUN_ID
BALDR_ROUTER_WORKFLOW
BALDR_ROUTER_ACTIVE_ROLE
BALDR_ROUTER_DISABLE_REENTRY=1
```

If a child provider tries to call Baldr Router again, the router should block workflow/delegation tools. Do not override this automatically.

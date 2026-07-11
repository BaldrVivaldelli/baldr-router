# Kiro mapping to the shared facade contract

Kiro has three public Baldr intentions:

```text
first-run / configuration → setup
health / diagnostics      → status
spec task / implementation → run
```

For `run`, translate the active Kiro task into a concise task plus relevant spec context, then invoke the existing Baldr workflow. Do not manually coordinate architect, implementer, or reviewer conversations.

After the core returns:

1. inspect the consolidated final report;
2. inspect the diff;
3. run relevant tests, lint, typecheck, or build;
4. mark the Kiro task complete only when verification passes.

Execution-profile resolution, durable state/recovery, provider selection, Context7 enrichment, telemetry, structured output, fix rounds, and verification gates remain core responsibilities.

# Baldr Agent Builder

`baldr-agent-builder` owns the language-aware development lifecycle for
externally owned Baldr agents. It installs the `baldr-agent` executable and
keeps build and publication concerns out of every language SDK.

```bash
baldr-agent init ./my-agents \
  --name repository-report \
  --owner product-team \
  --namespace product \
  --language typescript
cd my-agents
baldr-agent test
baldr-agent driver conformance baldr.typescript
baldr-agent build
baldr-agent publish
baldr-agent doctor
baldr-agent run --role implementer --workspace ../demo --request "Create the result"
```

The Builder reads `baldr-agent.toml`, runs declared tests, creates a
deterministic self-contained artifact, installs immutable release metadata,
publishes through the local catalog or Agent Manager, executes an ephemeral
development release through Runner and can reactivate a previous version with
`baldr-agent rollback VERSION`.

`test`, `build` and `publish` use the versioned
[`Builder Protocol`](../../docs/builder-protocol.md): the CLI talks to a
transport-neutral client, the local backend selects an exact driver, and the
Python or TypeScript driver runs as a bounded JSONL process. Additional drivers
use the same registration and execution contracts.

Agent source depends on a language SDK such as `baldr-agent-sdk` or
`@baldr/agent-sdk`; it never imports Builder internals. The built-in Python
driver packages Python projects, while `baldr.typescript` packages TypeScript
projects without changing Router, Agent Manager or Runner.

```bash
baldr-agent driver register \
  ../agent-builder-typescript/baldr-builder-driver.json
baldr-agent driver list
```

## Internal boundaries

The package keeps each lifecycle responsibility explicit:

```text
baldr_agent_builder/
├── cli.py              argument parsing, JSON output and error handling
├── client.py           transport-neutral Builder client
├── backend.py          local service binding and driver selection
├── protocol.py         protocol constants and bounded validation
├── driver.py           built-in Python JSONL driver
├── drivers.py          driver discovery, registration and exact selection
├── conformance.py      neutral driver compatibility and reproducibility gate
├── execution.py        exact ephemeral execution through Agent Runner
├── inventory.py        language-neutral source inventory and digest
├── models.py           immutable project, build and release values
├── config.py           baldr-agent.toml parsing and validation
├── scaffold.py         project creation from packaged templates
├── build.py            source inventory and deterministic artifacts
├── release.py          installation, publication, activation and rollback
├── diagnostics.py      declared tests and health checks
└── templates/          files emitted by baldr-agent init
```

There is deliberately no aggregate `project.py` compatibility module. Internal
callers import the responsibility they use, while the supported user-facing
contract remains the `baldr-agent` executable and `baldr-agent.toml`.

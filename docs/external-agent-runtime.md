# External agent runtime v1

Baldr can coordinate agents whose code, prompts and release lifecycle remain in
another repository. Baldr stores only their immutable identity and location;
it does not import product-agent code into the router.

```text
external agent repository                   Baldr infrastructure

agent code + tests
       | imports
       v
baldr-agent-sdk       baldr-agent.toml
       |                     |
       +---- packaged by ----+---> baldr-agent-builder
                                      |
                                artifact + manifest
                                      |
                                      v
                           Agent Manager / local registry
                                      |
                                exact resolution
                                      v
                                 AgentGateway
                                      |
                               baldr-agent-runner
                                      |
                           explicit workspace boundary
```

The public protocol is
[`agent-execution-v1.schema.json`](../contracts/agent-execution-v1.schema.json).
It covers health, invocation acceptance, status, paginated events,
cancellation and terminal results. Every invocation carries the exact
`AgentRef`, manifest digest, capabilities, effect mode, durable run/step/attempt
ids, timeout and idempotency key.

## Monorepo boundaries

The monorepo contains infrastructure only:

```text
contracts/agent-execution-v1.schema.json  wire contract
sdks/python/                              public authoring SDK
sdks/typescript/                          public TypeScript authoring SDK
tooling/agent-builder/                    build and release toolchain
tooling/agent-builder-typescript/         external TypeScript build driver
runtimes/agent-runner/                    local data-plane process
router/                                   resolution, policy and orchestration
```

The toolchain is language-extensible through
[`Builder Protocol v1`](builder-protocol.md). SDKs remain authoring-only;
developer tools call Builder, and Builder delegates testing and packaging to an
exact, content-identified language driver.

An owning team keeps its actual agent in its own repository, versions it,
builds the executable artifact and publishes a manifest. A fixture under a
test directory is allowed; a real product agent is not a Baldr component.

## Install from this checkout

Install the control plane and the independent runner in environments visible
on the same `PATH`:

```bash
uv tool install --force --editable ./router
uv tool install --force --editable ./runtimes/agent-runner \
  --with-editable ./sdks/python \
  --with-editable ./tooling/agent-builder \
  --with-executables-from baldr-agent-builder

baldr-agent-runner health
baldr-agent --help
```

The SDK is the only Baldr library imported by external agent source:

```bash
uv add baldr-agent-sdk
```

The release produces separate `baldr-router`, `baldr-agent-sdk`,
`baldr-agent-builder` and `baldr-agent-runner` wheels. This keeps authoring,
lifecycle and execution independently deployable;
VS Code, Kiro, Codex and MCP continue to use the existing router surfaces.
The VS Code private runtime bundles the router as before, so a local-process
agent additionally requires `baldr-agent-runner` on the runtime `PATH` (or an
explicit `BALDR_AGENT_RUNNER_COMMAND` pointing to its executable).

## Repository lifecycle

Agent Builder installs `baldr-agent`, keeps agent code in the owning team's
repository and exposes one language-neutral lifecycle:

```bash
baldr-agent init ./product-agents \
  --name repository-report \
  --owner product-team \
  --namespace product \
  --language python  # or typescript
cd product-agents
baldr-agent test
baldr-agent driver conformance baldr.python
baldr-agent build
baldr-agent publish
baldr-agent doctor
baldr-agent run \
  --role implementer \
  --workspace /ruta/al/workspace \
  --request "Generá el resultado"
```

The generated `baldr-agent.toml` schema v2 declares exact identity, version,
language, entrypoint, driver, roles, capabilities, sources and tests. Existing
schema v1 Python projects remain compatible. `build` emits either a
deterministic Python `.pyz` or a self-contained Node `.cjs`; both embed their
public SDK runtime, so the release has no relative path to a Baldr checkout,
project virtual environment or `node_modules`. `publish` copies it to
`${XDG_DATA_HOME:-~/.local/share}/baldr-agent/artifacts/<registry>/<namespace>/<project>/<version>/`,
generates manifests only after that stable installation, synchronizes the
local catalog and activates the exact version. Use `--catalog manager` to
publish the same manifests through Agent Manager.

Exact versions are immutable: the portable project definition is included in
the artifact digest, republishing an identical release is idempotent, and
changed source, roles, capabilities, ownership, effects or manifest metadata
under an installed version are rejected. Run `baldr-agent version VERSION`,
publish again, and use `baldr-agent rollback VERSION` if the team must
reactivate a previous local release. Baldr stores identity, digest and stable
location; it still does not own the agent source.

## Author an external Python agent

```python
from baldr_agent_sdk import Agent

agent = Agent(
    ref="company://product/reviewer@1.0.0",
    owner="product-team",
    capabilities=("workspace.read", "role.reviewer"),
)


@agent.invoke
def review(request, context):
    context.emit("verifying", "Review completed")
    return {
        "ok": True,
        "final_report": {
            "status": "approved",
            "summary": "No material defects found",
            # Include the remaining Baldr final-report fields used by the role.
        },
    }


if __name__ == "__main__":
    raise SystemExit(agent.serve_stdio())
```

Generate a local-process manifest from the built artifact:

```python
manifest = agent.local_process_manifest(
    command="python",
    arguments=("/opt/product-agents/reviewer.py",),
    artifact_path="/opt/product-agents/reviewer.py",
    timeout_seconds=900,
)
Agent.write_manifest("reviewer.agent.json", manifest)
```

The manifest pins both its canonical content digest and the artifact SHA-256.
Changing the program requires a new immutable AgentRef version and manifest.
Publish it through `baldr-router agent publish`/Agent Manager or synchronize a
manifest source as described in
[`external-agent-registry.md`](external-agent-registry.md).

`driver conformance` is the release gate for a language implementation. It
checks stable driver identity, fail-closed protocol negotiation, the project's
real tests, artifact attestation, reproducible bytes and relocation away from
the source checkout. `run` builds and installs an ephemeral exact release,
invokes the selected role through Agent Runner and removes that installation
after the invocation. It never bypasses the role's declared read/write effect.

## Author an external TypeScript agent

```ts
import { Agent } from "@baldr/agent-sdk";

const agent = new Agent({
  ref: process.env.BALDR_AGENT_REF!,
  owner: "product-team",
  capabilities: ["workspace.read", "role.reviewer"],
});

agent.invoke((_request, context) => {
  context.emit("verifying", "Review completed");
  return {
    ok: true,
    final_report: { status: "approved", summary: "No material defects found" },
  };
});

process.exitCode = await agent.serveStdio();
```

Register `tooling/agent-builder-typescript/baldr-builder-driver.json`, or place
its executable on `PATH`, then use the same `baldr-agent test`, `build` and
`publish` lifecycle. Run `baldr-agent driver conformance baldr.typescript`
before promotion. The published manifest selects `node` and pins the `.cjs`
artifact digest; Baldr's runner and orchestration path remain language-neutral.

Release artifacts support installation without a source checkout:

```bash
npm install --global \
  ./baldr-agent-sdk-0.20.0.tgz \
  ./baldr-agent-builder-typescript-0.20.0.tgz
baldr-agent driver doctor baldr.typescript
```

The global package exposes `baldr-builder-driver-typescript` on `PATH` and no
persisted registration is required. After registry publication, the driver can
be installed by package name and resolves the exact matching SDK version.

## Workspace and security boundary

The gateway first intersects requested capabilities with the manifest. The
runner then applies the data-plane boundary:

- `read-only`: a bounded disposable snapshot is created without repository
  metadata, generated dependency/build directories, symlinks or special files;
  omitted entries are never followed, write bits are removed and the original
  workspace path is never sent to the agent;
- `workspace-write`: the exact workflow workspace is passed only when both the
  phase and manifest declare `workspace.write`; Baldr already enforces exactly
  one participant for every write phase;
- local artifacts must be regular non-symlink files and match the pinned
  SHA-256 immediately before execution;
- the runner starts a separate process group, bounds input/output and time,
  and persists jobs/events in a private SQLite store;
- credentials cannot appear in a manifest target. The v1 local runner passes a
  minimal environment; a future secret-provider contract can grant named
  credentials without embedding values in metadata.

Cancellation is deliberately conservative. A cancelled read job is
`cancelled` and retryable. A cancelled or lost write job is `unknown`, because
the process may have changed the workspace before termination; normal durable
reconciliation decides what to do next.

Only the fixed public activity categories `working`, `analyzing`,
`researching`, `changing` and `verifying` enter Baldr's durable progress log.
Free-form event messages remain in the runner's private event store and cannot
leak into the UI activity channel.

## Transports and compatibility

- `local-process` uses the independent runner and supports explicit local
  workspace reads/writes.
- `http-json` remains backward compatible with the original synchronous,
  read-only invocation/result contract. A target with
  `protocol = "agent-execution-v1"` uses the new invoke/result envelope. Its
  workspace root is `null`; the remote service receives no local path and
  cannot request local writes.
- `provider` continues to adapt normal Codex and Kiro execution. Existing
  profiles without `agent_ref` are unchanged.

This means an organization can migrate one role at a time. A missing compatible
external agent still falls back to the configured Codex/Kiro profile in
automatic mode; an explicitly pinned missing agent fails loudly.

## Failure and recovery semantics

- retries preserve `job_id` and idempotency key;
- reusing an idempotency key with different content is rejected;
- completed retries return the stored terminal result without running the
  artifact again;
- artifact, identity and protocol mismatches fail closed;
- status and event requests survive the invoking router process;
- write timeouts, cancellation and process loss become `unknown` rather than a
  false success.

## Evolution

The protocol is language-neutral. Python and TypeScript now implement the same
schema and canonical manifest semantics without changing the router. Future
SDKs and connectors can place the runner behind a container, Kubernetes job or
durable queue while preserving `AgentRef`, idempotency, events, cancellation
and result semantics. Container images and queue consumers should pin an
artifact/image digest and must prove a workspace or external-effect boundary
before they can advertise write capabilities.

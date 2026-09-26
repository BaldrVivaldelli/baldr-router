# External agent resolution

Baldr coordinates agents that remain owned and executed outside the router. An
external binding uses four separate contracts:

```text
AgentRef
  -> AgentResolver
      -> AgentManifest
          -> AgentGateway policy check
              -> transport connector
                  -> externally owned agent
```

Discovery is deliberately separate from resolution:

```text
AgentSource --metadata only--> candidate AgentManifest
                                  |
                                  | explicit catalog synchronization
                                  v
AgentResolver --exact AgentRef + digest--> ResolvedAgent
```

An `AgentSource` says where external agents live and returns inert candidate
metadata. It cannot make a candidate executable, grant permissions, register a
version, or load agent code. Registration and exact resolution remain separate
steps, so merely pointing Baldr at a source has no execution side effect.

The local registry is the bootstrap resolver. It is metadata-only: Baldr reads
the manifest, verifies its identity and invokes the declared connector. It does
not import or evaluate agent code from the registry file.

## Agent Sources v1

Every discovery result implements
[`agent-source-v1.schema.json`](../contracts/agent-source-v1.schema.json). Each
candidate contains:

- a complete, exact-version `AgentManifest` and its canonical digest;
- source id, source kind, native id, scope and a non-secret locator;
- an availability state (`available`, `shadowed` or `unavailable`);
- optional display metadata and bounded warnings.

The initial adapters are:

- `KiroAgentSource`, which reads regular JSON definitions in workspace/global
  `.kiro/agents` directories and parses only built-in rows from
  `kiro-cli agent list`. It invokes no agent. File versions are derived from
  the full definition digest; built-ins are pinned to the Kiro CLI version and
  rechecked before invocation. Workspace definitions explicitly mark a
  same-named global definition as shadowed.
- `AgentManagerSource`, which adapts the existing authenticated manager catalog
  without resolving or invoking any candidate.
- `ManifestAgentSource`, which reads the same v1 document from a bounded,
  non-symlink JSON file or an HTTPS endpoint. Plain HTTP requires an explicit
  loopback pilot flag, and credentials are referenced only by environment
  variable name.

Preview these sources without registering anything:

```text
baldr-router agent discover --source kiro --workspace .
baldr-router agent discover --source manager --workspace .
baldr-router agent discover --source file --path ./agents.source.json \
  --expected-source-id product.agents
baldr-router agent discover --source endpoint \
  --endpoint https://agents.example.test/v1/source \
  --authorization-env PRODUCT_AGENT_SOURCE_TOKEN \
  --expected-source-id product.agents
```

Source documents and manifests cannot contain inline credentials. Endpoint
authorization values stay outside public output and provenance URLs reject
userinfo, query strings and fragments. The catalog synchronization layer
consumes these candidates through an explicit preview/diff before changing the
local registry.

## Catalog preview and synchronization

The normal workflow has only two commands:

```text
# Read-only preview: new, new-version, unchanged, disabled, unavailable,
# absent, revoked or conflict.
baldr-router agent sync --source kiro --workspace .

# Apply safe additions and exact matches. Missing agents are kept by default.
baldr-router agent sync --source kiro --workspace . --apply
```

The preview implements
[`agent-catalog-sync-v1.schema.json`](../contracts/agent-catalog-sync-v1.schema.json)
and includes a digest of the registry baseline and source ownership. Applying
the same result twice is a no-op. A changed agent definition creates a new
exact version; the synchronizer never overwrites an existing `AgentRef` or
digest.

Ownership is conservative:

- a version published by synchronization is `managed` by that source;
- an identical version that was registered manually is only `observed`;
- observed versions keep their manual enabled/disabled state;
- only managed versions can receive automatic lifecycle decisions from their
  source.

Missing or shadowed managed versions are preserved unless an operator supplies
an explicit decision:

```text
baldr-router agent sync --source kiro --workspace . --apply \
  --missing-action disable
```

If discovery has any warning, disable/revoke is rejected because the catalog
may be partial. Lifecycle changes are also rejected while an active durable
run references the version. Irreversible revocation additionally requires the
exact source id shown in preview:

```text
baldr-router agent sync --source kiro --workspace . --apply \
  --missing-action revoke --confirm-revoke kiro.local
```

Local revocations cannot be enabled or removed. Normal disabled removals leave
an immutable digest tombstone, so deleting a registry entry cannot be used to
republish different content under the same version. Source ownership and
bounded audit events live next to the registry in a private `*.sync.json`
sidecar; inspect safe counts and the latest event with
`baldr-router agent sync-status`.

## Exact agent references

References have the form:

```text
registry://namespace/name@version
```

For example:

```text
local://kiro/baldr-worker@1.0.0
company://cyber/threat-analyzer@3.1.0
```

Query strings, fragments and unversioned references are rejected. Every
manifest has a canonical SHA-256 digest. Baldr resolves that digest before it
creates the immutable workflow snapshot, then records the reference, digest,
registry and transport on each durable participant.

Changing the content of an already resolved version causes a digest mismatch
instead of silently changing a resumed workflow.

For file-backed Kiro agents, a provider target can additionally bind the
manifest to the exact external agent definition:

```json
{
  "provider": "kiro-cli",
  "agent": "baldr-worker",
  "definition_scope": "global",
  "definition_digest": "sha256:<digest of ~/.kiro/agents/baldr-worker.json>"
}
```

When these fields are present, Baldr verifies a regular, non-symlink JSON file
of at most 1 MiB, checks that its `name` matches `target.agent`, and compares
its SHA-256 before every invocation. A global definition also fails closed if
`.kiro/agents/<agent>.json` in the active workspace would shadow it. Version a
write-enabled definition under a new AgentRef instead of changing the file
behind an existing reference.

## Agents that are configuration rather than code

Not every agent is a program. A manifest with `transport: "provider"` runs
through an adapter Baldr already has, and its `target` is the whole definition:
there is nothing to build, package or publish an artifact for.

```json
{
  "ref": "local://equipo/revisor-seguridad@1.0.0",
  "owner": "equipo-plataforma",
  "transport": "provider",
  "capabilities": ["workspace.read", "role.reviewer"],
  "effect_mode": "read-only",
  "target": {
    "provider": "claude",
    "model": "opus",
    "tools": "Read,Grep",
    "instructions": "Sos un revisor de seguridad. Mirá la superficie pública."
  }
}
```

`tools` and `instructions` are declarations, and both only ever narrow. The
role decides whether a phase may write; a manifest cannot change that answer.
A read-only phase keeps the flags that remove the command-running and editing
tools, and `tools` is filtered against an allowlist on top of them, so a tool
nobody recognises is refused rather than admitted and a narrowed shell is still
a shell. What a phase asked for and did not get is reported as `refused_tools`
rather than dropped, because a declaration that quietly did less than it said
is worse than one that says so.

`instructions` is appended to the system prompt, never substituted for it: the
phase's own prompt and its report contract come first, and an instruction
grants nothing, because the flags decide what exists to be used.

The manifest is still immutable per exact version. Changing what an agent may
touch means publishing a new version, which is what keeps a durable run's
record of who did what true after the fact.

### Declaring them in the repository

A manifest carries a digest, and nobody writes one of those by hand. The
authoring file is what a person edits and a reviewer reads in a diff; the
manifest is compiled from it.

```toml
# baldr-agents.toml
[source]
id = "repo.agents"
label = "Agentes de este repositorio"

[[agent]]
ref = "local://equipo/revisor-seguridad@1.0.0"
owner = "equipo-plataforma"
description = "Revisa la superficie pública buscando problemas de seguridad"
capabilities = ["workspace.read", "role.reviewer"]
provider = "claude"
model = "opus"
tools = "Read,Grep"
instructions = "Mirá autenticación y manejo de secretos."
```

One flat table per agent. Keys that describe the agent stay at the manifest
level and keys that describe how to reach it become its `target`; an unknown
key stops the plan rather than being ignored, because a typo in this file is a
rule that silently went missing. `transport` defaults to `provider`, and an
agent is read-only unless the file says otherwise — an agent that can change a
workspace should be unmistakable in the diff that introduced it, and both
halves of that permission have to agree before the file is accepted.

```bash
make agents         # what would change, and nothing else
make agents-apply   # converge the catalog on the file
```

Reading the plan is the default because applying one changes which agents may
touch a workspace. What the file no longer declares is disabled rather than
revoked, so a removal somebody regrets is reversible.

The reconciliation underneath is the one that already existed:

```bash
baldr-router agent sync --source declarative --path baldr-agents.toml [--apply]
```

Editing an agent in place is a conflict, not an update. An exact version is
immutable, so a changed `tools` line under the same reference is refused and
the plan says so; publishing `@1.1.0` instead registers the new agent and
reports the old one as absent, to be disabled or kept by `--missing-action`.

## Local registry

The default path is:

```text
${XDG_CONFIG_HOME:-~/.config}/baldr-router/agents.json
```

`BALDR_AGENT_REGISTRY_PATH` can select another file. The file must implement
[`agent-registry-v1.schema.json`](../contracts/agent-registry-v1.schema.json)
and is limited to 1 MiB and 1,000 manifests.

A Kiro-compatible example is available at
[`examples/agents.local.json`](../examples/agents.local.json). It points to the
existing `kiro-cli` adapter and agent name; the Kiro agent remains external.

Use the administrative CLI instead of editing the JSON file directly:

```text
baldr-router agent list --workspace .
baldr-router agent inspect local://kiro/baldr-worker@1.0.0
baldr-router agent publish local://codex/reviewer@1.0.0 \
  --owner product --transport provider --target provider=codex \
  --capability workspace.read
baldr-router agent disable local://codex/reviewer@1.0.0
baldr-router agent enable local://codex/reviewer@1.0.0
baldr-router agent remove local://codex/reviewer@1.0.0
```

Publishing the same reference with different content is rejected; use a new
version instead. Removal requires the exact version to be disabled and fails
while an active durable run still references it. Writes are atomic and the
registry file is kept private to the local user.

Configure a role profile to bind the immutable reference:

```toml
[execution_profiles.external-kiro]
agent_ref = "local://kiro/baldr-worker@1.0.0"
agent_manifest_digest = "sha256:7e4ed0661ea2e464e7eb2ed17e24281c17a8a2ef39cde1dfcf41a8bd1d8c4b75"

[roles.architect]
profiles = ["external-kiro"]
```

The legacy `provider`, `model`, `agent` and `runner` fields remain valid. An
empty `agent_ref` follows exactly the previous ProviderRegistry path, which
keeps existing configurations and durable snapshots compatible.

## Resolving the team

New workspaces default to `automatic` team resolution. Baldr evaluates the
registered catalog independently for planning, execution and review. A
candidate must be enabled, ready, digest-valid and compatible with the role's
capabilities. Execution additionally requires both `workspace.write` and
`effect_mode = "workspace-write"`; read stages prefer least privilege. Exact
configured references, role-specific capabilities, the newest semantic
version and finally the lexical AgentRef form a deterministic ordering.

If no compatible external agent is ready, Baldr keeps the normal configured
Codex/Kiro profile and records that fallback in the durable snapshot. Existing
persisted workspaces migrate as `configured`, so upgrading never silently
changes their team.

The VS Code menu exposes only three normal choices: automatic, pin an agent to
one stage, or use Codex/Kiro normally. A pin is an explicit AgentRef override;
an absent, disabled, revoked or incompatible pin fails with a human-readable
error instead of silently selecting another agent. The exact manifest digest
is still resolved and frozen when the run begins.

The same choices are available to other clients:

```text
baldr-router facade setup . --team-mode automatic
baldr-router facade setup . --team-mode automatic \
  --agent-override reviewer=local://kiro/security-reviewer@1.0.0
baldr-router facade setup . --team-mode configured --clear-agent-overrides
```

The durable decision implements
[`agent-team-resolution-v1.schema.json`](../contracts/agent-team-resolution-v1.schema.json).

## Policy boundary

The registry cannot grant permissions. Before invocation the gateway
intersects the workflow request with the capabilities and maximum effect mode
declared by the manifest. A write-enabled step requires both
`workspace.write` and `effect_mode = "workspace-write"`; normal workspace trust,
sandbox and operator authorization checks still apply.

Registry targets cannot contain inline tokens, passwords, API keys or other
credentials. Future remote resolvers should return credential references whose
values are obtained from the deployment's secret manager.

The Kiro adapter also tolerates observable tool activity before the requested
JSON report. Terminal control sequences are removed and only a complete JSON
value is accepted, so tool logs do not degrade a valid structured result into
an opaque summary.

`baldr-router kiro-mcp-status` diagnoses Kiro's optional organizational MCP
registry separately from core agent health. This separation prevents an MCP
registry outage from disabling Codex/Kiro execution or causing recursive MCP
startup probes.

## Replacing the local registry

`AgentResolver` is independent from the file format. The Agent Manager can
implement the same exact-reference resolution contract and return the same
`ResolvedAgent` metadata. Workflows and the durable engine do not need to know
whether a manifest came from the bootstrap file, a company catalog or a SaaS
control plane.

The `provider` transport wraps the current Codex/Kiro adapters. The independent
`local-process` connector executes digest-pinned external artifacts through
`baldr-agent-runner`; HTTP can use either its compatible legacy envelope or the
transport-neutral execution v1 envelope. Future MCP, queue and container
connectors can still be registered behind `AgentGateway` without storing their
agents in Baldr.

The first independent connector and persistent manager implementation are described
in [`external-agent-http.md`](external-agent-http.md). They add the read-only
`http-json` transport, exact remote resolution, catalog and health contracts,
and the VS Code agent selector.

The SDK, local runner, workspace boundary and execution lifecycle are described
in [`external-agent-runtime.md`](external-agent-runtime.md).

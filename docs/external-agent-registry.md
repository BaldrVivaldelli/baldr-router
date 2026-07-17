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

The local registry is the bootstrap resolver. It is metadata-only: Baldr reads
the manifest, verifies its identity and invokes the declared connector. It does
not import or evaluate agent code from the registry file.

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

The initial `provider` transport intentionally wraps the current Codex/Kiro
adapters. Additional MCP, HTTP, queue or isolated-runner connectors can be
registered behind `AgentGateway` without storing their agents in Baldr.

The first independent connector and persistent manager implementation are described
in [`external-agent-http.md`](external-agent-http.md). They add the read-only
`http-json` transport, exact remote resolution, catalog and health contracts,
and the VS Code agent selector.

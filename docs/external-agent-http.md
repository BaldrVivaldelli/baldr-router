# External agent HTTP and Agent Manager contracts

Baldr's `http-json` connector invokes an externally hosted, read-only agent
without passing through `ProviderRegistry`. The public wire contract is
[`agent-transport-http-v1.schema.json`](../contracts/agent-transport-http-v1.schema.json).

The manifest target contains metadata, never credential values:

```json
{
  "transport": "http-json",
  "target": {
    "endpoint": "https://agents.example.test/v1/invoke",
    "authorization_env": "BALDR_AGENT_HTTP_TOKEN",
    "timeout_seconds": "30"
  }
}
```

`authorization_env` names an environment variable. Baldr reads its value only
when building the HTTP `Authorization` header and does not publish it in
status, evidence or the durable snapshot.

HTTPS is mandatory. Plain HTTP is accepted only for loopback pilots when
`BALDR_AGENT_ALLOW_INSECURE_LOOPBACK=1` or an equivalent explicitly enabled
client is used. Redirects, URL credentials, oversized messages and responses
with the wrong content type or contract are rejected.

HTTP transport v1 is intentionally read-only. It does not send the local
workspace path or child-provider environment to the remote process. Writable
agents require a connector that can prove its shared or isolated workspace
boundary; the Kiro compatibility pilot continues to use the existing provider
connector for that purpose.

For new endpoints, add `"protocol": "agent-execution-v1"` to the target. The
request then implements the shared
[`agent-execution-v1`](../contracts/agent-execution-v1.schema.json) invoke/result
envelope, including immutable identity, durable ids, timeout and idempotency.
The workspace root is explicitly `null`, so this HTTP mode remains read-only.
Endpoints without that target field continue to receive the original
`baldr-agent-invocation` contract unchanged.

## Agent Manager v1

The optional Agent Manager is configured without storing a credential value:

```toml
[agent_manager]
enabled = true
registry = "manager"
base_url = "https://agent-manager.example.test"
authorization_env = "BALDR_AGENT_MANAGER_TOKEN"
timeout_seconds = 10
allow_insecure_loopback = false
catalog_limit = 100
```

The bundled persistent service exposes bounded read and administration endpoints described by
[`agent-manager-v1.schema.json`](../contracts/agent-manager-v1.schema.json):

```text
GET /v1/health
GET /v1/agents?limit=100
GET /v1/agents/{namespace}/{name}/versions/{exact-version}
POST /v1/agents
POST /v1/agents/{namespace}/{name}/versions/{exact-version}/enable
POST /v1/agents/{namespace}/{name}/versions/{exact-version}/disable
POST /v1/agents/{namespace}/{name}/versions/{exact-version}/revoke
GET /v1/audit?after=0&limit=100
GET /v1/metrics
GET /livez
GET /readyz
```

Start a loopback instance backed by SQLite and configure the client with:

```text
export BALDR_AGENT_MANAGER_TOKEN="<local secret>"
baldr-router agent-manager serve --database ~/.local/state/baldr-router/agent-manager.sqlite3
baldr-router agent-manager configure --registry manager --base-url http://127.0.0.1:8766 \
  --authorization-env BALDR_AGENT_MANAGER_TOKEN --allow-insecure-loopback
```

`publish`, `enable`, `disable`, `revoke` and `status` are available beneath
`baldr-router agent-manager`. Published exact versions are immutable;
revocation is irreversible. The service stores only manifests and state, and
the configured credential is referenced by environment-variable name rather
than persisted as a value.

For multi-team deployments, pass `--policy` instead of sharing the legacy
single administrator credential. The versioned policy maps environment-backed
principals to reader, publisher, operator, auditor or admin roles and scopes
them by AgentRef namespace (tenant) and manifest owner. Publication files,
backup/upgrade procedure, probes and audit semantics are documented in
[`agent-manager-operations.md`](agent-manager-operations.md).

Resolution accepts only the exact requested `AgentRef`. The returned manifest
must validate its own SHA-256 digest; a durable expected digest must also
match. A manager therefore cannot silently replace an agent version during a
resumed workflow.

`baldr-router agent-catalog` combines safe local and manager metadata, health,
version and last durable execution without
returning transport targets. In VS Code choose **Agentes externos** from the
plus menu or **Equipo de Baldr → Usar un agente externo registrado**. The
selector filters capabilities for the chosen phase, creates the immutable
profile and assigns it without requiring manual TOML or JSON editing. **Equipo
de Baldr → Administrar agentes externos** registers and manages the local
bootstrap catalog; **Volver a Codex o Kiro normal** removes an external
binding from one role without changing the others.

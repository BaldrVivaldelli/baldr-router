# Operating the Agent Manager

The Agent Manager is Baldr's catalog control plane. It stores immutable
manifests and their lifecycle state; it does not host agent code, proxy model
credentials, or replace the external Codex/Kiro runtimes.

## Trust boundary

Run the service behind TLS and an authenticated network boundary. The bundled
HTTP server is suitable as the application process; TLS termination, rate
limits and network admission belong in the deployment's reverse proxy or
service mesh. Plain HTTP is accepted by Baldr clients only for an explicitly
enabled loopback deployment.

Credentials are never written in a policy, manifest, SQLite row, audit event
or API response. A policy names environment variables and the service resolves
their values once at startup. Rotate a credential by updating the secret
provider and restarting the process.

## RBAC and tenancy

Create a policy skeleton:

```text
baldr-router agent-manager init-policy ./manager-policy.json \
  --registry company \
  --principal-id product-publisher \
  --credential-env PRODUCT_MANAGER_TOKEN \
  --role publisher \
  --tenant product \
  --owner-scope product-team
```

Add more principals to the JSON as needed. The policy implements
`baldr-agent-manager-policy` v1. Its roles are intentionally small:

| Role | Permissions |
|---|---|
| `reader` | health, scoped catalog and exact resolution |
| `publisher` | reader permissions plus immutable publication |
| `operator` | reader permissions plus enable, disable, revoke and metrics |
| `auditor` | health, scoped audit and metrics |
| `admin` | every action |

An AgentRef namespace is the tenant boundary. For example,
`company://product/reviewer@2.0.0` belongs to tenant `product`. Publication
also checks the manifest's `owner` against the principal's owner scopes.
`"*"` is accepted only as the sole scope value and should be reserved for a
small number of administrators. Catalogs, resolution, metrics and audit are
filtered server-side; a client cannot select another tenant with a request
parameter.

Start the service:

```text
export PRODUCT_MANAGER_TOKEN="<value supplied by the deployment secret store>"
baldr-router agent-manager serve \
  --host 127.0.0.1 --port 8766 \
  --registry company \
  --database /var/lib/baldr/agent-manager.sqlite3 \
  --policy /etc/baldr/manager-policy.json
```

The old `--authorization-env` mode remains compatible and maps one credential
to a wildcard `admin` principal. New shared or remote deployments should use a
policy so publishing, operation and audit are separate authorities.

## Publishing an externally owned agent

The publication file contains the external location and credential reference,
not executable code or a secret value:

```text
baldr-router agent-manager init-manifest ./reviewer.agent.json \
  company://product/reviewer@2.0.0 \
  --owner product-team \
  --transport http-json \
  --target endpoint=https://agents.example.test/v1/review \
  --target authorization_env=PRODUCT_AGENT_TOKEN \
  --capability workspace.read

baldr-router agent-manager validate-manifest ./reviewer.agent.json
baldr-router agent-manager publish-file ./reviewer.agent.json
```

The same flow is available to Python integrations through
`AgentManagerPublisher`, `write_agent_publication` and
`load_agent_publication`. Publication is idempotent for the same digest. A
different manifest under an existing AgentRef is rejected; publish a new exact
version instead.

## Probes, metrics and audit

The service exposes:

```text
GET /livez             unauthenticated process liveness, no catalog data
GET /readyz            unauthenticated database/schema readiness
GET /v1/health         authenticated, tenant-scoped catalog counts
GET /v1/metrics        operator/auditor/admin, tenant-scoped counters
GET /v1/audit          auditor/admin, bounded cursor pagination
```

Every authenticated catalog, resolution and administration decision is
recorded with request id, principal id, tenant, action, AgentRef, outcome,
status and a fixed detail code. Request bodies, transport targets, credentials
and arbitrary error text are not recorded. SQLite triggers reject updates and
deletes from the audit table.

Useful client commands:

```text
baldr-router agent-manager status
baldr-router agent-manager metrics
baldr-router agent-manager audit --after 0 --limit 100
```

## Upgrade, backup and recovery

The database migrates forward when the service or `doctor` opens it. Current
schema version and durable counts are visible without starting HTTP:

```text
baldr-router agent-manager doctor \
  --registry company --database /var/lib/baldr/agent-manager.sqlite3
```

Before an upgrade, stop writers or use the SQLite online backup command:

```text
baldr-router agent-manager backup \
  --registry company \
  --database /var/lib/baldr/agent-manager.sqlite3 \
  --output /var/backups/baldr/agent-manager-before-0.19.0.sqlite3
```

Backups are created as new private files and never overwrite an existing path.
Test restore by running `doctor` against a copy before changing the production
database. Roll back the application only with a database schema version that
the older release understands; otherwise restore the pre-upgrade backup.

Operational alerts should cover failed readiness, increasing denied/failed
request counts, catalog versions unexpectedly disabled or revoked, and backup
age. Audit retention/export is deployment policy: the bundled store is
append-only and does not silently prune evidence.

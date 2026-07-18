# Builder Protocol v1

Builder Protocol separates the developer-facing tools from language-specific
build implementations. An SDK remains an authoring library; it does not import
or start Builder. The CLI, IDE integration or CI client calls Builder, and
Builder selects a driver for the project's language.

```text
agent source ──imports──> Python / TypeScript / future SDK
     │
     └──> CLI, IDE or CI ──> BuilderClient ──> Builder backend
                                                   │
                         JSONL driver protocol <───┤
                              │                    │
                         Python driver       TypeScript driver
```

The three public v1 schemas are:

- [`builder-protocol-v1.schema.json`](../contracts/builder-protocol-v1.schema.json):
  client-to-Builder service semantics;
- [`builder-driver-v1.schema.json`](../contracts/builder-driver-v1.schema.json):
  Builder-to-language-driver process semantics;
- [`builder-driver-registration-v1.schema.json`](../contracts/builder-driver-registration-v1.schema.json):
  portable discovery metadata for process drivers.

## Current transport

The local backend launches a bounded process for one operation and exchanges
exactly one JSON object per line in each direction. Standard output is reserved
for the response; diagnostics belong on standard error. `describe`, `test` and
`build` identify the driver by exact id, version and SHA-256 digest.

Requests carry a source digest and a workspace locator, not source bytes.
Results carry an artifact digest and a local path, not the artifact itself. A
remote implementation will replace those local locators with CAS URIs while
keeping the semantic messages stable. This prevents JSON, Protobuf or a
database from becoming an inefficient artifact transport.

Every operation has an idempotency key derived from its semantic input. The
local synchronous backend derives a stable job id from that key; a future
durable backend can reuse the same identity for status, event and cancellation
operations. The backend rechecks the workspace digest immediately before the
driver runs, and the driver checks it again before testing or building.

`network = "inherit"` is intentional for the current local driver. It records
that the child inherits the host network; it does not falsely claim sandboxing.
Backends may accept `disabled` or `enabled` only when they can enforce that
policy.

## Why JSON now and Protobuf later

Apache projects do not use one encoding for every layer. Spark Connect sends
language-neutral logical plans over gRPC with Protocol Buffers, while Spark's
monitoring REST API returns JSON. Arrow Flight uses Protobuf/gRPC for commands
and metadata but Arrow IPC for the bulk data stream. The split follows the
responsibility of each channel:

- JSON/JSONL is easy to inspect, fixture-test and implement from a local
  executable in any language;
- Protobuf/gRPC is valuable for a long-lived remote API with generated clients,
  streaming, deadlines and explicit compatibility rules;
- binary artifacts and large source bundles belong in CAS/object storage and
  are referenced by digest.

Official examples: [Spark Connect
overview](https://spark.apache.org/docs/latest/spark-connect-overview.html),
[Spark monitoring REST API](https://spark.apache.org/docs/3.5.7/monitoring.html)
and [Arrow Flight protocol](https://arrow.apache.org/docs/format/Flight.html).

Baldr therefore does not add a speculative `.proto` file in v1. When a remote
Builder service exists, its gRPC API will be a second binding of these proven
operations. Compatibility tests must feed equivalent JSONL and gRPC requests
into the same backend suite and require equivalent results.

## Neutral project configuration

`baldr-agent.toml` schema v2 replaces Python-specific `entry_module` with
`language`, `entrypoint` and an optional exact driver id. Existing schema v1
Python projects remain accepted and build with `baldr.python`; no source
migration is required.

The local registry always exposes the built-in Python driver and discovers
additional drivers from:

1. registration manifests listed in `BALDR_BUILDER_DRIVER_PATHS`;
2. persisted manifests under
   `${XDG_CONFIG_HOME:-~/.config}/baldr-agent/builder-drivers`;
3. executables named `baldr-builder-driver-*` on `PATH`.

Each candidate must answer `describe-request`. Builder then pins its exact
`id + version + digest` in the operation request and rejects missing,
ambiguous or changed implementations. Operators can inspect and persist the
same contract with:

```bash
baldr-agent driver list
baldr-agent driver doctor baldr.typescript
baldr-agent driver conformance baldr.typescript --project /path/to/agent
baldr-agent driver register /path/to/baldr-builder-driver.json
```

`driver conformance` is the neutral compatibility gate for every discovered
driver. Against a real agent project it requires stable `id + version +
digest`, rejects an unsupported protocol version, runs tests, verifies artifact
attestation, compares two byte-identical builds and rejects checkout-path leaks.
During a side-by-side upgrade, `--driver-version` or `--driver-digest` selects
the exact candidate under test.

## Implemented TypeScript slice

The first external driver lives in `tooling/agent-builder-typescript`. It:

1. implements the driver schema over stdin/stdout JSONL;
2. answers `describe-request` with an exact id, version, digest, operations and
   targets;
3. verifies the source digest and declared project identity;
4. implements `test-request` and `build-request`;
5. emits a deterministic, self-contained Node CommonJS artifact and returns its
   SHA-256 without embedding the file in JSON.

The generated project imports `@baldr/agent-sdk`, while Builder and the driver
remain separate processes. `scripts/test_typescript_agent_vertical.py` proves
the complete path: discover, test, reproducible build, immutable install,
catalog publication and a three-stage Baldr workflow ending in review approval.

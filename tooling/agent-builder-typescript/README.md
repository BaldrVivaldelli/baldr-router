# Baldr Agent Builder — TypeScript driver

This package implements `builder-driver-v1` as a bounded stdin/stdout JSONL
process. It tests TypeScript agent projects and emits deterministic,
self-contained `.cjs` artifacts that embed the public `@baldr/agent-sdk`
runtime. The resulting release requires Node.js, but not a project checkout or
`node_modules` directory.

Install the packaged driver globally so Builder discovers its executable from
`PATH` without a checkout registration:

```bash
node_package_dir=/path/to/release/artifacts/node
npm install --global \
  "$node_package_dir/baldr-agent-sdk-0.19.0.tgz" \
  "$node_package_dir/baldr-agent-builder-typescript-0.19.0.tgz"
baldr-agent driver list
```

Once published to npm, use
`npm install --global @baldr/agent-builder-typescript`. The package pins the
exact SDK release and exposes `baldr-builder-driver-typescript`, following
Builder's `baldr-builder-driver-*` discovery convention.

Explicit manifest registration remains useful for private or unpacked drivers:

```bash
baldr-agent driver register ./baldr-builder-driver.json
```

For checkout development, run from the monorepo root:

```bash
npm ci
npm test --workspace @baldr/agent-builder-typescript
BALDR_BUILDER_DRIVER_PATHS="$PWD/tooling/agent-builder-typescript/baldr-builder-driver.json" \
  baldr-agent driver doctor baldr.typescript
```

The driver verifies Builder's neutral source inventory, transpiles declared
TypeScript modules, embeds the public SDK runtime and rejects undeclared
external packages. Tests run against the built artifact path; builds are
deterministic and return `media_type`, launcher, size and SHA-256 evidence.

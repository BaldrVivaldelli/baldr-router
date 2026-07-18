# Release packaging

v0.19.0 separates source, executable artifacts, and validation evidence.

Python artifacts are also split by responsibility: `baldr-router` is the
control plane, `baldr-agent-sdk` is the public authoring API,
`baldr-agent-builder` is the language-aware lifecycle toolchain, and
`baldr-agent-runner` is the independent local data plane. The release gate
builds and installs all four infrastructure wheels together in a clean
environment, while the VSIX continues to embed only the router wheel.

The release also emits independent npm tarballs under `artifacts/node`:

- `baldr-agent-sdk-<version>.tgz`;
- `baldr-agent-builder-typescript-<version>.tgz`.

The release gate installs the Python wheels and both npm tarballs into fresh
temporary prefixes. It requires the packaged driver to be discovered from
`PATH`, verifies that its digest is unchanged by relocation, creates and tests
a TypeScript agent, compares two byte-identical builds, publishes versions
1.0.0 and 1.1.0, rejects replacement of 1.1.0 and rolls back to 1.0.0. Evidence
is written to `dist/validation/typescript-distribution.json`.

The release workflow can publish both packages to npm only through an explicit
`workflow_dispatch` with `publish_npm=true`. The npm organization must first
configure this repository as a trusted publisher for both package names.

```text
dist/baldr-router-0.19.0-source.zip
  source, tests, docs, workflows, contracts; no runtime database or cache

dist/baldr-router-0.19.0-artifacts.zip
  wheels, VSIX, Kiro Power, Agent Plugin, SBOM and provenance
dist/baldr-router-0.19.0-validation-evidence.zip
  portable synthetic build reports only
```

Individual artifacts remain under `dist/artifacts/`. Release metadata lives in
`dist/metadata/` and includes:

```text
SBOM.spdx.json
provenance.intoto.json
secret-scan.json
```

`dist/release-manifest.json` and `dist/SHA256SUMS.txt` cover the split bundles
and individual artifacts. The source bundle must not contain:

```text
SQLite state
validation caches
node_modules
virtual environments
absolute build paths
private evidence from a user machine
```

Build and verify with one cross-platform entrypoint:

```bash
python scripts/dev.py build
python scripts/dev.py verify-release
```

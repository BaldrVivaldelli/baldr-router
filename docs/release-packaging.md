# Release packaging

v0.16.1 separates source, executable artifacts, and validation evidence.

```text
dist/baldr-router-0.16.1-source.zip
  source, tests, docs, workflows, contracts; no runtime database or cache

dist/baldr-router-0.16.1-artifacts.zip
  wheels, VSIX, Kiro Power, Agent Plugin, SBOM and provenance
dist/baldr-router-0.16.1-validation-evidence.zip
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
make build
make verify-release
```

`make build` writes the individual installable artifacts under
`dist/artifacts/`: the core and Kiro adapter wheels/sdists in `python/`, the
VS Code `.vsix`, the Kiro Power ZIP, and the VS Code Agent Plugin ZIP. Use
`make facades` only to regenerate facade files from the shared contract; it
does not produce installable packages. For local MCP development, run
`make mcp` instead of building an artifact.

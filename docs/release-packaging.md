# Release packaging

v0.19.0 separates source, executable artifacts, and validation evidence.

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

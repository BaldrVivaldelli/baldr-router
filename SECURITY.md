# Security policy

Baldr Router executes local providers and can modify trusted Git repositories.
Treat the MCP server, client facades, provider CLIs, and generated artifacts as
privileged developer tooling.

## Supported line

Security and consistency fixes are accepted for the current `0.20.x` release
candidate line.

## Reporting

Report suspected vulnerabilities privately to the project maintainer. Include:

- affected Baldr version and client facade;
- operating system and WSL/remote context;
- minimal reproduction without real secrets;
- relevant evidence or qualification receipt IDs;
- whether writes escaped a trusted workspace or a process remained orphaned.

Never include API keys, provider credentials, private repository content, or
raw prompts in a report.

## Release controls

Release builds must pass:

```text
all Python and Node tests
Ruff and TypeScript checks
facade contract conformance
static secret scan
split-bundle hygiene
SBOM generation
checksum verification
synthetic Lab / Verify
```

A public promotion additionally requires real-environment qualification. Build
CI cannot self-certify a VS Code, Kiro, WSL, login, or real-repository scenario.

GitHub release automation emits build provenance and SBOM attestations when it
runs with the required repository permissions.

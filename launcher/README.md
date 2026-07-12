# Baldr Router Launcher

Optional cross-platform launcher for `baldr-router mcp`.

The launcher is **not** the core runtime. The core is `baldr-router` MCP. This launcher is a small auto-detection shim for clients that need help starting the router, especially this setup:

```text
MCP client runs on Windows
baldr-router is installed inside WSL/Ubuntu
Windows PATH cannot see the Linux baldr-router binary
```

In the default mode, it does not force WSL. It does:

1. Try `baldr-router` on the host PATH.
2. If the host is Windows and step 1 fails, probe the default WSL distro.
3. Then probe listed WSL distros.
4. If it finds the router in WSL, run `wsl.exe ... bash -lc 'exec baldr-router mcp'`.

That means WSL is just an automatic fallback, not a required runtime path.

## Local install from this kit

From Windows PowerShell, macOS, or Linux:

```bash
cd launcher
npm install -g .
```

Then:

```bash
baldr-router-launcher detect
baldr-router-launcher mcp
```

## Modes

```text
BALDR_ROUTER_LAUNCHER_MODE=auto  # default: host first, WSL fallback only when needed
BALDR_ROUTER_LAUNCHER_MODE=host  # host only; never try WSL
BALDR_ROUTER_LAUNCHER_MODE=wsl   # WSL only
```

`BALDR_ROUTER_FORCE_WSL=1` is kept as a backwards-compatible alias for `BALDR_ROUTER_LAUNCHER_MODE=wsl`.

## Useful environment variables

```text
BALDR_ROUTER_WSL_DISTRO=Ubuntu
BALDR_ROUTER_DEBUG=1
```

For local development without installing `baldr-router` as a WSL tool:

```powershell
$env:BALDR_ROUTER_WSL_MCP_COMMAND='cd /home/me/projects/baldr-router/router && exec uv run baldr-router mcp'
baldr-router-launcher mcp
```

Do not print anything to stdout in MCP mode except the child MCP server output. Diagnostics go to stderr.

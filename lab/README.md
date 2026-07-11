# Baldr Lab

This directory contains reproducible validation profiles for Baldr Router.

## Current environment

```bash
./lab/scripts/run-current.sh
```

## Linux container

```bash
./lab/scripts/run-linux-container.sh
```

## Windows Sandbox

Run `lab/windows/New-BaldrSandbox.ps1` from PowerShell to generate a `.wsb` file with the repository mounted read-only. Windows Sandbox validates a clean Windows host bootstrap; Windows + WSL needs a Windows VM or real machine with WSL enabled.

## Matrix

`matrix.json` defines the required real environments. Every mandatory environment must pass the same lifecycle suite three consecutive times from a clean snapshot. Store evidence IDs in `e2e/REAL_ENVIRONMENT_MATRIX.md`.

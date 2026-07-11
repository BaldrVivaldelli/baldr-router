#!/usr/bin/env sh
set -eu
cd "$(dirname "$0")/../.."
exec docker compose -f lab/docker/compose.yaml up --build --abort-on-container-exit --exit-code-from baldr-lab

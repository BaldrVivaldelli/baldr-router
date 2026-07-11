#!/usr/bin/env sh
set -eu
MODE=${BALDR_LAB_MODE:-full}
REPEAT=${BALDR_LAB_REPEAT:-3}
exec baldr-router lab --mode "$MODE" --repeat "$REPEAT" --profile "${BALDR_LAB_PROFILE:-current}"

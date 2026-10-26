#!/bin/sh
set -eu
REPO="$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)"
exec "${SESSIONGRAPH_PYTHON:-python3}" -B "$REPO/entrypoint.py" update --config "${1:?config required}" --chat "${2:?chat required}" --transcript "${3:-}" --event "${SG_HOOK_EVENT:-manual}"

#!/bin/sh
set -eu
ROOT=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
cd "$ROOT"
export UV_CACHE_DIR="$ROOT/var/uv-cache"
export UV_PYTHON_INSTALL_DIR="$ROOT/var/uv-python"
export TMPDIR="$ROOT/var/tmp"
mkdir -p "$TMPDIR"
exec uv run --locked python -m annie_runtime --replace-existing "$@"

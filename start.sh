#!/bin/sh
set -eu
(set -o pipefail) 2>/dev/null && set -o pipefail
ROOT=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
cd "$ROOT"
export UV_CACHE_DIR="$ROOT/var/uv-cache"
export UV_PYTHON_INSTALL_DIR="$ROOT/var/uv-python"
export TMPDIR="$ROOT/var/tmp"
export PYTHONUNBUFFERED=1
mkdir -p "$TMPDIR" "$ROOT/var/logs"
# 控制台输出同时写入 var/logs，终端照常显示
LOG="$ROOT/var/logs/runtime-$(date +%Y%m%d-%H%M%S).log"
echo "控制台日志: $LOG"
uv run --locked python -m annie_runtime --replace-existing "$@" 2>&1 | tee -a "$LOG"

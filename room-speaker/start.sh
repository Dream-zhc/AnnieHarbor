#!/bin/sh
# 独立扬声器。改下面三行即可换房间。A=roomv2，B=roomv3。
# 同一 Room 不要同时开网页调试扬声器。
set -eu
cd "$(dirname "$0")"
export PYTHONUNBUFFERED=1
export PYTHONIOENCODING=utf-8
SPEAKER_HOST="${SPEAKER_HOST:-192.168.1.47}"
SPEAKER_PORT="${SPEAKER_PORT:-8090}"
SPEAKER_ROOM="${SPEAKER_ROOM:-roomv2}"
export SPEAKER_HOST SPEAKER_PORT SPEAKER_ROOM

if command -v python3 >/dev/null 2>&1; then
  PY=python3
elif command -v python >/dev/null 2>&1; then
  PY=python
else
  echo "未找到 Python。地瓜板需要 Python 3.10+ 和 libportaudio2。" >&2
  exit 1
fi

"$PY" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)' || {
  echo "需要 Python 3.10 或更新版本。" >&2
  exit 1
}

if [ ! -x .venv/bin/python ]; then
  if ! "$PY" -m venv .venv; then
    echo "无法创建 .venv，改为使用当前 Python。请先执行: $PY -m pip install -r requirements.txt" >&2
    echo "Linux/地瓜若打不开声卡：sudo apt-get install -y libportaudio2" >&2
    exec "$PY" speaker.py "$@"
  fi
fi

if ! .venv/bin/python -c "import sounddevice, websockets" >/dev/null 2>&1; then
  .venv/bin/python -m pip install -r requirements.txt
fi

exec .venv/bin/python speaker.py "$@"

#!/bin/sh
# 独立扬声器（可选麦克风）。改下面三行即可换房间。A=roomv2，B=roomv3。
# 机器人板上同时开麦克风：ROOM_MIC=1 sh start.sh（或 sh start.sh --mic）。
# 只开麦克风：ROOM_MIC=1 ROOM_SPEAKER=0 sh start.sh。选设备：MIC_DEVICE=名称片段，
# 多声道阵列取第几路：MIC_CHANNEL=0。先用 sh start.sh --list-devices 看设备。
# 一个 Room 只留一个麦克风、一个扬声器：同时开网页麦克风或网页调试扬声器会在大屏提醒。
set -eu
cd "$(dirname "$0")"
# 板子上的固定配置（房间、设备、是否开麦克风）放在同目录 room-audio.env，
# 手动启动和开机自启（install-service.sh）读的是同一份。
if [ -f ./room-audio.env ]; then
  set -a
  . ./room-audio.env
  set +a
fi
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

if ! .venv/bin/python -c "import sounddevice, websockets, numpy" >/dev/null 2>&1; then
  .venv/bin/python -m pip install -r requirements.txt
fi

exec .venv/bin/python speaker.py "$@"

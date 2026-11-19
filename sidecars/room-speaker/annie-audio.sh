#!/bin/sh
# Annie Room 麦克风与扬声器。配置在同目录 room-audio.env。
#
#   sh annie-audio.sh start          后台启动（已在运行就先停旧的再起），约 7 秒返回
#   sh annie-audio.sh stop           停止
#   sh annie-audio.sh status         运行中退出码 0，没运行 3
#   sh annie-audio.sh log [行数]      最近日志
#   sh annie-audio.sh devices        列出声卡，用来填 room-audio.env
#   sudo sh annie-audio.sh install   安装依赖和开机自启（systemd 服务 annie-room-audio）
#   sh annie-audio.sh run            前台运行，异常退出 5 秒后重启（Ctrl+C 退出）
#
# 退出码：0 成功；1 失败（原因打印在输出里）；2 用法错误；3 没运行。
# root 和普通用户都能调，普通用户控制服务需要免密 sudo。可以重复调用，不会起第二份。
# 同一房间两块板时，后启动的那块接管，先启动的自动停止。
set -u
SELF="$(cd "$(dirname "$0")" && pwd)/$(basename "$0")"
DIR="$(dirname "$SELF")"
UNIT="${ANNIE_AUDIO_UNIT:-annie-room-audio}"
LOG="$DIR/logs/room-audio.log"
WAIT_S=6  # 准备依赖 + 打开声卡约 3～5 秒，失败会在这之前退出
CMD="${1:-start}"

# 启停排队执行，避免两个 start 交错；-o：拉起的程序不继承锁。
case "$CMD" in
  start|restart|stop|install)
    if [ -z "${ANNIE_AUDIO_LOCKED:-}" ] && command -v flock >/dev/null 2>&1; then
      export ANNIE_AUDIO_LOCKED=1
      exec flock -o -w 60 "$SELF" /bin/sh "$SELF" "$@"
    fi
    ;;
esac

say() { echo "[annie-audio] $*"; }
fail() { echo "[annie-audio] $*" >&2; exit 1; }

if [ "$(id -u)" -eq 0 ]; then SUDO=""; else SUDO="sudo -n"; fi
OWNER="$(stat -c %U "$DIR" 2>/dev/null || id -un)"

has_unit() {
  command -v systemctl >/dev/null 2>&1 && [ -d /run/systemd/system ] \
    && systemctl cat "$UNIT" >/dev/null 2>&1
}

need_root() {
  if [ -n "$SUDO" ] && ! sudo -n true 2>/dev/null; then
    fail "需要 root 或免密 sudo 才能控制服务 $UNIT"
  fi
}

# root 调用时，程序和依赖仍归目录所有者（例如 sunrise），和开机服务一致。
as_owner() {
  if [ "$(id -u)" -eq 0 ] && [ "$OWNER" != root ]; then
    exec runuser -u "$OWNER" -- /bin/sh "$SELF" "$@"
  fi
}

# 本程序的进程：工作目录是本目录，命令是 python speaker.py 或 sh annie-audio.sh run。
# 按参数逐个比较，命令行里碰巧带这些字的 shell、编辑器、主项目进程都不会被停。
our_pids() {
  for pid in $(pgrep -f 'speaker\.py|annie-audio\.sh' 2>/dev/null); do
    [ "$pid" = "$$" ] && continue
    [ "$(readlink "/proc/$pid/cwd" 2>/dev/null)" = "$DIR" ] || continue
    arg0="$(tr '\0' '\n' < "/proc/$pid/cmdline" 2>/dev/null | sed -n 1p)"
    arg1="$(tr '\0' '\n' < "/proc/$pid/cmdline" 2>/dev/null | sed -n 2p)"
    arg2="$(tr '\0' '\n' < "/proc/$pid/cmdline" 2>/dev/null | sed -n 3p)"
    case "${arg0##*/}" in
      python*)
        if [ "$arg1" = speaker.py ] || [ "$arg1" = "$DIR/speaker.py" ]; then echo "$pid"; fi ;;
      sh|dash|bash)
        if [ "$arg2" = run ] && { [ "$arg1" = "$SELF" ] || [ "$arg1" = annie-audio.sh ] \
            || [ "$arg1" = ./annie-audio.sh ]; }; then echo "$pid"; fi ;;
    esac
  done
}

# 停掉本目录的程序，参数是要保留的 PID（systemd 管着的那个）。
stop_pids() {
  keep="${1:-}"
  pids=""
  for pid in $(our_pids); do
    [ "$pid" = "$keep" ] || pids="$pids $pid"
  done
  [ -n "$pids" ] || return 0
  say "停止旧实例:$pids"
  kill -TERM $pids 2>/dev/null || $SUDO kill -TERM $pids 2>/dev/null
  i=0
  while [ $i -lt 50 ]; do
    alive=""
    for pid in $pids; do kill -0 "$pid" 2>/dev/null && alive="$alive $pid"; done
    [ -n "$alive" ] || return 0
    sleep 0.1
    i=$((i + 1))
  done
  kill -KILL $alive 2>/dev/null || $SUDO kill -KILL $alive 2>/dev/null
  return 0
}

unit_pid() { systemctl show -p MainPID --value "$UNIT" 2>/dev/null || echo 0; }
unit_state() { systemctl is-active "$UNIT" 2>/dev/null; }

show_log() {
  if has_unit; then
    $SUDO journalctl -u "$UNIT" -n "$1" --no-pager -o short-iso 2>/dev/null
  elif [ -f "$LOG" ]; then
    tail -n "$1" "$LOG"
  fi
}

# 本次启动以来的日志：systemd 按时间，后台模式按日志文件原来的长度。
new_log() {
  if has_unit; then
    $SUDO journalctl -u "$UNIT" --since "@$1" --no-pager -o short-iso 2>/dev/null
  elif [ -f "$LOG" ]; then
    tail -c +"$(($1 + 1))" "$LOG"
  fi
}

report_started() {
  say "已启动 pid=$1（$2）"
  output="$(new_log "$3")"
  # 去掉首次等待时附带的设备列表，只留关键几行
  echo "$output" | grep -v -E '[0-9]+[[:space:]]+[0-9]+ Hz|（输出）|（输入）' | tail -n 6
  if echo "$output" | grep -q "等待设备"; then
    say "注意：声卡还没到齐，插好后程序自动开始（sh $SELF status 查看）"
  fi
}

prepare() {
  cd "$DIR" || exit 1
  if [ -f room-audio.env ]; then
    set -a
    . ./room-audio.env
    set +a
  fi
  export PYTHONUNBUFFERED=1 PYTHONIOENCODING=utf-8 PYTHONDONTWRITEBYTECODE=1
  PY=python3
  command -v "$PY" >/dev/null 2>&1 || PY=python
  command -v "$PY" >/dev/null 2>&1 || fail "未找到 Python，需要 Python 3.10 或更新版本"
  "$PY" -c 'import sys; raise SystemExit(sys.version_info < (3, 10))' \
    || fail "需要 Python 3.10 或更新版本"
  if ! { [ -x .venv/bin/python ] && .venv/bin/python -c 'import importlib.util as u, sys
sys.exit(not all(u.find_spec(m) for m in ("sounddevice", "websockets", "numpy")))' 2>/dev/null; }; then
    if ! .venv/bin/python -m pip --version >/dev/null 2>&1; then
      rm -rf .venv
      if ! "$PY" -m venv .venv >/dev/null 2>&1; then
        rm -rf .venv
        fail "无法创建 Python 虚拟环境，先安装：sudo apt-get install -y python3-venv"
      fi
    fi
    say "安装 Python 依赖"
    .venv/bin/python -m pip install -q -r requirements.txt \
      || fail "Python 依赖安装失败，检查能否访问 PyPI"
  fi
  PY=.venv/bin/python
}

# 前台运行并在异常退出后重启；0 = 正常停止，10 = 被后启动的头接管，都不再重启。
supervise() {
  stopping=0
  child=""
  trap 'stopping=1; [ -n "$child" ] && kill -TERM "$child" 2>/dev/null' TERM INT HUP
  while :; do
    "$PY" speaker.py &
    child=$!
    code=0
    wait "$child" || code=$?
    while kill -0 "$child" 2>/dev/null; do wait "$child" || code=$?; done
    child=""
    [ "$stopping" = 1 ] && exit 0
    case "$code" in
      0|10) exit "$code" ;;
    esac
    echo "$(date +%H:%M:%S) 程序退出（$code），5 秒后重新启动"
    sleep 5 &
    wait $! 2>/dev/null
    [ "$stopping" = 1 ] && exit 0
  done
}

start_unit() {
  need_root
  stop_pids "$(unit_pid)"  # 有人在前台手动跑的同目录实例也算旧实例
  since="$(date +%s)"
  $SUDO systemctl reset-failed "$UNIT" >/dev/null 2>&1
  $SUDO systemctl restart "$UNIT" || fail "systemctl restart $UNIT 失败"
  pid="$(unit_pid)"
  sleep "$WAIT_S"
  if [ "$pid" != 0 ] && [ "$(unit_state)" = active ] && [ "$(unit_pid)" = "$pid" ]; then
    report_started "$pid" "开机自启服务 $UNIT" "$since"
    return 0
  fi
  say "启动失败（服务状态 $(unit_state)，会每 5 秒自动重试）。日志："
  new_log "$since" | tail -n 20
  exit 1
}

start_direct() {
  if [ -n "$(our_pids)" ]; then
    stop_pids ""
    sleep 1  # 让 USB 声卡释放完再重新打开
  fi
  mkdir -p "$DIR/logs"
  if [ -f "$LOG" ] && [ "$(wc -c < "$LOG")" -gt 5242880 ]; then mv -f "$LOG" "$LOG.1"; fi
  touch "$LOG"
  size="$(wc -c < "$LOG")"
  # nohup、setsid、setpriv 依次 exec，$! 就是之后的 run 进程。
  if [ "$(id -u)" -eq 0 ] && [ "$OWNER" != root ]; then
    chown "$OWNER" "$DIR/logs" "$LOG"
    HOME="$(getent passwd "$OWNER" | cut -d: -f6)" USER="$OWNER" LOGNAME="$OWNER" \
      nohup setsid setpriv --reuid="$OWNER" --regid="$(id -g "$OWNER")" --init-groups \
      /bin/sh "$SELF" run >>"$LOG" 2>&1 </dev/null &
  else
    nohup setsid /bin/sh "$SELF" run >>"$LOG" 2>&1 </dev/null &
  fi
  pid=$!
  sleep "$WAIT_S"
  if kill -0 "$pid" 2>/dev/null; then
    report_started "$pid" "后台运行，日志 $LOG；没装开机自启" "$size"
    return 0
  fi
  say "启动失败。日志："
  new_log "$size" | tail -n 20
  exit 1
}

install_service() {
  [ "$(id -u)" -eq 0 ] || fail "请用 root 运行：sudo sh $SELF install"
  [ -f "$DIR/room-audio.env" ] || fail "缺少 $DIR/room-audio.env"
  command -v systemctl >/dev/null 2>&1 && [ -d /run/systemd/system ] \
    || fail "本机没有 systemd，直接用 sh $SELF start（不会开机自启）"
  if ! runuser -u "$OWNER" -- /bin/sh "$SELF" devices; then
    say "安装系统依赖 python3-venv libportaudio2"
    apt-get install -y python3-venv libportaudio2 || fail "系统依赖安装失败"
    runuser -u "$OWNER" -- /bin/sh "$SELF" devices || fail "依赖仍不完整，见上面的错误"
  fi
  cat > "/etc/systemd/system/$UNIT.service" <<SERVICE
[Unit]
Description=Annie Room 扬声器与麦克风
After=network-online.target sound.target
Wants=network-online.target

[Service]
Type=simple
User=$OWNER
SupplementaryGroups=audio
WorkingDirectory=$DIR
ExecStart=/bin/sh $SELF service
Restart=always
RestartSec=5
# 10 = 同一房间里后启动的机器人头接管了本机：算正常停止，不自动重启抢回
SuccessExitStatus=10
RestartPreventExitStatus=10

[Install]
WantedBy=multi-user.target
SERVICE
  systemctl daemon-reload
  systemctl enable "$UNIT" >/dev/null 2>&1 || fail "systemctl enable $UNIT 失败"
  say "已安装开机自启服务 $UNIT（运行用户 $OWNER）"
  start_unit
}

case "$CMD" in
  start|restart)
    [ -f "$DIR/room-audio.env" ] || fail "缺少 $DIR/room-audio.env"
    if has_unit; then start_unit; else start_direct; fi
    ;;
  stop)
    if has_unit; then
      need_root
      $SUDO systemctl stop "$UNIT" || fail "systemctl stop $UNIT 失败"
    fi
    stop_pids ""
    say "已停止"
    ;;
  status)
    if has_unit && [ "$(unit_state)" = active ]; then
      say "运行中 pid=$(unit_pid)（开机自启服务 $UNIT）"
    elif pids="$(our_pids | tr '\n' ' ')" && [ -n "$pids" ]; then
      say "运行中 pid=${pids% }"
    else
      say "没有运行"
      show_log 5
      exit 3
    fi
    # 最近一条设备相关日志是“等待设备”说明声卡还没到齐
    if show_log 200 | grep -E "等待设备|扬声器 .* Hz，|麦克风 .* Hz × " | tail -n 1 | grep -q "等待设备"; then
      say "注意：还在等声卡，插好后自动开始"
    fi
    show_log 5
    ;;
  log)
    show_log "${2:-50}"
    ;;
  devices)
    as_owner devices
    prepare
    exec "$PY" speaker.py --list-devices
    ;;
  install)
    install_service
    ;;
  run)
    as_owner run
    prepare
    supervise
    ;;
  service)
    # 由 systemd 调用，重启交给 systemd。
    prepare
    exec "$PY" speaker.py
    ;;
  *)
    echo "用法: sh $SELF [start|stop|status|log [行数]|devices|install|run]" >&2
    exit 2
    ;;
esac

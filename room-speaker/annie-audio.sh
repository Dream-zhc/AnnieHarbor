#!/bin/sh
# 给机器人主项目调用的启动脚本：在本板拉起 Room 麦克风 + 扬声器（同目录 speaker.py，配置读 room-audio.env）。
#
#   sh /home/sunrise/annie-room-audio/annie-audio.sh start     # 默认。先停本程序旧实例再后台拉起，约 7 秒返回
#   sh /home/sunrise/annie-room-audio/annie-audio.sh stop      # 只停本程序，不碰主项目和其它进程
#   sh /home/sunrise/annie-room-audio/annie-audio.sh status    # 在运行退出码 0，没运行 3
#   sh /home/sunrise/annie-room-audio/annie-audio.sh log 50    # 最近 50 行日志
#
# 退出码：0 成功；1 失败（原因和最近日志打印在输出里）；2 用法错误；3 没运行（status）。
# root 和普通用户都能调；普通用户需要免密 sudo（板子默认就是）。脚本可以重复调用，不会起第二份。
# 同一 Room 有两块板时，后 start 的那块接管，先开的那块自动停止（head.py）。
# 装了 systemd 服务 annie-room-audio（install-service.sh）就交给它管：异常退出 5 秒自动重启，
# 被接管后保持停止；没装就直接后台运行，日志在 logs/room-audio.log，不会自动重启。
set -u
SELF="$(cd "$(dirname "$0")" && pwd)/$(basename "$0")"
DIR="$(dirname "$SELF")"
UNIT="${ANNIE_AUDIO_UNIT:-annie-room-audio}"
LOG="$DIR/logs/room-audio.log"
WAIT_S=6  # 依赖检查 + 打开声卡约 3～5 秒，失败会在这之前退出

# 同时调用多次时排队执行，避免两个 start 交错。
if [ -z "${ANNIE_AUDIO_LOCKED:-}" ] && command -v flock >/dev/null 2>&1; then
  export ANNIE_AUDIO_LOCKED=1
  exec flock -o -w 60 "$SELF" /bin/sh "$SELF" "$@"  # -o：拉起的程序不继承锁
fi

say() { echo "[annie-audio] $*"; }
fail() { echo "[annie-audio] $*" >&2; exit 1; }

if [ "$(id -u)" -eq 0 ]; then SUDO=""; else SUDO="sudo -n"; fi

has_unit() {
  command -v systemctl >/dev/null 2>&1 && [ -d /run/systemd/system ] \
    && systemctl cat "$UNIT" >/dev/null 2>&1
}

need_root() {
  if [ -n "$SUDO" ] && ! sudo -n true 2>/dev/null; then
    fail "需要 root 或免密 sudo 才能控制 systemd 服务 $UNIT"
  fi
}

# 本程序的进程：工作目录是本目录，且命令是 python speaker.py 或启动阶段的 sh start.sh。
# 只看前两个参数，命令行里碰巧带这些字的 shell、编辑器等不会被误杀。
our_pids() {
  for pid in $(pgrep -f 'speaker\.py|start\.sh' 2>/dev/null); do
    [ "$(readlink "/proc/$pid/cwd" 2>/dev/null)" = "$DIR" ] || continue
    args="$(tr '\0' '\n' < "/proc/$pid/cmdline" 2>/dev/null | head -n 2 | tr '\n' ' ')"
    case "$args" in
      *python*" speaker.py "|*python*" $DIR/speaker.py ") echo "$pid" ;;
      *sh" start.sh "|*sh" ./start.sh "|*sh" $DIR/start.sh ") echo "$pid" ;;
    esac
  done
}

# 停掉本目录的 speaker.py，参数是要保留的 PID（systemd 自己管的那个）。
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

start_unit() {
  need_root
  stop_pids "$(unit_pid)"  # 有人在前台手动跑的同目录实例也算旧实例
  $SUDO systemctl reset-failed "$UNIT" >/dev/null 2>&1
  $SUDO systemctl restart "$UNIT" || fail "systemctl restart $UNIT 失败"
  pid="$(unit_pid)"
  sleep "$WAIT_S"
  if [ "$pid" != 0 ] && [ "$(unit_state)" = active ] && [ "$(unit_pid)" = "$pid" ]; then
    say "已启动 pid=$pid（systemd 服务 $UNIT）"
    show_log 6
    return 0
  fi
  say "启动失败（服务 $UNIT 状态：$(unit_state)；systemd 会每 5 秒自动重试）。最近日志："
  show_log 20
  exit 1
}

start_direct() {
  if [ -n "$(our_pids)" ]; then
    stop_pids ""
    sleep 1  # 让 USB 声卡释放完再重新打开
  fi
  mkdir -p "$DIR/logs"
  if [ -f "$LOG" ] && [ "$(wc -c < "$LOG")" -gt 5242880 ]; then mv -f "$LOG" "$LOG.1"; fi
  owner="$(stat -c %U "$DIR")"
  # 主项目以 root 调用时，仍以目录所有者（sunrise）身份运行，和开机服务一致。
  # nohup、setsid、setpriv、start.sh 依次 exec，$! 就是之后的 python 进程。
  if [ "$(id -u)" -eq 0 ] && [ "$owner" != root ]; then
    chown "$owner" "$DIR/logs" "$LOG" 2>/dev/null
    HOME="$(getent passwd "$owner" | cut -d: -f6)" USER="$owner" LOGNAME="$owner" \
      nohup setsid setpriv --reuid="$owner" --regid="$(id -g "$owner")" --init-groups \
      /bin/sh "$DIR/start.sh" >>"$LOG" 2>&1 </dev/null &
  else
    nohup setsid /bin/sh "$DIR/start.sh" >>"$LOG" 2>&1 </dev/null &
  fi
  pid=$!
  sleep "$WAIT_S"
  if kill -0 "$pid" 2>/dev/null; then
    say "已启动 pid=$pid（后台运行；未装 systemd 服务，异常退出不会自动重启）"
    show_log 6
    return 0
  fi
  say "启动失败。最近日志（$LOG）："
  show_log 20
  exit 1
}

cmd="${1:-start}"
case "$cmd" in
  start|restart)
    [ -f "$DIR/room-audio.env" ] || fail "缺少 $DIR/room-audio.env（从 room-audio.env.example 复制后修改）"
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
      say "运行中 pid=$(unit_pid)（systemd 服务 $UNIT）"
    elif pids="$(our_pids | tr '\n' ' ')" && [ -n "$pids" ]; then
      say "运行中 pid=${pids% }"
    else
      say "没有运行"
      show_log 5
      exit 3
    fi
    show_log 5
    ;;
  log)
    show_log "${2:-50}"
    ;;
  *)
    echo "用法: sh $SELF [start|stop|restart|status|log [行数]]" >&2
    exit 2
    ;;
esac

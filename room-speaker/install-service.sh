#!/bin/sh
# 开机自动运行：安装 systemd 服务 annie-room-audio，以当前普通用户身份运行 start.sh。
# 用法（需要 root）：sudo sh install-service.sh
# 停用：sudo systemctl disable --now annie-room-audio
set -eu
DIR="$(cd "$(dirname "$0")" && pwd)"
RUN_USER="${SUDO_USER:-$(id -un)}"
if [ "$(id -u)" -ne 0 ]; then
  echo "请用 sudo 运行：sudo sh $DIR/install-service.sh" >&2
  exit 1
fi
if [ "$RUN_USER" = "root" ]; then
  echo "请用普通用户执行 sudo，服务会以该用户身份运行。" >&2
  exit 1
fi
if [ ! -f "$DIR/room-audio.env" ]; then
  echo "缺少 $DIR/room-audio.env，先从 room-audio.env.example 复制并修改。" >&2
  exit 1
fi
cat > /etc/systemd/system/annie-room-audio.service <<UNIT
[Unit]
Description=Annie Room 扬声器与麦克风
After=network-online.target sound.target
Wants=network-online.target

[Service]
Type=simple
User=$RUN_USER
SupplementaryGroups=audio
WorkingDirectory=$DIR
ExecStart=/bin/sh $DIR/start.sh
Restart=always
RestartSec=5
# 10 = 同一 Room 里后启动的机器人头接管了本机（head.py）：算正常停止，不自动重启抢回
SuccessExitStatus=10
RestartPreventExitStatus=10
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
UNIT
systemctl daemon-reload
systemctl enable --now annie-room-audio.service
echo "已安装并启动。状态：systemctl status annie-room-audio；日志：journalctl -u annie-room-audio -f"

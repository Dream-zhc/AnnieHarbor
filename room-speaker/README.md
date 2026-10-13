# Room 扬声器

订阅本机器人 Room 的 `reb_tts`，把 24 kHz PCM 交给声卡播放，再把声卡回调实际消耗的采样数以 `source=speaker` 报到 `reb_voice`。

云端 AEC 仍在 Brain 里。这个程序不合成语音，也不消回声。Brain 只有收到这里的实体播放回执，才会进入 `reference_aligned`。

同一 Room 同时只能有一路喇叭。运行本程序时，关掉工作台的「网页调试扬声器」。

## Windows

双击或在命令行运行 `start.bat`。脚本会自建 `.venv` 并安装依赖。列出声卡：

```bat
start.bat --list-devices
start.bat --device 扬声器名称中的一段
```

## Linux / 地瓜

需要 Python 3.10+ 和 PortAudio：

```sh
sudo apt-get install -y libportaudio2
sh start.sh
sh start.sh --list-devices
```

## 房间

默认连接 `ws://192.168.1.47:8090/robot/roomv2`。A 用 `roomv2`，B 用 `roomv3`。改 `start.bat` / `start.sh` 里的这三项：

```text
SPEAKER_HOST=192.168.1.47
SPEAKER_PORT=8090
SPEAKER_ROOM=roomv2
```

也可以直接传 `--host`、`--port`、`--room`，或完整的 `--tts-url` 与 `--voice-url`。

播放中大约每 100 ms 上报一次 `tts.playback.progress`。这个间隔要留在 850 ms 以内，云端 AEC 才会保持参考。暂停、停止和播完仍按实际游标上报。网络断开不会补发 `stopped`。

连接是直连，不读取 Windows 系统代理。若本机开了 Clash / V2Ray 的系统代理，旧版本会把 `192.168.*` 送进代理，表现为 `timed out during opening handshake`。

确认 Brain 已接上：`GET /api/robots/annie-a/inputs` 里 `aec.receipts` 增加，播放期间 `aec.status` 为 `reference_aligned`，`aec.processed` 增加。这只说明参考时钟对上了，不代表声学消除已经验收。

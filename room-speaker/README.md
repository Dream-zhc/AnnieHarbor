# Room 扬声器与麦克风

一个程序，两种用法：

- **扬声器**（默认，原有用法不变）：订阅本机器人 Room 的 `reb_tts`，把 24 kHz PCM 交给声卡播放，再把声卡回调实际消耗的采样数以 `source=speaker` 报到 `reb_voice`。
- **麦克风**（`--mic`）：采集本机麦克风，转成 16 kHz 单声道 S16LE、20 ms 一帧，以 `MIC_AUDIO` 发到 `reb_mic`，和网页麦克风发的完全一样；采集状态以 `source=microphone` 报到 `reb_voice`。

机器人 Linux 板上两者一起开。云端 AEC、唤醒和对话都在 Brain 里，这个程序不合成语音、不消回声、不做 VAD。Brain 只有收到这里的实体播放回执，才会进入 `reference_aligned`。

**一个 Room 同时只能有一个麦克风、一个扬声器。** 运行本程序时，关掉同一 Room 的网页麦克风和「网页调试扬声器」；多开时工作台和大屏会显示红色提醒，但不会被拦截。

## Linux / 地瓜 / 机器人板

板上只放运行需要的文件（不放本 README、开发记录或 Brain 相关资料，这些板子后续可能交给工厂外部人员）：

```text
annie-audio.sh   唯一的脚本：启动、停止、状态、日志、列声卡、安装开机自启
speaker.py  microphone.py  head.py  requirements.txt
room-audio.env   本机配置（新板从 room-audio.env.example 复制后修改）
```

运行时会自动生成 `.venv/`（Python 依赖）、`.speaker-volume`（远程调整后的音量）；没装开机自启时还有 `logs/`。

```sh
sh annie-audio.sh devices          # 列出声卡，填进 room-audio.env
sudo sh annie-audio.sh install     # 新板：装依赖（含 python3-venv、libportaudio2）和开机自启服务 annie-room-audio，并启动
sh annie-audio.sh start            # 启动；已在运行就先停旧的再起，约 7 秒返回
sh annie-audio.sh stop             # 停止（不改开机自启）
sh annie-audio.sh status           # 退出码 0=运行中，3=没运行
sh annie-audio.sh log 50           # 最近 50 行日志（服务模式即 journalctl -u annie-room-audio）
sh annie-audio.sh run              # 前台试跑，Ctrl+C 退出
```

- root 和普通用户调都行；普通用户控制服务需要免密 sudo（板子默认就是）。root 调用时程序仍以目录所有者（`sunrise`）运行。
- 可以重复调、同时调，板上始终只有一份。只认工作目录是本目录的 `python speaker.py` 和 `sh annie-audio.sh run`，不会动主项目或其它进程。
- 装了服务就交给 systemd（`ExecStart=… annie-audio.sh service`），异常退出 5 秒后重启；没装就后台运行并由脚本自己 5 秒后重启，日志在 `logs/room-audio.log`。
- 服务以目录所有者运行（加入 `audio` 组）。建议直接用声卡硬件名（`devices` 里带 `hw:` 的那几项），不经过 PulseAudio，扬声器回执才贴近声卡实际播放。
- 只想由主项目控制、不要开机自启：`sudo systemctl disable annie-room-audio`，脚本照样能 start/stop。

### 声卡缺失与掉线

- **启动时声卡不在**（没插、名字匹配不到或被占用）：不退出，日志记一次“等待设备”和现有设备列表，之后每 5 秒重新扫描、每分钟提示一次，插好后自动开始。等待期间不连 Room、不报机器人头，所以不会把房间从另一块正常工作的板子上抢走；设备到齐才开始计“启动时间”。
- **运行中掉线**（扬声器 5 秒没有回调、麦克风 5 秒没有数据）：先发停止回执（`reason=device_lost`），以退出码 75 退出，由 systemd 或脚本重新启动、重新枚举声卡。原来在进程内重开 USB 声卡时会在 PortAudio 里断言崩溃（188，2026-10-21），所以不再进程内重开。
- 麦克风精确全零（USB 麦克风自带降噪/自动增益，安静时会这样）只在持续 60 秒时记一条日志，不算掉线。

### 给机器人主项目调用

两块板都在 `/home/sunrise/annie-room-audio/annie-audio.sh`，主项目启动时调一次 `start`：

```python
import subprocess

result = subprocess.run(["/bin/sh", "/home/sunrise/annie-room-audio/annie-audio.sh", "start"],
                        capture_output=True, text=True, timeout=90)
log.info("Annie 麦克风扬声器 start 退出码=%s\n%s%s", result.returncode, result.stdout, result.stderr)
```

不想等结果就用 `subprocess.Popen([...], start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)`。主项目自己不要再打开同一块 USB 麦克风或扬声器：ALSA 硬件设备独占，后打开的那个会失败。

### 两个机器人头共用一个 Room

大头机器人（`192.168.1.188`）和 obj 娃娃小机器人（`192.168.1.143`）都接 `roomv2`，平时只开一个。各自在 `room-audio.env` 里写上自己是哪个头：

```sh
ROBOT_HEAD=big_head
ROBOT_HEAD_NAME=大头机器人
```

万一两块板都开了，**后启动的接管**：先启动的那块听到对方后停止播放和采集，发出 `robot.head.released`，然后以退出码 10 退出，systemd 把它记为正常停止、不会再拉起来（`SuccessExitStatus=10`、`RestartPreventExitStatus=10`）。要让它重新接管，在它上面执行 `sh annie-audio.sh start`，或者给它断电重启。谁先谁后按各自进程的运行时长比较，与两块板的系统时间是否准确无关。

两块板每 2 秒在 `reb_voice` 发一次 `robot.head.active`（头名、板子地址、麦克风和扬声器）。工作台会显示当前用的是哪个头，大屏顶部状态栏显示“当前：大头机器人（192.168.1.188）”；接管后 10 分钟内两处都会提示是谁接管了谁。其他监听 Room 的前端也可以直接订阅这条消息。没设置 `ROBOT_HEAD` 的程序（例如 Windows 上临时开的扬声器）不参与接管，多开时照旧显示红色冲突提醒。

### 用说话调音量

对小安妮说“大声点 / 小声点 / 音量调到一半”，Brain 会发 `speaker.volume.set`，本程序按 0–100 调软件音量（100 = 声卡原来的响度，只能往小调或调回来），并保存在 `.speaker-volume`，重启后沿用。声卡本身的硬件音量不改；要整体更响，用 `alsamixer` 或 `amixer -c <声卡> sset PCM 100%` 调硬件音量。

注意硬件增益超过 0 dB 的声卡：大头板（192.168.1.188）的 DOV 声卡 `PCM` 范围是 0～+3.9 dB，100% 时会把接近满幅的语音放大 3.9 dB 而削顶，听起来是破音、杂音。这种声卡把软件音量设在 80（≈-3.9 dB，抵消后正好 0 dB）：对小安妮说“音量调到80”，或写 `.speaker-volume` 后重启。2026-10-21 已在 188 上设为 80。143 的 CD002 在 100% 时是 -0.94 dB，不受影响。

## Windows

双击或在命令行运行 `start.bat`。脚本会自建 `.venv` 并安装依赖。

```bat
start.bat --list-devices
start.bat --device 扬声器名称中的一段
start.bat --mic --mic-device 麦克风名称中的一段
```

## 房间

默认连接 `ws://192.168.1.47:8090/robot/roomv2`。A 用 `roomv2`，B 用 `roomv3`。板上改 `room-audio.env`，Windows 改 `start.bat` 里的这三项：

```text
SPEAKER_HOST=192.168.1.47
SPEAKER_PORT=8090
SPEAKER_ROOM=roomv2
```

也可以直接传 `--host`、`--port`、`--room`，或完整的 `--tts-url`、`--voice-url`、`--mic-url`。

## 参数

| 参数 / 环境变量 | 说明 |
| --- | --- |
| `--device` / `SPEAKER_DEVICE` | 扬声器设备编号或名称片段 |
| `--mic` / `ROOM_MIC=1` | 同时开麦克风 |
| `--no-speaker` / `ROOM_SPEAKER=0` | 只开麦克风（须同时开 `--mic`） |
| `--mic-device` / `MIC_DEVICE` | 麦克风设备编号或名称片段 |
| `--mic-channel` / `MIC_CHANNEL` | 多声道麦克风阵列取第几路，从 0 开始，默认 0 |
| `--head` / `ROBOT_HEAD` | 机器人头标识（字母、数字、下划线），设置后参与“后启动的接管” |
| `--head-name` / `ROBOT_HEAD_NAME` | 工作台和大屏显示的头名，如 `大头机器人` |
| `--prebuffer-ms`、`--progress-ms` | 扬声器预缓冲、进度回执间隔（原有） |
| `--volume` / `SPEAKER_VOLUME` | 首次启动的音量 0–100，默认 100（= 声卡原来的响度）；之后用对话调的音量保存在同目录 `.speaker-volume` |

麦克风先按 16 kHz 单声道打开；声卡不支持时按它自己的采样率打开，低通后重采样到 16 kHz。网络慢或断线时最多缓存 500 ms，超过的音频直接丢掉、不补发（Brain 本来也会丢）。

## 回执

扬声器播放中大约每 100 ms 上报一次 `tts.playback.progress`。这个间隔要留在 850 ms 以内，云端 AEC 才会保持参考。暂停、停止和播完仍按实际游标上报。网络断开不会补发 `stopped`。另外每 2 秒发一次 `speaker.presence`，让 Brain 知道空闲的扬声器也在线。

麦克风发 `mic.capture.started`，之后每秒一次 `mic.capture.progress`（已采样数、已发帧、丢帧、声卡溢出、近 1 秒峰值 dBFS、reb_mic 是否连上），正常退出时发 `mic.capture.stopped`。

这些消息带本进程的 `instance_id`、主机名和声卡名，只用于显示和数同一 Room 里有几个设备，不是用户身份。

## 排查

连接是直连，不读取系统代理。若开了 Clash / V2Ray 的系统代理，旧版本会把 `192.168.*` 送进代理，表现为 `timed out during opening handshake`。

确认 Brain 已接上：`GET /api/robots/annie-a/inputs` 里

- `accepted` 持续增加：麦克风音频到了；
- `audio_devices.microphones` / `speakers` 各有一项：回执到了，`status` 为 `ok`；
- `aec.receipts` 增加，播放期间 `aec.status` 为 `reference_aligned`。这只说明参考时钟对上了，不代表声学消除已经验收。

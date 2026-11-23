# 语种 / 方言识别旁路（Windows 外挂）

和 `tts_viseme` 嘴型服务一样，独立跑在那台 Windows（RTX 4060）上：听各机器人 Room 的麦克风，用户每说完一句，判断是哪种语言、哪种方言，把结果发回同一个 Room 的 `reb_sam`。主脑订阅 `reb_sam`，工作台顶部显示“语种 · 粤语 99%”。只观察，不改音频、不往麦克风写东西。

## 用法

1. 把整个 `annie-dialect-lid` 文件夹拷到 Windows 上（路径里最好没有空格）。
2. 需要 Python 3.11（tts_viseme 用的同一个就行）和 NVIDIA 显卡驱动。
3. 双击 `start_win.bat`。第一次会自动：
   - 在本文件夹建 `.venv`，装 CUDA 版 PyTorch（约 2.5 GB）和几个小依赖；先走阿里云镜像，失败再走官方源；
   - 下载 FireRedLID 模型（约 3.5 GB）：先走 hf-mirror.com 镜像，失败再走 Hugging Face；中途断了再双击会续传。
   - 推理代码已经随文件夹带上（`vendor/fireredlid`，Apache-2.0，见 `vendor/README.md`），不用访问 GitHub。
4. 之后再双击就直接启动。窗口里每识别一句打一行，日志也写在 `logs/lid.log`（见下面“日志怎么看”）。
5. 同一台电脑只允许一个实例（占本机端口 37661）。
6. **更新旧版**：只把新的 `lid_service.py` 覆盖进去（`config.json` 没改过也一起覆盖），`.venv`、`models` 不用动，再双击 `start_win.bat`。

## 配置 `config.json`

| 字段 | 说明 |
| --- | --- |
| `room_root` | Room 服务器，默认 `ws://192.168.1.47:8090/robot`；也可用环境变量 `LID_ROOM_ROOT` 覆盖。整个配置文件也可用环境变量 `LID_CONFIG` 指向别的文件 |
| `robots` | 机器人和 Room 一一对应：annie-a→roomv2、annie-b→roomv3；annie-small→roomv4 先关着，`enabled` 改成 `true` 即可。`annie-test`→`lidtest` 是自测用的私有房间，见下面“不碰真机的自测” |
| `mic_channel` / `publish_channel` / `tts_channel` | 听 `reb_mic`，结果发 `reb_sam`，同时只读订阅 `reb_tts`（判断哪些声音是机器人自己的） |
| `use_half` | 显卡上用半精度（显存约 1.8 GB；整精度约 3.5 GB） |
| `queue_max` | 排队上限，满了丢最旧的一句 |
| `vad` | 切句：连续 120 ms 有声算开口，静 600 ms 算说完；短于 1 秒不判断；超过 12 秒先判断一段再继续听；`margin_db` / `min_db` 是比底噪高多少、最低音量（门槛 = max(`min_db`, 底噪 + `margin_db`)） |

## 它听到的麦克风长什么样

Room 里每条麦克风消息是一个带信封的二进制帧：2 字节大端的类型长度（9）+ 文字 `MIC_AUDIO` + 640 字节 PCM（16 kHz 单声道 S16LE，20 ms）。和主脑 `annie_adapters.decode_room_binary_frame` 完全一致；服务先拆信封再切句，别的类型、残缺的帧直接丢掉。（2026-11-23 之前的版本没拆信封，机器人一开麦克风就会把信封当声音。）

## 机器人自己的声音（回声）

机器人的麦克风也会收到她自己扬声器里的声音，不能当成用户去判断。服务同时只读订阅同一个 Room 的 `reb_tts`，按主脑发出的每个 `tts` 包算出她“正在出声”的时间段（音频是一段接一段按真实速度播的；前留 0.15 秒、后留 0.6 秒余量；收到 `stop` 包就立刻结束）。一句话有一半以上落在这个时间里，就当作回声，不送模型，日志写 `skipped ... reason=robot_speaking overlap=NN%`，心跳里的 `echo_skipped` 加一。用户在她说完后很快接话（1 秒内）、或只在她最后一秒才开口，都不受影响。门槛在 `lid_service.py` 里：`ECHO_SHARE`、`Playing.BEFORE`、`Playing.AFTER`。

## 发到 `reb_sam` 的消息

每句一条：

```json
{"type":"language_id","room":"roomv2","robot_id":"annie-a","seq":12,
 "label":"zh yue","lang":"zh","dialect":"yue","name":"粤语","confidence":0.999,
 "duration_s":3.12,"latency_ms":420,"ended_at_ms":1795171638220,"model":"FireRedLID"}
```

每 2 秒一条心跳（每个 Room 各一条）：

```json
{"type":"language_id_health","room":"roomv2","robot_id":"annie-a","status":"ready",
 "reason":null,"mic_connected":true,"mic_fps":49.9,"device":"cuda","queue":0,
 "processed":35,"dropped":0,"echo_skipped":2,"model":"FireRedLID"}
```

`status`：`loading` 加载模型、`ready` 正常、`error` 出错（`reason` 写原因）。主脑 10 秒收不到心跳就显示“离线”。`mic_fps` 是这两秒里每秒收到的麦克风消息数，正常约 50；接近 0 说明机器人的麦克风没有往 Room 送声音。

`label` 可能的方言：`zh mandarin` 普通话、`zh yue` 粤语、`zh wu` 吴语、`zh min` 闽语、`zh north` 北方官话（山东/河北/山西/陕西/辽宁等）、`zh xinan` 西南官话（四川/重庆/云南/贵州/湖北）、`zh xiang` 湘语；其他语言是 `en`、`ja`、`ko` 等 100 多种。

Room 会给每条消息加上 `from`、`ts`，并去掉值为 null 的字段。

## 日志怎么看

- `language robot=annie-a room=roomv2 label=zh yue conf=0.999 dur=3.1s infer_ms=240 wait_ms=0`：每识别一句一行；`infer_ms` 是推理用的时间，`wait_ms` 是排队等的时间。
- `skipped robot=... reason=robot_speaking overlap=100%`：这句话是她自己的声音，没判断。
- 每分钟一行 `stats ... mic_fps=50.0 vad_floor=-52dB processed=35 echo_skipped=2 dropped=0`：`mic_fps` 约 50 表示机器人的麦克风在送声音；`vad_floor` 是服务估计的环境底噪，切句门槛是它加 12 dB（但不低于 -45 dB）。真机上说话没被切出来时，把这几行发给我，我按实际音量调门槛。

## 不碰真机的自测

Room 服务器允许任意房间名，互相隔离。`config.json` 里的 `annie-test`→`lidtest` 是一个里面没有机器人、大脑和扬声器的私有房间，默认关着。要测 4060 的速度和精度时：把它的 `enabled` 改成 `true`，重启服务；然后从别的电脑往 `ws://192.168.1.47:8090/robot/lidtest/reb_mic` 按 20 ms 一帧发带信封的测试声音，在 `lidtest/reb_sam` 收结果。完全不会碰 roomv2 / roomv3。测完改回 `false`。

## 能不能同时听两台机器人、并发多少

- 每台机器人一条麦克风连接，各自切句，互不串：结果只发回它自己的 Room，并带 `robot_id`。
- 推理只有一个工作线程，按顺序一句一句判断，不会两句同时抢显卡。
- 速度（实测，2～3 秒的话）：RTX 4060（半精度）服务里排队加推理中位 41 ms、最大 46 ms；同一批在 Mac M4 只用 CPU（fp32）上中位 0.85 秒。单线程每秒能判断二十多句，两台机器人同时说话最多让后一句多等约 40 ms。每句的实际用时在日志 `language robot=...` 的 `infer_ms`、`wait_ms` 里。
- 显存：半精度约 1.8 GB，和 tts_viseme 一起放在 8 GB 的 4060 上够用（截图里已用 4.8 GB）。

## 结果什么时候用

用户说完一句后约 0.65 秒出结果（0.6 秒是等她说完，推理约 0.04 秒；实测最后一个有声帧到结果送达中位 0.63 秒、最大 0.72 秒），所以只影响下一轮：主脑先在工作台显示和写日志（`language_id room=... label=...`）；要不要让她据此跟着说方言，由 `config/runtime_defaults.toml` 的 `[dialect_follow]` 开关决定，默认关，等真人方言录音测完准确率再定。

## 出错了怎么办

- 窗口里有提示就按提示处理，修好后再双击 `start_win.bat`，已装好的部分不会重装。
- 提示 `.venv` 不是 Python 3.11，或者反复装不上：删掉本文件夹里的 `.venv` 再双击。
- 模型下载失败：检查网络后再双击，会接着下。
- 工作台“语种”显示“没收到麦克风声音”：服务在线，但机器人的麦克风没有往 Room 送声音（`mic_fps` 接近 0），请检查机器人板子上的麦克风程序。

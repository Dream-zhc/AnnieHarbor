# 语种 / 方言识别旁路（Windows 外挂）

和 `tts_viseme` 嘴型服务一样，独立跑在那台 Windows（RTX 4060）上：听各机器人 Room 的麦克风，用户每说完一句，判断是哪种语言、哪种方言，把结果发回同一个 Room 的 `reb_sam`。主脑订阅 `reb_sam`，工作台顶部显示“语种 · 粤语 99%”。只观察，不改音频、不往麦克风写东西。

## 用法

1. 把整个 `annie-dialect-lid` 文件夹拷到 Windows 上（路径里最好没有空格）。
2. 需要 Python 3.11（tts_viseme 用的同一个就行）和 NVIDIA 显卡驱动。
3. 双击 `start_win.bat`。第一次会自动：
   - 在本文件夹建 `.venv`，装 CUDA 版 PyTorch（约 2.5 GB）和几个小依赖；官方源失败自动换阿里云镜像；
   - 下载 FireRedASR2S 推理代码（GitHub，Apache-2.0）；
   - 下载 FireRedLID 模型（约 3.5 GB，Hugging Face，失败自动换 hf-mirror.com）。
4. 之后再双击就直接启动。窗口里每识别一句打一行 `language robot=annie-a room=roomv2 label=zh yue conf=0.999 ...`，日志也写在 `logs/lid.log`。
5. 同一台电脑只允许一个实例（占本机端口 37661）。

## 配置 `config.json`

| 字段 | 说明 |
| --- | --- |
| `room_root` | Room 服务器，默认 `ws://192.168.1.47:8090/robot`；也可用环境变量 `LID_ROOM_ROOT` 覆盖 |
| `robots` | 机器人和 Room 一一对应：annie-a→roomv2、annie-b→roomv3；annie-small→roomv4 先关着，`enabled` 改成 `true` 即可 |
| `mic_channel` / `publish_channel` | 听 `reb_mic`，结果发 `reb_sam` |
| `use_half` | 显卡上用半精度（显存约 1.8 GB；整精度约 3.5 GB） |
| `queue_max` | 排队上限，满了丢最旧的一句 |
| `vad` | 切句：连续 120 ms 有声算开口，静 600 ms 算说完；短于 1 秒不判断；超过 12 秒先判断一段再继续听；`margin_db` / `min_db` 是比底噪高多少、最低音量 |

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
 "reason":null,"mic_connected":true,"device":"cuda","queue":0,"processed":35,"dropped":0,"model":"FireRedLID"}
```

`status`：`loading` 加载模型、`ready` 正常、`error` 出错（`reason` 写原因）。主脑 10 秒收不到心跳就显示“离线”。

`label` 可能的方言：`zh mandarin` 普通话、`zh yue` 粤语、`zh wu` 吴语、`zh min` 闽语、`zh north` 北方官话（山东/河北/山西/陕西/辽宁等）、`zh xinan` 西南官话（四川/重庆/云南/贵州/湖北）、`zh xiang` 湘语；其他语言是 `en`、`ja`、`ko` 等 100 多种。

## 能不能同时听两台机器人、并发多少

- 每台机器人一条麦克风连接，各自切句，互不串：结果只发回它自己的 Room，并带 `robot_id`。
- 推理只有一个工作线程，按顺序一句一句判断，不会两句同时抢显卡。
- 速度：Mac M4 只用 CPU 时一句（3～6 秒的话）0.4～0.6 秒；官方给的显卡速度约每秒音频 0.07 秒，RTX 4060 上一句大约 0.2～0.4 秒。也就是一秒钟能处理十几秒的话，两台机器人就算同时说话也只多排队零点几秒；理论上十台以内都跟得上。
- 显存：半精度约 1.8 GB，和 tts_viseme 一起放在 8 GB 的 4060 上够用（截图里已用 4.8 GB）。

## 结果什么时候用

用户说完一句约 0.5 秒出结果，所以只影响下一轮：现在主脑先在工作台显示和写日志（`language_id room=... label=...`），要不要让她据此跟着说方言，等真人方言录音测完准确率再定。

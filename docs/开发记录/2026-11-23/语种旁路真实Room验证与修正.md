# 语种旁路：真实 Room 验证与修正（2026-11-23）

用户在 Windows 上启动了 11-20 写的 `sidecars/annie-dialect-lid`，要求我自己测。结论：服务和 Room 是通的，但测出两处会在机器人一开麦克风时出问题的缺陷，已修；Windows 上还是旧版，需要覆盖 `lid_service.py` 后重启。

## 只读探测真实 Room（没有发送任何数据）

- 两台机器人的 `reb_sam` 上都收到 Windows 服务每 2 秒一条的 `language_id_health`：`status=ready`、`device=cuda`、`mic_connected=true`、带 `robot_id`。Room 原样转发，另加 `from`、`ts`，并去掉值为 null 的字段。
- 两台机器人的 `reb_mic` 8 秒内都是 0 帧：机器人的麦克风当时没有往 Room 送声音，所以没法用真人声音测。
- Room 接受任意房间名，各房间互相隔离（`lidtest` 的每个通道 `size=1`）。WELCOME 的 `size` 含义仍未核实，不依赖。

## 测出的缺陷与修复（`sidecars/annie-dialect-lid/lid_service.py`）

1. **没拆 Room 的信封。** 麦克风二进制帧是“2 字节大端类型长度 + `MIC_AUDIO` + 640 字节 PCM”（主脑 `decode_room_binary_frame` 的格式），旧代码当纯 PCM 读。复现：真实信封下 22 秒的数字静音被切成一段 12 秒的“话”送去模型，而且每条消息的 11 个字节（奇数）还会让后面的采样错位。11-20 的 Mac 测试用的是我自己编的假 Room（发裸 PCM），所以没测出来。修复：`mic_payload()` 拆信封，和 `decode_room_binary_frame` 逐条对拍（含截断、零长度、别的类型、200 条随机字节）。
2. **机器人自己的声音被当成用户。** 她的扬声器声音会被她的麦克风收进去（云端 AEC 存在的原因）。修复：同时只读订阅同一 Room 的 `reb_tts`，按每个 `tts` 包的样本数算她“正在出声”的时间段（`Playing`，前留 0.15 秒、后留 0.6 秒，`stop` 包立刻结束）；一句话有一半以上落在里面就跳过，日志 `skipped ... overlap=NN%`，心跳 `echo_skipped` 加一。
3. **`reb_sam` 发布连接没人读。** Room 会把通道里的所有消息推给每个连接，发布连接不读会让 websockets 的接收队列满、保活应答也卡住。修复：给发布连接起一个读掉并丢弃的任务（和 10-16 板端麦克风程序“读掉 Room 推送”一致）；连接断了就忘掉，下次发布重连。
4. 顺手：连接用 `proxy=None`（Room 在局域网，不走系统代理；旧版 websockets 没这个参数就退回）；心跳加 `mic_fps`、`echo_skipped`；每分钟一行 `stats`（含 `vad_floor`）；识别结果为空时不发布；`LID_CONFIG` 可指向别的配置；`config.json` 加默认关闭的自测房间 `annie-test`→`lidtest`；`requirements.txt` 改为 `websockets>=15,<16`。

## 主脑与工作台

- `room_monitor.py`：`language` 视图带上 `mic_fps`、`echo_skipped`。
- 工作台“语种”胶囊：服务在线但没有最近结果、麦克风帧率低于 5 时显示“没收到麦克风声音”（黄色）；悬停显示麦克风帧率和跳过的回声数。已重新打包 `web/dist`。

## 真实 Room 上的实测（私有房间 `lidtest`，没有碰 roomv2 / roomv3）

做法：这台 Mac 上跑修好的服务（`run_service.py` 加载仓库里的 `lid_service.py`，CPU fp32）；脚本扮演机器人的麦克风（每 20 ms 一条带信封的消息，静音时是全零，像板子的 AGC 麦克风）、扬声器（`reb_tts` 的 `tts` 包）和 `reb_sam` 的听众。

| 项目 | 结果 |
| --- | --- |
| 安静 6 秒（全零帧，真实信封） | 0 次送模型；心跳 `mic_fps` 51.5 |
| 22 段合成方言实时播放 | 22/22 出结果；与“整段离线参考”一致 19/22 |
| 与“服务切出的同一段音频的离线结果”对比 | 21/22 完全一致（标签、段长、把握度）；另 1 段标签相同、把握度 0.937 对 0.960，段长差 20 ms（服务的底噪估计带着前面的状态） |
| 3 段与整段参考不同（东北 ×1、河南 ×2） | 北方官话 → 普通话（把握度 0.70～0.84）。原因：切句去掉了首尾静音（12/22 段比原片短），模型在北方官话与普通话的边界上不稳定，和 11-20 的结论一致；这种错法只是不跟，不会误切到别的方言 |
| 说完 → 出结果（Mac CPU） | 中位 1.17 秒，最大 2.91 秒（第一句含预热），含 0.6 秒等她说完 |
| 服务内排队 + 推理（Mac CPU） | 中位 0.85 秒，最大 2.34 秒（第一句） |
| 回声：`reb_tts` 播 3.2 秒的片段，0.25 秒后麦克风出现同一段 | 跳过，`overlap=100%`，`echo_skipped` +1，没送模型 |
| 用户在她最后一个字后 1.2 秒接话 | 正常识别：粤语 1.0 |
| 心跳与发布连接 | 约 4 分钟没有断，没有 `publish failed` |
| 主脑端到端：隔离主脑（只连 `lidtest`）订阅 `reb_sam` | `/api/robots/annie-a/room-observations` 的 `language`：`online`、`mic_fps` 50.3、`echo_skipped` 1、最近一句粤语 1.0；主脑日志 `language_id room=lidtest ...` |

回归：`uv run --locked pytest -q` 1555 passed；ruff、`uv lock --check` 通过；`npm --prefix web test` 146/0。新增测试在 `tests/test_language_id.py`（信封对拍、信封下静音不唤醒、回声时间段、服务跳过回声并计数、心跳字段、主脑视图）和 `web/tests/language-id.test.mjs`。

## 没测、待办

- **Windows 4060 的速度和半精度（fp16）精度**：Windows 上现在跑的是旧版，覆盖 `lid_service.py` 重启后，把 `config.json` 里 `annie-test` 的 `enabled` 改成 `true`，我从这边往 `lidtest` 发 22 段声音量（不碰真机）。
- 真麦克风下的切句门槛（-45 dB / 底噪 +12 dB）、回声余量（0.15 / 0.6 秒）、用户打断她时的表现：只能用真机的 `stats`、`skipped` 日志调。
- 真人方言的准确率（合成方言不是真人）。
- 旧版仍在 Windows 上跑的风险：机器人一开麦克风，旧版会把信封当声音，工作台会显示没有意义的结果；不影响机器人说话（方言跟随默认关）。

## 过程中的两条教训

- 假 Room 必须用真实的线上格式（信封、原样转发规则），否则测不出协议层的缺陷；测试夹具里已经用 `room_message()` 生成真实信封。
- 用户的主脑可能随时启动：端口 8780 上 11:04 起有用户自己的主脑连着 roomv2 / roomv3。所有往 Room 发数据的测试都只在私有房间里做，清理日志时按文件名精确删除自己产生的那一份，不按“最新”删。

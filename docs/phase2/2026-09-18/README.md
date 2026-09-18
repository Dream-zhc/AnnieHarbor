# Annie Harbor Phase 2 验收记录

日期：2026-09-18。

## 状态

Phase 2 的**内部协议与PCM准入层已通过验收**；真实Java Room网络Adapter尚未启用，因此Phase 2整体仍处于进行中，不能标记真实Room联调完成。

功能提交：`98a5726 feat: add Phase 2 bounded PCM gateway`、`7d859e1 feat: decouple PCM gate from mic.start`。进入Phase 2前独立基线：`2cebdf6 chore: establish Phase 1 baseline`。

## 已落地

- `annie-adapters`按Phase 0 fixtures解析Room BinaryFrame；非法长度、空type、截断和坏UTF-8均拒绝。
- `mic.start`作为兼容协议入口时只接受已证明的`pcm_s16le / 16000Hz / mono / 20ms / 640B`；PCM Gate本身通过内部`start_pcm_stream`启动，不再假设真实Room一定先发送`mic.start`，且transport仍拒绝其它未验证格式。
- ASR、`robot_event`、pressure/touch/gas、结构化/legacy vision和small-brain cancel身份规则均在Adapter边界归一；不新增外部target字段。
- 每个`robot_id`拥有独立Audio Input Gate和有界PCM队列。默认`audio_queue_capacity=25`、`audio_max_age_ms=500`；满队列丢最旧帧，优先保留新鲜输入。
- 来源必须先由Runtime显式绑定；支持内部定向和广播，未知来源、local loopback、目标未绑定、乱序、过期、错误帧长、重连旧队列均拒绝或清理。
- 同一显式source的PCM流激活后，legacy ASR兼容入口拒绝再次触发，避免已知同源双触发。
- SQLite升级到schema v2，只给`runtime_events`增加`queue_wait_ms`和`source`；PCM dequeue可观测输入排队等待。
- `/health`明确返回`phase=2`、`room=protocol_ready_not_connected`、`pcm_input=gate_ready`。`publish_enabled`仍禁止。
- live `WELCOME/ACK`投影已固化到`room_control_live.jsonl`，Adapter有严格`WELCOME`/订阅`ACK`解析回归；不把连接级`clientId`提升为稳定设备身份。
- `/api/debug/robots/{robot_id}/room-pcm`与`tests/html/phase2.html`用于本地二进制协议/准入验收，不冒充真实Room连接；调试入口也不再伪造`mic.start`。

## 验证结果

```text
uv run --locked pytest -q
47 passed, 2 warnings, 25 subtests passed

uv run --locked ruff check apps packages tests/test_phase1.py tests/test_phase2.py
All checks passed!

node --test web/tests/dashboard.test.mjs
PASS

uv lock --check
PASS

sh start.sh --check
ready=true, phase=2, room=protocol_ready_not_connected, schema_version=2
```

另在`127.0.0.1:8872`真实启动Uvicorn进程并完成HTTP验收：`/health`返回200；`/phase-tests/phase2.html`可访问；将Phase 0 `valid-mic.bin`通过调试入口注入`annie-a`后得到`accepted=1`、`queue_depth=1`、格式为16kHz/mono/S16LE/20ms/640B；随后进程正常终止。

两条Python warning仍来自FastAPI/Starlette测试栈对当前httpx/AnyIO接口的弃用提示，本阶段不为消除第三方warning扩大依赖范围。

## 真实Room控制面探测

同日对配置中的`ws://192.168.1.47:8090/robot/roomv2/reb_mic`做了只读控制面探测，不发送任何`MIC_AUDIO`业务帧：

- WebSocket可正常建立；服务端首先返回`WELCOME`，随后对`{"type":"SUBSCRIBE","data":{"types":["*"]}}`返回`ACK`，`data.what=subscribed`。
- `WELCOME.robotId`实测为`reb_mic`、`room`为`roomv2`，说明当前`robotId`更像路由路径标识，不能直接当设备实例ID。
- `WELCOME.clientId`由服务端分配为`anonymous-*`；连续两次重连得到不同clientId，因此它只可视为连接级身份，不能作为跨重连稳定设备身份。
- live catalog明确登记`MIC_AUDIO`为`PCM S16LE/16k/mono/20ms/640B`，与Phase 0 fixture一致；catalog没有登记`mic.start`。因此实现已把PCM Gate启动与`mic.start`解耦，由已验证transport格式直接启动；`mic.start`仅保留兼容解析路径。
- 第一次约5秒、随后连续约15秒观察均未收到真实`MIC_AUDIO` BinaryFrame，说明探测时没有活动麦克风数据，尚不能验证发送端身份、序号/时间语义、广播/定向或重连数据行为。

## 仍未证明 / 不得越界

- `MIC_AUDIO`已证明的BinaryFrame只含type+payload；**没有已证明的`robot_id`、source、sequence、timestamp或target wire字段**。当前`source_id`、sequence、定向/广播均是Runtime/Adapter内部元数据。
- Room控制面握手已证明，但稳定发送设备身份、服务端是否广播/定向、多个头订阅同房间时的设备归属仍未知；在数据面证据到位前不自动连接`config/rooms.toml`。
- 真实发送端抓包、AEC/回声责任、输出目标、播放回执仍缺失。
- cancel当前只锁定并测试身份匹配纯规则；将cancel真正作用于会话/TurnCoordinator属于后续会话协调层，不能在Phase 2凭弱身份误停新回合。
- legacy ASR双触发保护只对**已显式绑定为同一source**的输入成立；不同外部source是否对应同一麦克风尚无证据，不能自动合并。
- Realtime、独立TTS、嘴型、Memory、真实Room输出均未接入；Brain继续使用Fake。
- 本次真实启动验收为macOS；Windows启动需在Windows环境单独补验。

## 下一步

先让真实麦克风发送端在线并取得`MIC_AUDIO`数据面样本，至少确定：发送连接如何映射稳定设备、是否有额外序号/时间元数据、同房间定向/广播规则、重连行为和AEC责任。基于证据实现最小网络Adapter并做双头真实联调；若服务端没有可验证寻址能力，应明确增加协议/通道方案，而不是在Brain内部伪造设备目标。真实Adapter通过后再进入Phase 3。

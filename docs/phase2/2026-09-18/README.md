# Annie Harbor Phase 2 验收记录

日期：2026-09-18。

## 状态

Phase 2 的内部协议/PCM Gate与**真实Java Room网络Adapter均已落地**。roomv2/roomv3真实数据面隔离、自动化WebSocket生命周期与用户物理麦克风人工发送均已验证；本阶段输入Gateway可收尾。是否进入Phase 3仍由用户明确安排，不自动跨阶段。

前置内部Gateway提交：`98a5726 feat: add Phase 2 bounded PCM gateway`、`7d859e1 feat: decouple PCM gate from mic.start`。本轮真实Room实现：`13c2874 功能：接入按房间隔离的真实PCM网关`；验收页与回归：`c37584b 测试：增加真实Room回环与多房间隔离验收`。进入Phase 2前独立基线：`2cebdf6 chore: establish Phase 1 baseline`。

## 已落地

- `annie-adapters`按Phase 0 fixtures解析Room BinaryFrame；非法长度、空type、截断和坏UTF-8均拒绝。
- `mic.start`作为兼容协议入口时只接受已证明的`pcm_s16le / 16000Hz / mono / 20ms / 640B`；PCM Gate本身通过内部`start_pcm_stream`启动，不再假设真实Room一定先发送`mic.start`，且transport仍拒绝其它未验证格式。
- ASR、`robot_event`、pressure/touch/gas、结构化/legacy vision和small-brain cancel身份规则均在Adapter边界归一；不新增外部target字段。
- 每个`robot_id`拥有独立Audio Input Gate和有界PCM队列。默认`audio_queue_capacity=25`、`audio_max_age_ms=500`；满队列丢最旧帧，优先保留新鲜输入。
- 当前产品模型固定为**一个机器人绑定一个Room**。`config/robots.toml`当前为`annie-a→roomv2`、`annie-b→roomv3`；两个启用监听机器人若配置同一Room会直接拒绝启动，防止MIC_AUDIO串流。
- `config/rooms.toml`完整保留一个机器人Room所需的`main/tts/mic/visualdata/vision/sam`六条地址；roomv2使用已核实原始URL，其它Room按相同通道结构从`room_root`生成。
- Runtime启动后为每个机器人独立连接`/{room_id}/reb_mic`，严格检查WELCOME中的room/robotId/catalog，发送SUBSCRIBE并等待ACK。首个合法MIC_AUDIO自动启动对应机器人PCM stream；断线会清空该Room旧队列/stream并重连。
- `RoomPcmConnection`已从`websocket-client + threading.Thread + call_soon_threadsafe`重构为`websockets`纯`asyncio`协程；每个Room对应一个asyncio task，PCM在同一event loop直接进入Audio Input Gate，不再每20ms做跨线程调度。
- 同一显式source的PCM流激活后，legacy ASR兼容入口拒绝再次触发，避免已知同源双触发。
- SQLite升级到schema v2，只给`runtime_events`增加`queue_wait_ms`和`source`；PCM dequeue可观测输入排队等待。
- 全部配置Room订阅成功后`/health`返回`phase=2`、`room=connected`、`pcm_input=room_gateway_ready`；连接中/部分连接会如实降级。`publish_enabled`仍禁止。
- live `WELCOME/ACK`投影已固化到`room_control_live.jsonl`，Adapter有严格`WELCOME`/订阅`ACK`解析回归；不把连接级`clientId`提升为稳定设备身份。
- `/api/debug/robots/{robot_id}/room-pcm`只保留自动化协议边界回归。`tests/html/phase2.html`已改成真实回环页：浏览器直接连接选中Room的`reb_mic`，可发送测试PCM或采集真实麦克风，随后通过`/api/phase2/rooms`核对Runtime收帧和多Room隔离。

## 验证结果

```text
uv run --locked pytest -q
52 passed, 2 warnings, 25 subtests passed

uv run --locked pytest -q tests/test_phase2.py
18 passed, 2 warnings, 14 subtests passed

uv run --locked ruff check apps packages tests/test_phase1.py tests/test_phase2.py
All checks passed!

node --test web/tests/dashboard.test.mjs
PASS

uv lock --check
PASS

uv run --locked python -m annie_runtime --check --database var/tmp/phase2-check/runtime.db
ready=true, phase=2, room=connected, pcm_input=room_gateway_ready, schema_version=2
```

另在`127.0.0.1:8894`启动真实Uvicorn进程，同时成功订阅`roomv2/reb_mic`与`roomv3/reb_mic`。初始两头`accepted=0`；经真实Java Room发送3帧roomv2 MIC_AUDIO后仅`annie-a accepted=3`，`annie-b=0`；随后发送4帧roomv3后`annie-b accepted=4`，`annie-a`保持3。证明真实Room数据面按room隔离，没有跨房间串流。

本轮纯asyncio重构后再次对真实Java Room复验：Runtime同时成功订阅roomv2/roomv3；用`websockets`异步publisher发送`roomv2=3`帧、`roomv3=4`帧后，`annie-a accepted=3`、`annie-b accepted=4`，另一Room计数不受影响，证明重构未破坏线上兼容与隔离。

自动化新增真实本地WebSocket Server集成测试，不再直接调用私有PCM入口作为网络证明。测试覆盖`connect → WELCOME → SUBSCRIBE → ACK → binary PCM → roomv2主动断线 → Runtime重连 → 新PCM → shutdown`，同时用不同payload验证roomv2/roomv3不会串流，且重连会清掉旧Room PCM队列。

两条Python warning仍来自FastAPI/Starlette测试栈对当前httpx/AnyIO接口的弃用提示，本阶段不为消除第三方warning扩大依赖范围。

## 真实Room控制面与数据面

同日对配置中的`ws://192.168.1.47:8090/robot/roomv2/reb_mic`做了只读控制面探测，不发送任何`MIC_AUDIO`业务帧：

- WebSocket可正常建立；服务端首先返回`WELCOME`，随后对`{"type":"SUBSCRIBE","data":{"types":["*"]}}`返回`ACK`，`data.what=subscribed`。
- `WELCOME.robotId`实测为`reb_mic`、`room`为`roomv2`，说明当前`robotId`更像路由路径标识，不能直接当设备实例ID。
- `WELCOME.clientId`由服务端分配为`anonymous-*`；连续两次重连得到不同clientId，因此它只可视为连接级身份，不能作为跨重连稳定设备身份。
- live catalog明确登记`MIC_AUDIO`为`PCM S16LE/16k/mono/20ms/640B`，与Phase 0 fixture一致；catalog没有登记`mic.start`。因此实现已把PCM Gate启动与`mic.start`解耦，由已验证transport格式直接启动；`mic.start`仅保留兼容解析路径。
- 2026-09-18补齐真实数据面：Runtime同时订阅roomv2与roomv3后，外部测试publisher分别向两个真实`reb_mic`发送不同PCM payload，收帧计数严格落到对应机器人，跨Room隔离成立。
- 这也确认当前输入不需要寻找“稳定发送设备ID”：产品约束是一个机器人一个Room，MIC_AUDIO归属由接收连接的`room_id`决定。`clientId`继续只作为连接级信息。
- 用户于2026-09-18人工确认：两个物理麦克风分别都可以把连续音频发送到各自WebSocket Room。此证据只用于Phase 2“真实麦克风→Room输入”验收，不外推为TTS输出、设备播放或AEC已经完成。

## 仍未证明 / 不得越界

- `MIC_AUDIO`仍只含type+payload；**没有`robot_id`、source、sequence、timestamp或target wire字段**。Phase 2不再尝试补这些字段，Room连接就是输入隔离边界。
- AEC/回声责任、TTS输出、播放回执与嘴型输出尚未接入，属于后续输出/全双工阶段。
- cancel当前只锁定并测试身份匹配纯规则；将cancel真正作用于会话/TurnCoordinator属于后续会话协调层，不能在Phase 2凭弱身份误停新回合。
- legacy ASR双触发保护只对**已显式绑定为同一source**的输入成立；不同外部source是否对应同一麦克风尚无证据，不能自动合并。
- Realtime、独立TTS、嘴型、Memory、真实Room输出均未接入；Brain继续使用Fake。
- 本次真实启动验收为macOS；Windows启动需在Windows环境单独补验。

## 下一步

Phase 2到此停止：Room PCM输入、跨Room隔离、断线重连、本地真实WebSocket生命周期测试和用户物理麦克风发送证据已经齐备。保留当前六通道配置与Phase 2长期回归，等待用户明确安排Phase 3；不自动接Realtime/TTS/Memory。

# Annie Harbor（安妮港）— 小安妮 Brain V2

当前独立开发根：`/Users/dream/Documents/zhc/python/brainv2`。

**状态：Phase 1本机收尾已通过；Phase 2真实Room PCM Gateway已接入，roomv2/roomv3真实Java Room数据面隔离已通过，浏览器真实麦克风回环待人工验收。** 原目录继续冻结，只读参考，不回写。

## 开发前阅读

1. [文档入口与优先级](docs/README.md)
2. [项目规则](PROJECT_RULES.md)
3. [后端开发总计划](docs/规划/小安妮V2后端开发总计划.md)
4. [项目结构与配置规范](docs/规范/项目结构与配置规范.md)

只有处理外部协议/兼容问题时再读Phase 0；需要核对已完成骨架时再读Phase 1验收记录。不要默认扫描全部`docs/`。

## Phase 1 已完成

- Python 3.11 + `uv` workspace，成员仅`apps/runtime`与五个`annie-*`包；sidecar不进入workspace，第三方源码目录只在真实需要时创建。
- `start.sh`与`start.bat`统一进入`uv run --locked python -m annie_runtime`。
- Phase 1建立`runtime_snapshots` + `runtime_events`和WAL/幂等migration；Phase 2当前schema v2仅新增输入`source`与`queue_wait_ms`，仍未创建Ledger/Memory/Graph表。
- 一个Python进程内承载配置中的全部机器人头；当前示例配置为`annie-a`、`annie-b`。每个`robot_id`拥有独立queue/worker/current turn/cancel状态，但所有头由同一项目进程并发调度；自动化测试额外覆盖3头并发，证明实现不写死为两个头。
- `/health`、本地Fake控制API和最小Dashboard；Phase 2已接入Room PCM输入，LLM/TTS/长期记忆/嘴型仍未接入。
- 自动化测试证明停止、取消、FakeBrain异常与A队列积压不会破坏B。
- V1 可复用配置资产已提前迁入：阿里/DashScope、Qwen TTS、MiniCPM视觉地址、Java Room六个WS地址、天气、动作/attention/嘴型配置；Phase 2当前只接线各机器人Room的`reb_mic`输入，其余通道仍作为后续阶段配置资产。V1 `skills.json` 不迁入。
- 根目录`.env`自动加载；非敏感配置统一在`config/`，运行数据/SQLite/测试与ruff缓存统一在`var/`。
- 启动前检查端口：`start.sh/start.bat`发现目标端口已有Annie Harbor时自动终止旧实例并拉起新实例；如果占用者不是Annie Harbor则拒绝误杀并明确报错，不随机换端口。

项目显示名定为 **Annie Harbor（安妮港）**；目录和既有`annie-*`包名不为命名再做无价值重构。

## Phase 2 当前状态

- 已按Phase 0 fixtures实现Room BinaryFrame、兼容`mic.start`严格解析、ASR/传感器/视觉/robot_event归一和cancel身份匹配；live `reb_mic` catalog未登记`mic.start`，因此PCM Gate已与该wire消息解耦，transport直接以已验证16kHz/mono/S16LE/20ms/640B格式启动。
- 每个`robot_id`有独立Audio Input Gate与有界PCM队列；默认容量25帧、最大排队年龄500ms，满队列丢最旧帧保新鲜度。
- 当前产品模型固定为**一个机器人绑定一个Room**。`config/rooms.toml`保留该Room的`main/tts/mic/visualdata/vision/sam`六条完整地址；多个机器人通过不同`room_id`并发运行，Phase 2只启用各自`reb_mic`输入连接。
- Runtime启动后自动连接每个监听机器人对应的`/{room_id}/reb_mic`，完成`WELCOME → SUBSCRIBE → ACK`后进入`subscribed`；首个合法`MIC_AUDIO`自动拉起该机器人的PCM stream，断线清旧队列并重连。
- 已通过真实Java Room数据面回环：向roomv2发送3帧后`annie-a 0→3`且`annie-b=0`；再向roomv3发送4帧后`annie-b 0→4`且`annie-a`保持3，证明跨房间PCM隔离成立。
- `tests/html/phase2.html`现在直接把浏览器麦克风或测试PCM发送到真实Room，再由Runtime收回；不再用HTTP伪注入作为主要验收。
- 当前Brain仍为Fake；Realtime/TTS/Memory/真实Room输出均未接入。

## 当前下一步

先用`/phase-tests/phase2.html`完成人工真实麦克风回环：选择roomv2/roomv3对应机器人，确认麦克风帧持续发送且只有对应机器人的`accepted`增长；再执行页面“多房间隔离测试”。该人工验收通过后Phase 2收尾，再进入Phase 3。AEC、TTS输出、播放回执属于后续输出/全双工阶段，不再阻塞Phase 2输入Gateway。

后续按`3会话/协调 → 4 Realtime音频输入/文本输出 → 4B独立TTS+最小嘴型 → 5上下文 → 6记忆 → 7真实工具/多设备 → 8事件/复杂工作流 → 9完整嘴型/观测 → 10复验`推进；Phase 11再评估复杂任务模型策略。

**目标架构（规划，未接线）：** Qwen-Audio 3.0 Realtime Flash负责ASR、smart_turn、对话、Persona和Function Calling；普通聊天不经过LangGraph。保留独立Qwen-Audio-3.0-TTS-Flash与ForcedAligner（同一PCM+文本对齐后生成嘴型）；动作/表情/简单工具走Function Calling。事件经Event Router、Speak Policy、TurnCoordinator决定是否/何时开口；LangGraph仅负责复杂任务与结构化结果，Realtime统一最终表达。

## 启动与核验

从本项目根执行：

```sh
./start.sh --check
./start.sh
uv run --locked pytest -q
node --test web/tests/dashboard.test.mjs
uv lock --check
uv run --locked ruff check apps packages tests/test_phase1.py tests/test_phase2.py
```

Windows使用`start.bat`进入同一个Python入口。当前sidecar仍未多头化；Phase 2只完成Room PCM输入，不把后续TTS/动作/嘴型输出能力提前标为完成。

可视化验收页：`/phase-tests/phase1.html`用于单进程多头并发回合验收；`/phase-tests/phase2.html`用于真实麦克风→Java Room→Runtime回环和多Room PCM隔离验收。阶段HTML统一放`tests/html/`，最终整合时删除。

配置优先级固定为：CLI参数 > 已导出的系统环境变量 > 根目录`.env` > `config/*.toml`。机器本地配置从`.env.example`复制为`.env`；`.env`不提交。

## 后续AI任务提示词

```text
只在/Users/dream/Documents/zhc/python/brainv2开发。
先读AGENTS.md、docs/README.md、PROJECT_RULES.md和后端总计划；不要默认扫描历史阶段资料。
Phase 1骨架与本机审查收尾已经完成；Phase 2内部协议/PCM Gate已实现，不重复造Gateway。
Phase 2真实Room PCM Adapter已经接入并完成roomv2/roomv3真实数据面隔离测试；进入Phase 3前只需完成人工浏览器麦克风回环验收。
旧版只证明协议与行为，不决定V2结构；禁止运行时依赖父目录或robot_brain。
当前Runtime只有Fake Brain；Room PCM输入已真实连接，Realtime/TTS/长期记忆/嘴型均未连接。
目标热链是Room PCM → Realtime纯文本 → 独立TTS/ForcedAligner，正常聊天禁止经过LangGraph。
LangGraph仅做复杂任务结构化结果；事件/工具续推理由TurnCoordinator协调，Persona按Session/revision加载。
每个robot_id继续隔离可变状态；一个机器人只绑定一个Room，多个机器人通过不同room_id隔离。
```

# Annie Harbor（安妮港）— 小安妮 Brain V2

当前独立开发根：`/Users/dream/Documents/zhc/python/brainv2`。

**状态：Phase 1本机收尾已通过；Phase 2协议/PCM Gate内部验收已通过，真实Room网络联调仍待来源与寻址证据。** 原目录继续冻结，只读参考，不回写。

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
- 一个Python进程内同时运行`annie-a`、`annie-b`两个Fake RobotRuntime；每头独立queue/worker/current turn/cancel状态。
- `/health`、本地Fake控制API和最小Dashboard；真实Room/LLM/TTS/长期记忆/嘴型全部明确显示未接入。
- 自动化测试证明停止、取消、FakeBrain异常与A队列积压不会破坏B。
- V1 可复用配置资产已提前迁入：阿里/DashScope、Qwen TTS、MiniCPM视觉地址、Java Room六个WS地址、天气、动作/attention/嘴型配置；真实服务仍未接线。V1 `skills.json` 不迁入。
- 根目录`.env`自动加载；非敏感配置统一在`config/`，运行数据/SQLite/测试与ruff缓存统一在`var/`。
- 启动前检查端口：`start.sh/start.bat`发现目标端口已有Annie Harbor时自动终止旧实例并拉起新实例；如果占用者不是Annie Harbor则拒绝误杀并明确报错，不随机换端口。

项目显示名定为 **Annie Harbor（安妮港）**；目录和既有`annie-*`包名不为命名再做无价值重构。

## Phase 2 当前状态

- 已按Phase 0 fixtures实现Room BinaryFrame、严格`mic.start`（16kHz/mono/S16LE/20ms/640B）、ASR/传感器/视觉/robot_event归一和cancel身份匹配。
- 每个`robot_id`有独立Audio Input Gate与有界PCM队列；默认容量25帧、最大排队年龄500ms，满队列丢最旧帧保新鲜度。
- `source_id`、sequence、定向/广播仅是Runtime显式绑定的内部元数据，不是新增wire字段；未知来源、回流、乱序、过期、错误格式和重连旧帧均有回归。
- Web文本调试继续保留；`/api/debug/.../room-pcm`与`tests/html/phase2.html`仅用于本地协议/准入验收，不代表真实Room已连接。
- 当前Brain仍为Fake；Realtime/TTS/Memory/真实Room输出均未接入。

## 当前下一步

Phase 2尚未满足“真实Adapter联调”门槛。先取得真实Room发送/服务端证据，确认`MIC_AUDIO`连接身份、来源/序号/时间语义、同房间定向/广播方式、AEC责任及输出目标/回执；证据不足时保持`protocol_ready_not_connected`，禁止猜字段。完成真实Adapter与双头联调后再进入Phase 3。

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

Windows使用`start.bat`进入同一个Python入口。当前sidecar仍未多头化，同房间真实设备寻址仍是已知协议缺口。

可视化验收页：`/phase-tests/phase1.html`、`/phase-tests/phase2.html`。阶段HTML统一放`tests/html/`，最终整合时删除。

配置优先级固定为：CLI参数 > 已导出的系统环境变量 > 根目录`.env` > `config/*.toml`。机器本地配置从`.env.example`复制为`.env`；`.env`不提交。

## 后续AI任务提示词

```text
只在/Users/dream/Documents/zhc/python/brainv2开发。
先读AGENTS.md、docs/README.md、PROJECT_RULES.md和后端总计划；不要默认扫描历史阶段资料。
Phase 1骨架与本机审查收尾已经完成；Phase 2内部协议/PCM Gate已实现，不重复造Gateway。
Phase 2下一项是补真实Room来源/寻址证据并做最小网络Adapter联调；证据不足不猜wire字段。
旧版只证明协议与行为，不决定V2结构；禁止运行时依赖父目录或robot_brain。
当前Runtime只有Fake Brain；Room协议/PCM Gate已就绪但真实Room网络仍未连接，Realtime/TTS/长期记忆/嘴型均未连接。
目标热链是Room PCM → Realtime纯文本 → 独立TTS/ForcedAligner，正常聊天禁止经过LangGraph。
LangGraph仅做复杂任务结构化结果；事件/工具续推理由TurnCoordinator协调，Persona按Session/revision加载。
每个robot_id继续隔离可变状态；同房间输出目标未核实前保留待确认。
```

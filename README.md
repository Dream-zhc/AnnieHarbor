# Annie Harbor（安妮港）— 小安妮 Brain V2

当前独立开发根：`/Users/dream/Documents/zhc/python/brainv2`。

**当前状态以[总计划执行摘要](docs/规划/小安妮V2后端开发总计划.md)及对应阶段验收记录为准；本页不重复维护阶段通过/待验结论。** 原目录继续冻结，只读参考，不回写。

## 先看阶段目标

[总计划开头的“业务边界与阶段效果”](docs/规划/小安妮V2后端开发总计划.md)先解释做完能看到什么；第25节是Phase 2–11逐阶段任务卡（目标、输入输出、必须做/保留、不做、验收、必须先问、交付），第32节是AI开工模板。阶段README只记录实际证据，不定义另一套目标。

**固定业务模型：一机器人一独立Room，一房间六通道、一路PCM。** A→roomv2，B→roomv3；每个Room只归属一个机器人，六通道不因当前只接mic而删除。本期不做同房间多机器人或多音源混流。

## Phase 4C：正式工作台基础与功能整合（规划）

复用现有React + TypeScript工程及Phase 4/4B接线；4B正在开发，新增4C专门完善网页基础、接齐Phase 1–4B已有功能并验收布局/交互。之后功能在同一工作台追加，不重做4B、不另建前端。范围见[总计划第22/25节](docs/规划/小安妮V2后端开发总计划.md)，4C当前为规划，不代填完成。

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

以[总计划“当前执行摘要”](docs/规划/小安妮V2后端开发总计划.md)和对应阶段README为准，不在这里维护另一份验收进度。Phase 3验收与进入Phase 4的门槛见[Phase 3记录](docs/phase3/2026-09-18/README.md)；本次增加React工作台规划，不代替用户验收或启动下一阶段。

后续按`4 React工作台+Realtime文字 → 4B独立TTS+最小嘴型 → 4C工作台基础/功能/布局整合 → 5上下文 → 6记忆 → 7真实工具 → 8事件/复杂工作流 → 9嘴型/观测 → 10复验`推进，均在同一正式工作台交付；Phase 11再评估复杂任务模型策略。

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

可视化验收页：`/phase-tests/phase1.html`用于单进程多头并发回合验收；`/phase-tests/phase2.html`用于真实麦克风→Java Room→Runtime回环和多Room PCM隔离验收。Phase 1–5（含4B/4C）的阶段HTML统一放`tests/html/`；Phase 6及以后不新增独立HTML页，功能与网页验收直接进入现有React工作台。既有临时页按Phase 10最终整合安排、用户确认后清理，不提前删除。

配置优先级固定为：CLI参数 > 已导出的系统环境变量 > 根目录`.env` > `config/*.toml`。机器本地配置从`.env.example`复制为`.env`；`.env`不提交。

## 后续AI任务提示词

```text
只在/Users/dream/Documents/zhc/python/brainv2开发。
先读AGENTS.md、docs/README.md、PROJECT_RULES.md和后端总计划；不要默认扫描历史阶段资料。
Phase 1骨架与本机审查收尾已经完成；Phase 2内部协议/PCM Gate已实现，不重复造Gateway。
实际当前阶段与验收缺项按总计划执行摘要和对应阶段README核实，不从历史Phase 2说明推导当前进度。
保留Phase 4/4B既有React与业务接线；Phase 4C统一补齐网页基础、前序功能和布局验收，之后持续追加。
旧版只证明协议与行为，不决定V2结构；禁止运行时依赖父目录或robot_brain。
当前Runtime只有Fake Brain；Room PCM输入已真实连接，Realtime/TTS/长期记忆/嘴型均未连接。
目标热链是Room PCM → Realtime纯文本 → 独立TTS/ForcedAligner，正常聊天禁止经过LangGraph。
LangGraph仅做复杂任务结构化结果；事件/工具续推理由TurnCoordinator协调，Persona按Session/revision加载。
每个robot_id继续隔离状态；一机器人独占一个Room、六通道全保留、一路PCM输入，本期不做同房间多机器人。
实施前按总计划第25/32节复述目标/效果、输入输出、保留项、不做事项、验收和未决问题。
不明确的业务/协议/设备语义或删除配置/换模型/跨阶段扩展先问用户；相关改动等答复，不擅自替用户决定。
```

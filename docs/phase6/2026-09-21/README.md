# Phase 6｜长期记忆实施与统一验收

**状态：接线完成，待用户现场统一测试。** 自动化、一次真实云端请求、真人语音、浏览器与物理机器人分别计证据。

## 已确认业务

- 遗忘只删除长期记忆，不删除原始 SQLite 对话、Ledger、历史导出或备份。事实正文与向量一起失效；保留事实键、来源标识、版本和墓碑以防迟到任务复活。事实键若含敏感信息，不声称已做全系统隐私擦除。
- LangMem 0.0.30 调用阿里云 qwen3.8-flash 做后台抽取；text-embedding-v4 输出 256 维。密钥仅通过 DASHSCOPE_API_KEY；有 Key 默认启用，ANNIE_MEMORY_ENABLED=0 可关闭，ANNIE_MEMORY_BASE_URL 可覆盖百炼地址。
- 服务器绑定 robot_id 是身份边界。semantic 同机器人跨 Chat 使用；persona 和 episodes 随人格隔离；不跨机器人共享，不自造 user_id。模型和浏览器都不能自选机器人和 Persona。
- 现有 Realtime 主链、独立 TTS、Room、ForcedAligner 不变。普通闲聊不经过额外抽取或记忆搜索；问旧事才通过 Realtime recall_memory，明确更正/遗忘可通过版本化工具或工作台完成。

## 数据和失败语义

- 有来源、已完成且包含用户原话的 Ledger turn → 后台 LangMem 最多2条候选 → 版本化 SQLite 事实 → 异步向量写入。首启从当时最新 Ledger ID 开始，不会自行为旧历史批量调用云端模型。
- 最多检索8个候选、注入4条相关证据，最多256 UTF-8字节（非精确Token计量）。显式记忆工具的总截止时间为1.2秒；**普通实时聊天不等待这1.2秒**。此预算不是声学首响 P95 的实测承诺。
- 更正、遗忘由完整机器人/Persona、ID、版本校验，旧向量与正文同一事务失效；旧抽取和迟到 embedding 不能覆盖。Embedding 失败保留事实并显示失败状态，保守文字召回可兜底；检索故障不能编造用户经历。
- sqlite-vec 在现有 Runtime SQLite 内计算向量距离，另有版本化行和墓碑。本次不是 AsyncSqliteStore 作为生产主存，也不是 ANN 虚拟索引；之前独立的 AsyncSqliteStore 兼容实验不作为此生产实现已使用的证明。

## 自动化和真实云端证据

- Python：uv run --locked pytest -q
- 阶段六：uv run --locked pytest -q tests/test_phase6.py tests/test_phase6_memory.py tests/test_phase6_worker.py tests/test_phase6_failures.py tests/test_phase6_http.py
- Ruff：uv run --locked ruff check apps packages tests
- 依赖：uv lock --check
- 前端：npm --prefix web run typecheck；npm --prefix web test；npm --prefix web run build
- macOS arm64 / Python 3.11 实际验证：LangMem 抽取“喜欢喝茶”为 semantic，改口“喜欢咖啡”复用偏好键；text-embedding-v4 返回256维。Fake Cloud + 真实 SQLite/ASGI 测试覆盖 robot/Persona 隔离、跨 Chat、CRUD、版本冲突、迟到写入/向量、遗忘和后台 Ledger 消费。Windows、真实 Qwen Function Calling、多轮真人对话和物理播放仍待现场验证。

## 工作台统一测试 P6-W1 / P6-W2

1. 重启 Runtime，检查 health.memory 的运行和错误状态。当前机器人新 Chat 说“我喜欢喝茶”，待后台抽取后点顶部“记忆”查看事实、scope、来源、版本和索引状态；换 Chat 问“我之前说过喜欢喝什么”，核对回复与实际 recall_memory 结果。普通“你好”不应每轮调用记忆。
2. 改口“不是茶，现在喜欢咖啡”，查是否更正同一事实；在工作台手动更正也应刷新后持久化，旧后台输出不可改回茶。
3. 遗忘后刷新、换 Chat、重连不得通过长期记忆召回旧事实；原始历史仍在。旧版本修改、B机器人或其它 Persona 应拒绝或不泄漏。另用语音说“忘记这条长期记忆”，核对工具确实拿到所属 ID/版本并成功返回。
4. 桌面/窄屏浏览器、真人多轮、Room 音频、设备扬声器和P50/P95另行记录；不把 Fake/构建/单次云端样本冒充人工 PASS。Phase 5 长空闲与声学问题仍需单独复验。

### 2026-09-22｜跨 Chat 相对日期召回返工（待真人语音复验）

- 复现：同一 `annie-lively` Persona 的旧 Chat 曾说“下周我朋友来找我”，新 Chat 问“下周有人找我吗”，已有 `episodes` 未进入 Realtime 初始化，工具调用也不保证触发。实际库中还存在来源不符的重复摘要。
- 修复：Session 建立时仅从同机器人/Persona 的近期已完成用户原话中，有界恢复明确的来访陈述（21天、最多256轮扫描、最多2条，沿用512字节预算）；带原话 UTC 时间，明确相对日期需按原话时间解释，不将旧话断言为当前日程。较新的取消/忘记阻止旧陈述重现；问句、假设和别的 Persona 不作事实。普通回合不新增云端调用。
- 抽取候选中“来访”须由当轮用户陈述支持；召回时过滤旧 `episodes` 中来源不支持的相对日期摘要，旧记录保留供人工核对，不静默改库。自动化测试覆盖完整假 Realtime `session.update`、跨 Chat、来源错误、问句、否定、时效、取消及遗忘。
- 尚未验收：真实 Qwen 实际自然语言回答、真人 Room PCM 与浏览器、设备发声。自动化通过仅证明接线与证据输入，不宣称模型必然使用证据。

### 2026-09-22｜记忆管理简化

- 记忆管理仅列当前作用范围内仍有效的事实；遗忘、到期及旧 `no_extraction`、`placeholder`、能力测试和来源不匹配的旧来访摘要不占用主列表/计数。原始数据库记录不自动删除；技术字段只在单条「详情」展开。
- 「长期通用 / 当前角色 / 本次会话」明确切分作用范围，「关系 / 经历」为中文类型；搜索和类型筛选仅影响展示，不改变存储。新增表单由右上角 `＋` 打开，不占常驻列表空间。
- 自动提取与 SQLite 仍保持“用户……”客观第三人称，界面仅在已知句式中转换为“你……”；不改写底层事实，也不把关系推断成永久情感。遗留问题句及动作能力测试在后端注入/召回和新增抽取环节一并过滤。
- 前端单元测试和 Python 提取质量测试已增加；真实浏览器交互与实体机器人的自然语言表现仍须人工验收。

### 2026-09-23｜按回合统一诊断

- 先选当前 Chat 的一轮，再查该轮用户原文、生成文本与扬声器完整播报证据；LLM 只展示该轮所属 Provider Session 实际下发的 instructions、建连时恢复的轮次、字符预算和启动记忆，云端持续滚动历史无法直接读取，不能称为本轮完整云端输入。
- 显式 `recall_memory` 返回值、动作/表情 Function Calling 及后续 Room 状态、ForcedAligner 对齐音素计数/样本、音频和设备起播回执按 turn_id 隔离；模型调用返回、Room 发布、设备实际执行不混为一谈。
- 时序仅使用本轮有时间戳的区间，未记录不计算；音素详细事件每轮最多抽样3个、计数单独累计，动作与播放事件单轮最多保留32条；32轮时间线、4份 Session 快照和64条工具记录有界，不作为全量持久审计。旧轮证据过期明确显示“未记录”。
- Fake Realtime/SQLite 的分轮、工具和事件回归已补；真实云端提示词、真人语音、嘴型端和设备状态仍需现场验收。

# Phase 8｜外部事件与按需复杂工作流

## 阶段状态

- 2026-09-22：用户要求直接进入 Phase 8，Phase 7 真机物理证据及各阶段网页/设备验收延后统一执行。延后验收不等于既有未完成项已通过。
- 8A 与 8B 软件功能已接入；8C可选主动规划首版已接线但未完成真机/真实Qwen验证。真实设备、Room/云端和浏览器人工验收统一留待用户执行。**软件回归通过不等于 Phase 8 完整验收 PASS。**
- 8B 采用规划中已有的“已确认到期提醒”场景：一个用户明确提交内容及秒数的单次提醒；这是此次软件验证的具体范围，不推断用户同意了任何其他复杂任务或自主定时行为。

## 2026-09-23｜职责补充及对话提醒缺口

- 旧8B仅实现React「创建提醒」→HTTP→LangGraph→SQLite→到期发声；Realtime Session未声明`create_reminder`工具，`_tool_call`也不处理提醒。故用户在聊天输入“两小时后提醒我取外卖”时，模型可以说“收到”，但没有任何真实任务产生。截图中8B列表空不是Graph丢失事件，而是对话请求未进入任务流程；「最近事件」显示的是8A传感器策略，并非Graph节点流。
- 本轮补接`create_reminder` Function Calling（仅当前真实用户回合，文本/1..86400秒明确相对时长），按`robot/chat/persona/session_epoch/generation`校验并经已有ReminderService与LangGraph创建任务。返回`scheduled + task_id + due_ms`后才可作创建成功依据；外部事件不许自主创建提醒。当前仅有模型指令及工具回执约束，**尚无逐字流式输出承诺的强制拦截**；若模型跳过工具却仍答应，依旧必须判验收失败，不把修接线宣称为100%消灭假承诺。
- Fake Qwen/SQLite回归覆盖Session工具声明、7200秒创建任务、重复call_id去重、跨机器人隔离、外部事件拒绝；另使用独立真实Qwen Realtime文字会话验证该输入返回`create_reminder`，参数`{"text":"取外卖","delay_seconds":7200}`且本轮未先输出文本。真实模型测试**未写入生产Runtime/SQLite或现场Room**，真实口语到期提醒与设备播放仍待验。
- 8C已按以下「可选首版」接入；此前的“未实现”是2026-09-23时点状态，不能视为今日现场验收结论。

## 2026-09-24｜8C 主动规划软件闭环（现场验收待完成）

- 模型：**独立规划专用** `qwen3.8-max-0902`，从 `config/providers.toml` 的 `llm.inquiry` 读取，12秒独立超时、最多2个HTTP连接，显式 `enable_thinking=false` 以保持后台决策延迟可控；`ANNIE_IDLE_MODEL` 可覆盖。普通Qwen-Audio Realtime、记忆抽取及天气工具模型均不变。已在项目当前百炼配置下真实调用，成功返回合法结构化SPEAK/new_value；该请求不是整条Room/TTS真机实测。
- 启用：`ANNIE_IDLE_ENABLED=1`、`ANNIE_TTS_ENABLED=1`、`ANNIE_IDLE_TRUSTED_ROOM=1` 和 `DASHSCOPE_API_KEY` 缺一不能启动8C。**仅运营者明确确认受控私有空间**才设 trusted-room，不要在公共区域启用；模型仍复用已有Realtime Chat，因此不保证从云端已有历史中绝不生成私密细节，未验证现场人物身份时不可宣称已完成隐私验收。
- 判定：有效用户回合结束超过约60秒，当前TTS/音频/事件均无抢占，Room `reb_visualllm` 连接新鲜（≤10秒），同一私有场景中恰好1个人、v2结构化 `facts.subjects` 至少2帧相隔≥1秒且置信度≥0.8，才调用一次Graph+独立Qwen。自然语言“有人”不构成人物证明；`roomv3`尚无可用视觉监听时保持静默。超时、失败或NO_SPEAK均本轮静默不重试。
- 二次追问：第一句必须有**同轮真实speaker started + drained回执**，从实际播完起至少120秒，持续有人且有不同于第一句的新公共场景证据，同时模型结构化 `new_value=true` 且新话题与上一句非重复才允许；最多两次，不可凭 `submitted_unknown`、网页试听或本地PCM完成继续追问。所有音频/视觉/Chat/Persona/generation在异步完成后复核；用户语音开始立刻作废候选并禁用本轮再次尝试。
- 权限：Graph不发房间/电机，规划模型仅见公共物体类别，不携带历史对话和记忆；最终表达仍通过当前Realtime/TTS。外部回合禁止物理动作、表情、提醒创建和记忆召回/改写；未实测原Chat记忆可能混入最终生成文本的强隔离，**不得在公共场所开启**。
- 诊断与验收：工作台「定时提醒与事件」展示模型、实际规划耗时、抑制原因、次数和真机播放回执；`submitted_unknown`仅证明提交Realtime。本轮Fake Graph/Realtime/双头/视觉/打断、Qwen HTTP MockTransport验证见 `tests/test_phase8c_idle.py`、`tests/test_phase8c_model.py`。P8-W4还需真实持续视频流、真实模型与Realtime回答、设备开始/结束播报、双头及隐私人工验收；网页/模拟用例不代填这些结论。

## 8A 已实现的软件边界

- 复用所属 Room 的 `main`（已启用 TTS 时复用现有监听连接）及 `visualdata` 文本事件，不推断消息内自称的目标机器人；仅通过已经验证的 Room/socket 绑定路由。无 Realtime 时不额外创建事件 WS，避免影响 Phase 2 原有纯 PCM 网关。
- 使用 Phase 2 已验证的 `sensor.touch` / `sensor.pressure` / `sensor.gas`、`vision.*` 和 `robot_event` 归一化输入；当前**不接原始画面**或新视觉模型。WorldState 仅保留有限、带时间/来源的摘要，不将逐帧图像或 tracks 内容写入模型对话。
- 仅明确标识头顶且 `level=1` 的触摸事件在 15 秒冷却后提出简短回复请求；其他触摸、压力、视觉事实默认 Context Only。重复 `event_id`、过期/非法时间与置信度异常记录拒绝原因。提醒仅由当前会话用户明确创建的**本地持久任务**触发，不信任任意 Room 消息伪造到期提醒。
- 每机器人独立等待队列，订阅 Realtime 生命周期事件驱动发声；语音候选、当前回复、播放中不抢话。等待时记录原因，到期/换 Chat 或 generation 丢弃；提交至 Realtime 是 `submitted_unknown`，绝不代表 TTS 或物理扬声器完成。
- 外部事件回合通过当前 Persona Realtime 生成自然语言，**硬拒绝物理动作、表情、预览以及长期记忆写入工具**；不让被动感知偷偷造成电机副作用。普通聊天不依赖事件服务或 Graph。
- 正式 React 工作台可查看事件、状态、排队原因与当前有限事实；后端 GET `/api/robots/{robot_id}/external-events` 按当前会话过滤事件。

## 8B 已实现的软件边界

- 使用现有 LangGraph `StateGraph` 完成 `prepare → scheduled → due_checked → result_prepared` 单次提醒流程；SQLite 任务表持久保存每个阶段返回的 graph state/checkpoint、结构化结果、session 身份及版本。当前**不是 LangGraph 原生 AsyncSqliteSaver**：Graph 节点是短暂确定性的，持久化与恢复由业务 SQLite CAS 驱动，不宣称恢复节点内部的任意指令进度。
- GET/POST `/api/robots/{robot_id}/reminders`、POST `/reminders/{task_id}/control` 支持真实创建、暂停、恢复和取消；前端集成于原 React 工作台 Phase 8 面板，不增独立 HTML 页。提醒秒数范围 1～86400，最多记录当前 Chat/Persona 最新 64 条；不做通用周期任务或任意复杂任务平台。
- 重复 request_key 在同一 robot/chat/persona 内最多创建一个任务；SQLite 状态 CAS 保证同一 task_id 最多预约一次 Realtime 提交。切换 Chat/Persona 立刻标记旧任务 stale_session；同一会话服务重启可接管仅上一代尚未提交的待办和暂停任务。进程崩溃时处于提交中则标记 `send_unknown`，**不重试可能已经发送的消息**。
- 8B修复：到期不再受60秒TTL静默丢弃；忙碌、Realtime离线或服务中断时保留 `scheduled`，恢复后唤醒及最多5秒周期复查。迟到不足10分钟补提醒并说明延迟（只有真实重启记录才提及重启）；迟到10分钟及以上询问是否仍需要，不自动新建提醒。一个机器人同批逾期任务合并为一次外部文本请求，SQLite批量CAS预约；另一个机器人的任务绝不合并。重启接管所有仍有效的当前Chat/Persona待办，并恢复历史误标为 `expired/late_reminder` 的未发送任务。`submitting`崩溃仍转`send_unknown`、`submitted_unknown`仍不当成物理播出回执，绝不盲目重播。
- 没有任务时不创建提醒表、不加载 LangGraph、不启动额外轮询进程；Graph 失效不会拦截普通聊天。仅使用一个唤醒式协程处理各头任务，休眠至到期、失败/过期或收到会话/播放状态通知。

## 明确保留的未完成项与最终验收

### `reb_visualllm` 视觉文字上下文补接（2026-09-23）

- 2026-09-23 视觉交谈调整：在现有 Realtime 的可替换视觉段加入直接回答主体/动作/位置、可按人格自然接话、禁止无关视觉复述与主动技术辩解的规则；不增加新模型、Graph或自发响应。兼容原 `description`，可选 `schema_version:2 + facts` 显式保留主体、关系、推测与变化；上下文仍有预算与10秒有效期，截断/无效结构在诊断中可见。算法同步格式、限额和风险见 [视觉事实与对话协议](视觉事实与对话协议.md)。Fake Qwen上下文/Room WS测试只能验证传输和提示词，真实模型口语效果待人工验收。

- 新增仅属于 `roomv2` 的 `reb_visualllm` 文字监听：`{"type":"visualllm","description":"..."}`，不处理原始图片、不额外请求视觉模型。工作台可看最新画面与 LLM 上下文确认状态。
- 每个机器人只保留最新摄像头描述；仅有效 Room 连接且收到描述未超过 10 秒才可注入。最新描述通过现有 Qwen Realtime 的 `session.update.instructions` 替换视觉段；保留本 Session 的 Persona、记忆与其它指令，不创建用户消息、不调用 `response.create`、不主动发声。首条即时提交，后续更新最多每秒一次；过期或断线后撤销视觉段；云端 `session.updated` 回显相同 instructions 才标记已确认。
- 自动化证据：`tests/test_visual_llm_context.py` 检查双机器人隔离、上下文替换/清理、无虚构回合；`tests/test_visual_llm_network.py` 从本地真实 WS 发布视觉文字并验证到 Fake Qwen 的 instructions。另以独立临时 Room WebSocket + 真实 Qwen Realtime 做文字问答联调，`session.updated` 确认最新描述，模型回答“右边桌上有一个蓝色的杯子”。生产 Room 摄像头发布端、物理麦克风与扬声器现场验收仍待用户统一执行，不用文字联调代替物理闭环。

- 未做真实 Room 事件输入、真实 Qwen、真实 TTS/扬声器起声、双机器人硬件和端到端网页人工验收；8A/8B 均仅是软件实现和 Fake Provider 回归。
- Phase 7 的设备动作开始/结束/停止可信回执、聊天误触发动作风险与真机签收仍需纳入最后统一验收，不用 Phase 8 软件测试替代。

## 软件回归

- 2026-09-23 内部触发消息泄漏修复：提醒和外部事件回合持久化 `turn_source=external`、`external_prompt`，`user_text` 留空；正式聊天只展示小安妮回复，不再生成虚假的用户紫色气泡（包含 SSE 实时流和重载历史）。开发者回合诊断可查看内部提示。上下文恢复和记忆抽取跳过内部输入，SQLite v8 迁移识别历史提醒/事件前缀与关联提醒 turn_id，保存原文用于诊断并清除伪用户文本。Provider 仍按既有 Realtime API 提交内部输入，任务 submitted_unknown 仍非物理播出回执。首次使用新版本需重启 Runtime 完成数据库迁移。

- 2026-09-23 18:06「关电脑」实机排查：SQLite任务到期18:06:50，仍scheduled；创建回合18:06:21已发布TTS、18:06:24声学生成完成，但没有扬声器播放完成回执，保留的speech.turn_id阻塞提醒调度，直到18:09:21 Realtime重连才提交LLM，18:09:22才生成回复。补充「本地PCM流已完整发布 + worker结束 + 2秒保守等待 + 无真机speaker仍在播放回执」的等待释放条件；**本地完成不等于真实扬声器播放完成，原unknown证据不改写**。真机speaker明确上报playing/progress/held仍继续阻塞，避免抢话。Scheduled因Realtime/当前回合/播放而等待时记录原因，工作台显示等待而非“状态正常”。代码与模拟回归完成不代表现运行实例已更新或真机回执可靠。

- 2026-09-23 8B调度返工：Fake Realtime + 独立SQLite新增跨重启短/长迟到、历史expired恢复、同机器人合批、双机器人隔离、离线恢复与无毫秒空转、一次SQLite读取失败后继续运行。`tests/test_phase8_reminders.py` 11项通过；全量Python、网页测试及真实硬件的最终结论分别以最新执行/人工记录为准。此项仍须真实Runtime健康、真实Qwen、TTS与扬声器到期验收，不据Mock宣称物理闭环。

- `tests/test_phase8_events.py`：时间/TTL、冷却、去重、有限事实、两头等待/隔离、提交模型未知态、外部事件动作拦截及 API。
- `tests/test_phase8_reminders.py`：真实到期协程、Graph 状态与 SQLite checkpoint、暂停/恢复/取消、重试幂等、重启同会话接管/发送未知态、跨机器人/Chat 隔离、迟到 Graph 返回与故障时普通对话。
- `uv run --locked pytest -q`、`uv run --locked ruff check apps packages tests`、`uv lock --check`、`npm --prefix web test`、`npm --prefix web run build` 以及 `git diff --check`，以本阶段提交前最终执行结果为准。

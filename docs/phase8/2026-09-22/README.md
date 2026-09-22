# Phase 8｜外部事件与按需复杂工作流

## 阶段状态

- 2026-09-22：用户要求直接进入 Phase 8，Phase 7 真机物理证据及各阶段网页/设备验收延后统一执行。延后验收不等于既有未完成项已通过。
- 8A 与 8B 软件功能已接入；真实设备、Room/云端和浏览器人工验收统一留待用户执行。**软件回归通过不等于 Phase 8 完整验收 PASS。**
- 8B 采用规划中已有的“已确认到期提醒”场景：一个用户明确提交内容及秒数的单次提醒；这是此次软件验证的具体范围，不推断用户同意了任何其他复杂任务或自主定时行为。

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
- 到期时只在所属 Realtime 空闲、未处于物理播放时提交模型；当前用户说话、播放中不抢话，超过到期后 60 秒记录 expired。Graph 只返回事实，最终语言与 TTS 仍交给现有 Realtime；完成状态 `submitted_unknown` 仅代表请求交给模型，不是物理播出回执。
- 没有任务时不创建提醒表、不加载 LangGraph、不启动额外轮询进程；Graph 失效不会拦截普通聊天。仅使用一个唤醒式协程处理各头任务，休眠至到期、失败/过期或收到会话/播放状态通知。

## 明确保留的未完成项与最终验收

- 未做真实 Room 事件输入、真实 Qwen、真实 TTS/扬声器起声、双机器人硬件和端到端网页人工验收；8A/8B 均仅是软件实现和 Fake Provider 回归。
- Phase 7 的设备动作开始/结束/停止可信回执、聊天误触发动作风险与真机签收仍需纳入最后统一验收，不用 Phase 8 软件测试替代。

## 软件回归

- `tests/test_phase8_events.py`：时间/TTL、冷却、去重、有限事实、两头等待/隔离、提交模型未知态、外部事件动作拦截及 API。
- `tests/test_phase8_reminders.py`：真实到期协程、Graph 状态与 SQLite checkpoint、暂停/恢复/取消、重试幂等、重启同会话接管/发送未知态、跨机器人/Chat 隔离、迟到 Graph 返回与故障时普通对话。
- `uv run --locked pytest -q`、`uv run --locked ruff check apps packages tests`、`uv lock --check`、`npm --prefix web test`、`npm --prefix web run build` 以及 `git diff --check`，以本阶段提交前最终执行结果为准。

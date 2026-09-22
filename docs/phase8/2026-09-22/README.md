# Phase 8｜外部事件与按需复杂工作流

## 阶段状态

- 2026-09-22：用户要求直接进入 Phase 8，Phase 7 真机物理证据及各阶段网页/设备验收延后统一执行。延后验收不等于既有未完成项已通过。
- 8A 软件实现已接入；8B **待选定具体多步骤任务，尚未实施**。本阶段整体不得标记 PASS。

## 8A 已实现的软件边界

- 复用所属 Room 的 `main`（已启用 TTS 时复用现有监听连接）及 `visualdata` 文本事件，不推断消息内自称的目标机器人；仅通过已经验证的 Room/socket 绑定路由。无 Realtime 时不额外创建事件 WS，避免影响 Phase 2 原有纯 PCM 网关。
- 使用 Phase 2 已验证的 `sensor.touch` / `sensor.pressure` / `sensor.gas`、`vision.*` 和 `robot_event` 归一化输入；当前**不接原始画面**或新视觉模型。WorldState 仅保留有限、带时间/来源的摘要，不将逐帧图像或 tracks 内容写入模型对话。
- 仅明确标识头顶且 `level=1` 的触摸事件在 15 秒冷却后提出简短回复请求；其他触摸、压力、视觉事实默认 Context Only。重复 `event_id`、过期/非法时间与置信度异常记录拒绝原因。**没有配置提醒事件的可信协议/开口语义，因此尚未接入提醒触发。**
- 每机器人独立等待队列，订阅 Realtime 生命周期事件驱动发声；语音候选、当前回复、播放中不抢话。等待时记录原因，到期/换 Chat 或 generation 丢弃；提交至 Realtime 是 `submitted_unknown`，绝不代表 TTS 或物理扬声器完成。
- 外部事件回合通过当前 Persona Realtime 生成自然语言，**硬拒绝物理动作、表情、预览以及长期记忆写入工具**；不让被动感知偷偷造成电机副作用。普通聊天不依赖事件服务或 Graph。
- 正式 React 工作台可查看事件、状态、排队原因与当前有限事实；后端 GET `/api/robots/{robot_id}/external-events` 按当前会话过滤事件。

## 明确保留的未完成项与最终验收

- 8B 缺少用户指定的真实多步骤目标、步骤、工具及完成判据；不替用户虚构天气代理，不创建空壳 LangGraph/checkpoint。待明确任务后实现暂停/恢复/取消、幂等与结果隔离。
- 未做真实 Room 事件输入、真实 Qwen、真实 TTS/扬声器起声、双机器人硬件和端到端网页人工验收；8A 仅是软件实现和 Fake Provider 回归。
- Phase 7 的设备动作开始/结束/停止可信回执、聊天误触发动作风险与真机签收仍需纳入最后统一验收，不用 Phase 8 软件测试替代。

## 软件回归

- `tests/test_phase8_events.py`：时间/TTL、冷却、去重、有限事实、两头等待/隔离、提交模型未知态、外部事件动作拦截及 API。
- `uv run --locked pytest -q`、`uv run --locked ruff check apps packages tests`、`uv lock --check`、`npm --prefix web test`、`npm --prefix web run build` 以及 `git diff --check`，以本阶段提交前最终执行结果为准。

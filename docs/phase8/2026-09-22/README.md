# Phase 8｜事件、提醒与按需复杂任务摘要

> 2026-10-10精简。8A/8B及8C软件接线与独立云端验证已记录；真实Room事件、持续视觉、TTS/扬声器、隐私与双头物理闭环待统一验收。**软件回归不是Phase 8整体PASS。** 当前配置/新Bug优先于历史数值。

## 8A：事件路由与发声协调

- 复用所属Room验证过的main/visualdata连接及sensor.touch/pressure/gas、vision.*、robot_event归一输入；不相信消息自称的机器人目标，不处理原始画面或新增视觉模型。无Realtime不额外创建事件WS。
- WorldState只留有限、带来源/时间的摘要；明确头顶且level=1触摸在15秒冷却后提出短回复，其余触摸/压力/视觉默认Context Only。event_id去重、TTL/时间/置信度校验与拒绝原因可查。
- 各robot等待队列独立，生命周期事件驱动协调，用户起声/回复/播放中不抢话；到期、切Chat或generation丢弃。提交Realtime仅为submitted_unknown，不是TTS或物理播放完成。
- 外部事件回合硬拒绝物理动作、表情、预览、记忆写入等副作用；8C另禁记忆召回。被动感知不授予电机权限，普通聊天不依赖事件服务或Graph。
- React复用事件与有限事实视图，`GET /api/robots/{robot_id}/external-events`按当前会话过滤。

## 8B：用户明确的单次提醒

- 仅用户明确提交内容及1～86400秒相对时长的单次提醒，不扩展成周期调度/通用自主任务平台。React/API与Realtime create_reminder共用ReminderService；工具必须是当前真实用户回合，经robot/chat/persona/session_epoch/generation与call_id校验，外部事件不能自主创建。
- 创建成功依据是scheduled、task_id、due_ms真实返回。**仍无逐字流式承诺强拦截**，模型跳过工具却答应仍属验收失败，不能称假承诺已100%消除。
- LangGraph确定性短节点 `prepare → scheduled → due_checked → result_prepared`，业务SQLite持久graph state/checkpoint、结构化结果和身份/CAS；不是原生AsyncSqliteSaver，不声称能恢复任意节点内部指令。
- 既有 `/reminders` GET/POST与 `/reminders/{task_id}/control`支持创建/暂停/恢复/取消，当前Chat/Persona最新64条。无任务不加载Graph、建提醒表或另起轮询进程；一个唤醒式协程管理各头，Graph失败不拦普通聊天。
- request_key在robot/chat/persona内幂等，CAS最多预约一次提交；切身份旧任务stale_session，重启接管有效待办/暂停任务。提交中崩溃标send_unknown，不重试可能已发送的消息。
- 到期不再受60秒TTL静默丢弃；忙碌、离线/中断保留scheduled，通知唤醒及最多5秒复查。迟到不足10分钟补提醒并说明延迟，≥10分钟问是否仍需要；仅有真实重启证据才提重启，不自动新建任务。
- 同robot同批逾期合并一次Realtime请求并批量CAS，不跨机器人合并；恢复历史误标expired/late_reminder的未发送任务，submitted_unknown始终非物理播出证据。

## 视觉文字上下文

- 该阶段roomv2的reb_visualllm监听只接视觉文字，兼容description及schema_version:2/facts，不请求额外视觉模型；主体/关系/推测/变化、预算和风险见[视觉事实与对话协议](视觉事实与对话协议.md)。其他Room无可信新鲜视觉时保持缺证据，不伪装已接线。
- 每头仅最新描述、已验证连接且≤10秒才可注入；复用Realtime session.update.instructions替换视觉段，保留Persona/记忆/其他指令，不造用户消息、不response.create或主动开口。首条即时，后续最多每秒一次，过期/断线撤销；session.updated精确回显才确认生效。
- 规则要求直接回答主体/动作/位置，不无关复述、不主动技术辩解；传输/提示通过不证明真实视觉或口语质量合格。

## 8C：可选空闲主动规划

- 独立规划模型历史基线qwen3.8-max-0902，读取providers的llm.inquiry、可用ANNIE_IDLE_MODEL覆盖；12秒超时、最多2个HTTP连接、enable_thinking=false。普通Realtime、记忆和天气模型不替换。
- 显式ANNIE_IDLE_ENABLED、TTS、ANNIE_IDLE_TRUSTED_ROOM和Key满足才启用；仅经运营者确认的受控私有空间，不在公共场所开启。规划模型只见公共物体类别、不带历史/记忆、不发Room/电机，最终表达仍交当前Realtime/TTS。
- 用户有效轮结束约60秒且无音频/事件抢占，视觉连接新鲜≤10秒、同一私有场景恰好1人，v2 facts.subjects至少2帧实测跨度≥1秒且置信度≥0.8才调用Graph+规划。自然语言“有人”不证明人物；失败/超时/NO_SPEAK静默不重试。
- 二次追问需第一句同轮真实speaker started+drained，实际播完至少120秒，持续有人且有不同公共场景证据，模型new_value=true且话题非重复；最多两次。submitted_unknown/browser_test/本地PCM完成不能解锁追问。
- 异步后复核音频/视觉/Chat/Persona/generation；用户起声立即作废候选并封本轮重试。外部轮不能动作/表情/创建提醒/召回或写记忆。
- **隐私未闭合**：最终复用的Realtime Chat云端已有历史可能混入私密内容，未证明人物身份与记忆强隔离；不得把规划模型只见公共事实误写成最终表达已完成隐私验收。
- 工作台显示模型、耗时、抑制原因、次数和真实回执；2026-10-08两帧人物ID/跨度及语音抢占竞态修复细节见[8C核对](../2026-09-24/8C验收核对.md)。

## 历史实证与保留的Bug修复

| 证据 | 结果与限度 |
| --- | --- |
| 独立真实Qwen提醒工具 | “两小时后提醒我取外卖”返回create_reminder、text=取外卖、delay_seconds=7200，当次未先输出正文；未写生产库/发现场Room，不证明真实口语到期播出。 |
| 独立规划HTTP | 百炼返回合法SPEAK/new_value结构；不是整条Realtime/TTS真机闭环。 |
| 本地Room WS＋真实Qwen视觉文字 | session.updated确认最新描述，回答“右边桌上有一个蓝色的杯子”；非生产摄像头/物理声学验收。 |
| 提醒延迟真实排查（2026-09-23） | “关电脑”18:06:50到期但仍scheduled；18:06:24本地合成完成、缺speaker完成回执而speech.turn_id阻塞，至18:09:21重连后提交、18:09:22生成。缺回执不能冒充播完。 |

- 内部提醒/事件用turn_source=external、external_prompt保存，user_text空；聊天只展示机器人回复，不生造用户气泡，开发者可看内部提示。上下文恢复/记忆抽取跳过内部输入；历史SQLite v8迁移保留原文供诊断，不把内部文本当用户原话。
- 调度等待可在本地PCM完整发布＋worker结束＋2秒保守等待且无speaker仍播放证据后释放；speaker明确playing/progress/held继续阻塞。**这是等待释放，不是物理drained**，unknown不变；工作台显示实际等待原因。
- 当时提醒专项11项通过，覆盖重启短/长迟到、历史expired恢复、同头合批/跨头隔离、离线恢复无空转及SQLite读取失败后恢复；不将Mock通过代填当前现场结论。

## 长期回归与统一验收

保留 `tests/test_phase8_events.py`、`test_phase8_reminders.py`、`test_phase8c_idle.py`、`test_phase8c_model.py`和 `test_visual_llm_context.py/test_visual_llm_network.py`，覆盖TTL/冷却/去重、队列、身份、Graph状态、SQLite恢复、未知发送、失败隔离、视觉替换/撤销和晚包。常规命令见[项目入口](../../../README.md)。

待用户在正式React工作台验证：真实触摸/视觉→策略/排队→Qwen/TTS→speaker起停播；聊天创建与迟到/重启提醒；8C持续人物证据、第一句及真实播完后的第二句、用户抢占、双头及隐私；Phase 7动作完成/停止、误触发风险仍一并保留。网页/模拟证据与设备/人工证据分列，不恢复独立HTML验收页。

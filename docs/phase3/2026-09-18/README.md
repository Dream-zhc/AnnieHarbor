# Annie Harbor Phase 3 验收记录

日期：2026-09-18。

## 当前状态

Phase 3 的会话事实、隔离、Conversation Ledger、TurnCoordinator 最小规则与前端加载接口已经实现；真实 Realtime/TTS/设备输出仍未接入。本阶段自动化通过，tests/html/phase3.html 已提供人工浏览器验收入口；浏览器布局与交互仍需人工实际点击确认，因此当前不自动宣布 Phase 3 完成人工验收，也不进入 Phase 4。

## 本阶段确认的业务规则

- 机器人本身就是身份边界。Phase 3 不引入 user_id，也不把 room_id、设备名、人脸或声纹当自然人身份。
- 逻辑会话按 robot_id + chat_id + persona_id 隔离；同名 Chat/Persona 在不同机器人之间仍是两份独立状态。
- 各机器人默认 Persona 继续来自 config/robots.toml 的 persona_id；Persona revision 是会话版本字段。
- SQLite 持续保存完整会话，直到显式删除；切 Chat/Persona 不删除旧历史，只切换当前 Session。
- Realtime 后续只加载当前 Chat/Persona 最近 20 轮有效对话。Phase 3 已实现并测试该窗口；同一 Session 不在每轮重复加载历史。
- 有效短期上下文只取完成且未取消/未中断的对话；失败、取消、旧 generation/discarded 回合仍留在完整 Ledger，但不进入 20 轮窗口。
- 播放证据分为 generated / published / spoken / playback evidence。无真实播放证据时保持 unknown，模拟状态不得冒充设备已播。

## 已落地

- SQLite schema 升级到 v3，新增 conversation_sessions、conversation_turns、conversation_social_state。
- Runtime 启动会恢复上次选中的 Chat/Persona，但递增 session_epoch/generation，防止进程重启后重放旧副作用。
- 切 Chat/Persona 会使旧 generation 失效、取消当前 Fake 回合、清理旧排队请求；迟到结果不会写成当前回复。
- 显式删除当前 Chat 时也先使 generation 失效并取消旧回合，避免删除后迟到结果又写回来。
- 每个机器人独立 TurnCoordinator：区分模型忙与播放仍在进行；请求有容量、TTL、等待原因和去重；即时允许和等待中的请求都在 TTL 内去重。
- SocialState 当前只接受显式结构化更新，不增加情绪模型；字段按当前 Chat/Persona 隔离，支持纠正、过期和 stale generation 拒绝。
- config/runtime_defaults.toml 的 conversation.history_turns 已从 6 改为 20，并由统一 bootstrap 传入 Runtime。
- health 升级为 phase=3，新增 capabilities.conversation=ledger_ready；llm/tts/memory/viseme 继续如实保持未接入状态。

## 前端/HTTP接口

Phase 3 为正式前端保留了以下后端接口边界：

- GET/POST /api/robots/{robot_id}/session
- GET /api/robots/{robot_id}/conversations
- GET/DELETE /api/robots/{robot_id}/conversations/{chat_id}?persona_id=...
- GET /api/robots/{robot_id}/context
- GET /api/robots/{robot_id}/turns/{turn_id}
- POST /api/robots/{robot_id}/turns/{turn_id}/playback
- GET /api/robots/{robot_id}/coordinator
- POST /api/robots/{robot_id}/response-requests
- GET/PUT /api/robots/{robot_id}/social-state

这些接口当前用于 Fake 会话规则与后续 Phase 4 Realtime 接线，不声称已经具备真实 AI/播放能力。

## 自动化验证

    uv run --locked pytest -q tests/test_phase1.py
    34 passed, 2 warnings, 11 subtests passed

    uv run --locked pytest -q tests/test_phase2.py
    18 passed, 2 warnings, 14 subtests passed

    uv run --locked pytest -q tests/test_phase3.py
    11 passed, 2 warnings

    uv run --locked pytest -q
    63 passed, 2 warnings, 25 subtests passed

    uv run --locked ruff check apps packages tests
    All checks passed!

    node --test web/tests/dashboard.test.mjs
    PASS

    uv lock --check
    PASS

两条 Python warning 仍来自 FastAPI/Starlette 测试栈的第三方弃用提示，本阶段不因此扩大依赖范围。

## Phase 3 验收页

访问 /phase-tests/phase3.html：

1. 选择 A/B，查看当前 Chat、Persona、session_epoch 与 generation。
2. 切 Chat/Persona，确认 epoch/generation 变化，旧历史仍可加载。
3. 连续发送 Fake 回合，确认完整历史持续增长，而短期窗口最多 20 轮。
4. 模拟部分播放，确认 generated/published/spoken/unknown 分列。
5. 标记播放中后提交响应请求，确认进入 waiting；重复请求被去重，TTL 后消失。
6. 切换机器人后重复同名 Chat/Persona，确认两边历史互不出现。
7. 显式删除当前 Chat 时确认只删除选中机器人+Persona 的历史。

该页面只调用当前公开 HTTP 接口，不复制后端会话逻辑。浏览器桌面/窄屏布局、按钮交互和删除确认仍需人工实际点击，不用自动化测试代填通过。

## 未实现 / 不得越界

- 未接真实 qwen-audio-3.0-realtime-flash，PCM 仍只进入 Phase 2 Audio Input Gate。
- 未建立 Provider Session，也未实际向 Realtime 注入 20 轮上下文；Phase 3 只建立稳定的数据/加载契约。
- 未接独立 TTS、Room 输出、真实播放回执、ForcedAligner、动作或 LangGraph。
- assistant_spoken_text 目前仅能通过测试/模拟接口写入；不得称为真实设备播放证据。
- Long-term Memory/LangMem 仍未开始；Conversation Ledger 不是 Phase 6 长期记忆。

## 下一步门槛

先由用户通过 tests/html/phase3.html 完成会话切换、A/B隔离、20轮窗口和删除行为人工验收。Phase 3 人工验收确认后，等待用户明确安排 Phase 4；不得自动接 Realtime。

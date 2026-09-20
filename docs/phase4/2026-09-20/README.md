# Annie Harbor Phase 4 实施记录

## 状态

- 用户已明确安排进入 Phase 4；Phase 3 README 保留原有人工验收待确认状态，不倒填通过。
- 4-工作台：第一轮真实 Phase 3/2 API 接线已实施，**自动化和构建可通过，不代表浏览器/设备人工验收通过**。
- 4-Realtime：真实型号已接线，文本/工具调用完成小范围真实账号联调；**连续真语音→smart_turn→转写→文字及物理麦克风尚待人工验收，不宣布4-Realtime通过**。Fake与真实接口分开。

## 本次已实施

- web/ 原位升级 React + TypeScript + Vite，固定 npm 锁文件；旧 JS/CSS 及复制式构建脚本不再发布。
- 会话切换、列表、完整 Ledger 历史、20 轮上下文、Fake 文本输入/取消、SocialState/Coordinator 和 Room 诊断接既有 HTTP API。
- 每机器人独立订阅身份；旧会话异步结果按切换序号拒绝。切机器人或离页释放本页浏览器麦克风。
- 测试 PCM / 浏览器麦克风复用 Phase 2 MIC_AUDIO BinaryFrame 和真实 Room WebSocket，须先人工确认同 Room 无第二音源；不冒充协议已有多音源检测。
- Python 正式静态入口从 web/dist/ 发布；启动前检查入口及 hash 资源，构建丢失时在替换旧进程前报错。
- tests/html/phase4.html 只存验收步骤和正式页面入口，不复制 React 功能。

## 4-Realtime 接线与真实证据

- 每个启用机器人单独持有 Qwen-Audio 3.0 Realtime Flash WebSocket Session。单个 session.update 联合设置 text-only、pcm、smart_turn、20轮上限、Persona 标识及仅模拟 preview_action 工具；密钥仅从根 .env / 系统环境读取，不返回前端。国际地域可通过 ANNIE_REALTIME_URL 显式覆盖，型号不允许变更。
- Runtime 的 Room PCM Gate 在模型 Session ready 后把每帧 20ms、16kHz、S16LE、mono、640B 连续 append 到对应机器人。禁止自建 ASR、串另一 LLM、双播语音或普通聊天进 LangGraph；Provider 结果分派到同一机器人的事件缓冲及既有 SQLite Ledger。
- 实时事件由 /api/robots/{robot_id}/realtime/events?after=N 有界增量提供，前端按 robot/chat/persona/epoch/generation 过滤；转写、增量文字、首文本指标、错误、模拟工具调用及必要续推理分开显示。
- 切 Chat/Persona、删除当前 Chat、取消及 Room 断连触发 Session 更替，旧 generation 的迟到文本不得进入新会话；仅有效完成回合进入20轮上下文，完整历史不自动删除。外置 TTS/播放/嘴型仍未启用，播放证据维持 unknown。
- 真正连接两路 roomv2/roomv3 及两个不同 Provider Session，联合 session.update 成功；在隔离测试 Chat 发出“请只回复：收到。”，收到了“收到”“。”两段文字增量，最终 Ledger completed/“收到。”，本地网页文本提交至首段文字 119.48ms（不是麦克风至首响）。
- 真正发起 preview_action(description=“举手”)，结果 simulated/real_device_executed=false，模型续推理返回“模拟结果：已预演‘举手’动作，未执行真实设备动作。”；本地网页文本提交至首段文字 464.77ms。真实设备未执行。
- 测试 Chat phase4-realtime-smoke 已显式删除，删除了两条测试 Turn；机器人A恢复原 chat/persona，机器人B未操作。真实 Provider 连接退出曾触发 HTTP 代理 socket.close 卡死；改为丢弃 Session 时直接 abort transport。随后实测 SIGTERM，两路 Session 均正常清理，Runtime 出现 Application shutdown complete，未再次 SIGKILL。

## 尚待落实与验收

### 4-Realtime 音频泵返工

- 将 `_audio_pump` 中 `next_nowait()` + `sleep(0.01)` 轮询替换为 `AudioInputGate.next_frame()` 的 `await queue.get()`；无 PCM 时阻塞等待新帧唤醒，不引入额外固定轮询等待。Phase 2 原 `next_nowait()` 接口保留。
- `task_done()` 在发送或丢弃之后的 `finally` 执行，包含发送协程取消；原有有界队列、满队列丢最旧帧及统计保留。
- 在发往 Provider 之前按单调时钟复核实际采样接收时间是否过期，按 `stream_epoch`、source 和 Session generation 拦截被重启/切换淘汰的帧；不改变 20ms/640B 的 Room PCM 格式。
- Fake Socket 自动化验证覆盖空闲等待唤醒、发送期间取消与 `queue.join()`、溢出、过期及同源重启，不以此代替物理麦克风证据。**真实 Room 麦克风 → smart_turn → 转写 → 文本仍待人工联合验收；未宣布 Phase 4 PASS。**

- **未验**真实 Room PCM/物理麦克风 → Provider smart_turn → 实际转写/语音文字连续两轮、附和误判、两头并发语音、网络断线/自动恢复；PCM pump 与失效/工具隔离已有 Fake Socket 单元回归，不冒充真实音频证据。
- 4-工作台桌面/窄屏实际点击、两个 Room 的真实 PCM 与物理麦克风隔离、关闭页面后 Runtime 独立运行。
- 当前北京默认 endpoint/本机账号已证明可调用目标型号并返回真实文字和工具结果；其他地域、正式 Persona 文本素材、固定语音/判轮质量样例、长时抖动统计及正式浏览器人工体验仍未确认。当前指令仅携带 Persona 标识及共性规则，**不得声称正式人格素材已完整注入**；现有旧 Provider 配置资产不因此覆盖。
- 不进入 4B 的 TTS、嘴型、实际动作或长期记忆。

## 可复核命令

    npm --prefix web ci
    npm --prefix web run typecheck
    npm --prefix web test
    npm --prefix web run build
    uv run --locked pytest -q
    uv lock --check
    uv run --locked ruff check apps packages tests

本次实测：React typecheck/build 退出 0，Node 回归 4 passed；Python 70 passed / 2 warnings / 25 subtests。两条 warning 是已有 Starlette/AnyIO 测试依赖弃用提示；ruff 通过。真实 CLI 使用显式端口 18781 启动，两个已配置 Room 的 MIC WS 订阅成功，GET /、/phase-tests/phase4.html 和 /health 均返回 200。临时 CLI 实例已关闭；未执行物理麦克风/设备人工验收，未调用 Realtime Provider。

追加4-Realtime自动化最终复核：Python **79 passed / 2 warnings / 25 subtests**（含 9 项 Realtime 专项）；Node 4 passed；TypeScript、Vite build、uv lock、ruff 均通过。已有两条 Starlette/AnyIO 第三方弃用 warning 未扩大依赖范围。未使用 Fake 结果代替上述真实 Provider 证据。真实联调只针对单轮文本、工具续推理和连接/退出；不把 119.48ms 误写成 PCM 至首文本指标。

正式页面 /；临时验收入口 /phase-tests/phase4.html。所有真实设备结果仍需人工提供证据。

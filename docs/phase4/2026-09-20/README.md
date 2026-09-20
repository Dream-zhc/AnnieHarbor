# Annie Harbor Phase 4 实施记录

## 状态

- 用户已明确安排进入 Phase 4；Phase 3 README 保留原有人工验收待确认状态，不倒填通过。
- 4-工作台：第一轮真实 Phase 3/2 API 接线已实施，**自动化和构建可通过，不代表浏览器/设备人工验收通过**。
- 4-Realtime：尚未接线；LLM capability 保持未接入，不能用 Fake 回显冒充真实对话。

## 本次已实施

- web/ 原位升级 React + TypeScript + Vite，固定 npm 锁文件；旧 JS/CSS 及复制式构建脚本不再发布。
- 会话切换、列表、完整 Ledger 历史、20 轮上下文、Fake 文本输入/取消、SocialState/Coordinator 和 Room 诊断接既有 HTTP API。
- 每机器人独立订阅身份；旧会话异步结果按切换序号拒绝。切机器人或离页释放本页浏览器麦克风。
- 测试 PCM / 浏览器麦克风复用 Phase 2 MIC_AUDIO BinaryFrame 和真实 Room WebSocket，须先人工确认同 Room 无第二音源；不冒充协议已有多音源检测。
- Python 正式静态入口从 web/dist/ 发布；启动前检查入口及 hash 资源，构建丢失时在替换旧进程前报错。
- tests/html/phase4.html 只存验收步骤和正式页面入口，不复制 React 功能。

## 尚待落实与验收

- Phase 4 Realtime 的真实 PCM → smart_turn → ASR/增量文本/Function Calling、会话断线恢复、工具校验和延迟基线。
- 4-工作台桌面/窄屏实际点击、两个 Room 的真实 PCM 与物理麦克风隔离、关闭页面后 Runtime 独立运行。
- 目标账号/地域的模型调用可用性、Persona 素材和有效轮次质量样例需在真实接入前确认；现有旧 Provider 配置资产不得为此覆盖。
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

正式页面 /；临时验收入口 /phase-tests/phase4.html。所有真实设备结果仍需人工提供证据。

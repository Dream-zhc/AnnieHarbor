# Phase 5｜短期上下文与人格质量

## 本轮范围及实际状态

- 用户明确安排开工；Phase 4/4B/4C 的真机音频、AEC、嘴型及人工网页验收仍待验，不改写既有结论。
- 本轮只修复 Realtime **重连重建**时的有界短期历史：保留最新完整用户改口，按新到旧选取连续上下文，限制最多原有 `context_limit` 轮及约 12,000 字符；字符只是保守代理，**不是精确 token 统计**。不引入记忆、Graph、额外 LLM。
- TTS 启用时只把带 `speaker_reported_drained` 的实际 `assistant_spoken_text` 注入助手历史。未确认播放的已完成用户输入可以恢复为 user-only；browser_test、仅发布 PCM、仅生成文本均不得冒充已播。纯文字模式才使用生成回复。
- Realtime 稳定指令增加“明确更正优先、必要时才追问、用户要求不继续时收住”；不是情绪关键词状态机，也不假设 `persona_id` 已拥有风格素材。
- 开发者观测区分「重连真正注入快照」与 Ledger 最近 20 轮；快照不冒充 Provider 后续实时对话。正式 React 保留，`tests/html/phase5-context.html` 仅给出可复跑操作及人工 A/B 留证规则。

## 业务仍待确认，不能宣布 Phase 5 PASS

- 三套 Persona 起始预设现已在 `config/personas.toml`，只是新建的自然/温和/活泼候选，**不是迁移来的历史 Persona 名称或已获用户确认的最终风格**；真实模型差异和关系边界仍须用户实测。既有任意 Persona ID 保持可用，但未配置时明确显示「未配置」，不假称有风格素材；Chat 的 `persona_revision` 保留为会话隔离标记，不假称已实现独立版本化素材。
- 承诺应在短期保留多久、匿名 Chat 结束边界及人工量表分歧处理；这些问题不使用猜测写入长期记忆。
- 用户实测同模型多轮原始回答，桌面/窄屏与麦克风/扬声器全双工，设备 speaker 真实回执与首响 P50/P95。Phase 4B 噪音和整机卡顿仍不能宣布修复。

## 复核

`uv run --locked pytest -q tests/test_phase5.py`；`uv run --locked pytest -q`；`uv run --locked ruff check apps packages tests`；`npm --prefix web run typecheck`；`npm --prefix web test`；`npm --prefix web run build`；`uv lock --check`。人工复跑入口 `/phase-tests/phase5-context.html`。

本轮实测：Phase 5 专项 7 passed；Python 全量 116 passed、25 subtests、2 条既有第三方弃用警告；前端 8 passed，TypeScript、Vite 构建、Ruff、uv lock、`git diff --check` 通过。HTML 的 HTTP 可访问性仅由 Fake TestClient 验证，未执行真实 CLI 进程/浏览器操作、声学设备或 Provider 多轮 A/B；以上结果不等于 Phase 5 完成。

实施中工作区出现另一任务正在编辑并暂存 Realtime SSE 与前端音频相关文件；本阶段不得将其混入 Phase 5 提交，也不得覆盖、恢复、清理相关变更或编造单独可归因的音频验收。

## Phase 5 工作区接线（本轮追加）

- `config/personas.toml` 保留三个可编辑起始表达风格；Python bootstrap 启动时一次性读取，`/api/personas` 只暴露 ID 和标签。新建 Chat 弹窗可直接选预设，不重建前端；Persona 只在 Session 初始化设置一次，共性身份/事实/工具合同不因风格改变。
- React 对话页侧栏加入用户主动设置的「自然追问/少追问/不主动追问」：沿用现有 `question_pressure` SocialState，限定 `source=user_setting` 和枚举值；当前机器人/Chat/Persona 作用域持久化，空闲时重建 Realtime Session 后应用；忙时返回明确失败。其他传感器来源 SocialState 只做证据，不提升为指令；设置后以实际 `persona_snapshot` 为准。
- 对话侧栏加入三组多轮案例、逐句填入、当前 Chat 的真实 Ledger 回复与回执状态，不自动向 Room/Provider 批量发送，也不把关键词相等当成质量合格；开发者区展示 Persona 生效快照、SocialState 来源、实际重连注入历史及裁剪预算。
- 修复 SQLite 恢复查询误丢没有助手文本但完成了的用户输入：纯文字/独立 TTS 已生成与扬声器已播仍由重连选择器分别判断，不能用历史展示伪装成 Provider 实际收到的回复。

**本轮自动化重新验收：** 专项 10 passed；Python 全量 119 passed、25 subtests、2 条既有弃用警告；前端 8 passed，TypeScript 与 Vite build、uv lock、diff check 通过。原有字段、Room/PCM/扬声器协议不变；真实浏览器点击、真实 Provider 人格 A/B、耳听与物理设备均未以自动化结果替代。

**独立真实 HTTP 检查：** 以临时 SQLite、Fake Runtime、不订阅现场 Room、不使用 Provider 密钥，在本机 127.0.0.1:18973 实际启动 Uvicorn；`/`、`/health`、`/api/personas`、`/phase-tests/phase5-context.html` 均返回 200，Persona API 返回三个真实预设 ID/标签；测试服务已终止，未影响现场默认端口/Room。未安装可用的 Playwright 依赖，不宣称桌面/窄屏浏览器 GUI 与真实语义质量通过。

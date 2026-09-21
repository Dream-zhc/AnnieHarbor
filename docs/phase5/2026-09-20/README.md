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

## 2026-09-21 用户截图反馈：聊天布局与网页播放

- 逐轮消息以 `turn_id` 合并 Ledger 与同一流式回复，不再给每个运行中回合画空助手气泡，避免完成瞬间单独流式气泡消失/重复；取消与失败显示中文小字，播放证据不再以裸 `unknown` 混在聊天正文。
- 右侧以「语音输出」优先展示真实 TTS 状态、最近 PCM 发布帧、设备回执；Persona/追问、多轮复跑和连接详情为命名明确的折叠卡。聊天气泡限制阅读宽度，桌面输入区保留在视口内，窄屏不横向溢出。
- 现场 `/health` 的 `annie-a / roomv2` 曾显示 TTS `cancelled`、已发布 768 帧 PCM、设备回执 `unknown`；这不是「TTS 未配置」，也不是网页或设备完成播放证据。正式 React 不含浏览器 PCM 播放器；没有私自重建已在 4C 删除的第二套播放器、不自动启用浏览器和真机双声源或修改现场 Room/`.env`。
- 共性 Persona 指令抑制用户陈述喜好后跑题为机器人身体能力解释与故事菜单；生效需下一次 Provider Session 初始化，不宣称旧会话回复已变好。

## 2026-09-21 追加：Room TTS 网页调试扬声器

- 用户明确要求在正式 React 对话页接 `reb_tts` 的原音频，并有手动开关。右侧语音输出卡片加入默认关闭的「开启网页调试扬声器」；只连接所选 robot 归属 Room 的 `urls.tts`，检查 WELCOME `room + reb_tts`、订阅 `tts` 并确认 ACK。24kHz/mono/S16LE Base64 PCM 按真实 sample cursor 顺序进 AudioContext；重复 seq 不重播、停播立即清空、500ms 以上积压舍弃，关闭/离页/切机器人释放 WS 与音频节点。打开后只听新发布的帧，**不是历史音频下载或第二次合成**。
- 正式后端继续只把 TTS 输出发布到 Room `reb_tts`；网页没有 Provider Key，不向 Room 重发 TTS PCM、不建第二个 TTS 服务，也**不向 `reb_main` 伪报 browser_test/speaker 回执**。网页能听见≠实体扬声器实际播放；原 `speaker_reported_*` 证据继续单列。请使用耳机，避免与本页 Room 麦克风或外部扬声器形成回声。
- 本机对真实 `roomv2/reb_tts` 做**只读连接/订阅检查**，收到 `WELCOME roomv2/reb_tts` 及 `ACK subscriptions=['TTS','*']`，随后退出；未向现场发布测试音频、未调用模型。Chromium + 假 Room 配合假 AudioContext 验证 960B PCM 调度、重复 seq 去重、Room stop、开关关闭及切机器人释放；另用**原生 Chromium AudioContext** 检查播放节点实际调度到 `onended`。仍不能把无真实扬声器/声学测量的浏览器测试记为耳听、AEC 或真机验收。
- 回归：`npm --prefix web run typecheck`、`npm --prefix web test`（11 passed）、`npm --prefix web run build`、Ruff/diff check 通过；可选真实 Chromium 复跑 `node web/tests/room-debug-speaker.browser.mjs`，需本地 Runtime，但 mock 拦截 reb_tts 不会向现场发布帧。

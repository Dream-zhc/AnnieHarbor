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

## 2026-09-21 实际对话/全双工排障（未作现场自动干扰）

- **真实历史与重连快照：** `annie-a / phase3-chat1` 的 Provider Session 恢复了 20 个仅 user 输入，assistant 全部是 `user_only_no_speaker_drain`，其中有 `Fake debug probe` 和多段过期问候/眨眼/摄像头问题。随后用户让机器人「你选一个呗」，它却回到那些旧主题；这证明重连提供了误导性的单向对话，不证明 Provider 的其他内部因素不存在。现在最多恢复连续 3 个未确认听见的 user-only 轮；已确认扬声器播完的助手回复仍按原协议保留，不把网页试听当成实体扬声器。旧 Ledger 不删除；需安全重建 Session 才会采用新恢复边界。
- **TTS stop 的确定原因：** Realtime 事件存在 `tts_queue_full`、`tts_queue_timeout`，对应 stop 发布给 reb_tts；最新一次模型文本完成但外置 TTS 只播发 65 帧（约 1.3s PCM）后因超时主动停播；网页「收到 Room 停播指令」不属于播放器伪报。原实现 8 段队列满即 cancel，`finish()` 等 1s 仍满也 cancel，且让主 Realtime 读事件协程等待。新实现保留 8 段有界队列，把尚未入队的原文合并在 `pending`，合成消费者空出位置再分段，尾段/stream_final 必须到最后才发布；`finish()` 不堵模型事件读循环，显式取消仍按原 Room STOP 协议生效。新增缓慢 Fake TTS+长文本压力回归验证全文不丢、不会意外 STOP，不等同于真实听感验收。
- **麦克风仍待定位：** 当前健康里 Room 累计接收 12,166 帧，Runtime 累计向 Provider 发送 12,156 帧，随后当前 Session 有 `response_idle_timeout`（180 秒未生成回复）；原计数是累计值，不足以证明这些帧在同一次 Provider Session 发出，更不能证明 PCM 有效声音、ASR 触发或是否被 smart_turn 判为无效。新增网页本地输入设备/电平（不保存原始音频），Runtime 当前 Session 的送帧/`speech_started`/完成转写/无效轮次计数与右侧诊断。该数据要等新 Runtime 与再次实际说话才能判定缺口，**没有擅自改 VAD、Provider 模型、Room PCM 协议或向真实 Room 注入测试音频**。
- **回复风格：** Session 共性提示改为优先响应最新句、用户让选就明确选、闲聊简短、不反复自介/问候/套菜单、不把未接线硬件推断为没有实体。只在新 Session 生效，不能以提示修改替代多轮真人 A/B。
- **旧进程保留：** 用户当时浏览器麦克风已在传输，未重启 8780 的 Runtime，未打断现场 Room；Python 新逻辑必须在用户停止麦克风后安全重启才生效。前端 build 已完成，刷新网页可先观察本地电平，但前后端新诊断需重启后联动。
- 回归：Python 121 passed / 25 subtests / 2 条既有第三方警告；前端 12 passed（新增 16k/48k 非零波形编码断言）、TS/build/Ruff/uv lock/diff check 通过；Chromium 假麦克风/假 Room + 原生 WebAudio 自动化通过。尚无真人说话 ASR 成功、现场持续 TTS 整句播完、AEC 全双工与三人格语义验收证据。

## 2026-09-21 Demo 迁移第一刀：Qwen 听觉 + 外置流式 LLM

- 按 `sidecars/qwen_smart_duplex_demo_v1_7_qwen_primary` 的主链路，将 Realtime 角色改成可选的**听觉/ASR/smart_turn 协处理器**：配置外置 LLM 时，Provider Session 使用 `["text", "audio"]` 模态和极短 sensor prompt，不再让 Realtime 直接生成最终回答；最终文字由 DashScope OpenAI-compatible Chat Completions 流式模型输出，再送原有外置 TTS 与 Room 协议。默认 `ANNIE_CASCADE_LLM_ENABLED=1`，模型默认 `ANNIE_CASCADE_LLM_MODEL=qwen3.7-flash`，可用 `ANNIE_LLM_BASE_URL` 覆盖；未配置 `DASHSCOPE_API_KEY` 时不会启用外置 LLM。
- 新增 `ExternalChatLLM` 适配器，保持连接池，支持 OpenAI-style streaming text 与 `tools`/`tool_calls`；当前只接入既有 `preview_action` 工具，运行时本地校验并发布 `tool.result`，不执行真实设备动作。此实现以运行时实测为准：网页/日志会显示 `cascade_llm`；如模型不返回 tool call，只会走纯文本，不把能力写死为已验证。
- 文本输入路径不再向 Realtime 发送 `response.create`；音频路径在 ASR final 后启动外置 LLM，忽略 Realtime sensor 可能吐出的 `.` 或其他内部文本，避免双回答。语音打断方面，ASR 增量会发布 `playback.hold_requested` 诊断事件；有效 final 会取消旧 TTS 并启动新回答。完整的 Demo 式“暂停保留音频 → 无效恢复 → 有效清空”还未迁入实体/Room 播放端，下一步应移植可靠播放器状态机。
- 修复一个前次 TTS 背压补丁遗漏：当 TTS 队列已被消费完、`finish()` 才到达时，worker 可能睡在 `queue.get()`，不会发布 `stream_final`；现在 finish 会唤醒 worker 并发布尾包。新增回归覆盖「finish 晚于队列清空」场景。
- 回归：Python 125 passed / 25 subtests / 2 条既有警告；前端 12 passed，TS/build、Ruff、uv lock、diff check 通过。未重启现场 8780 Runtime，未向真实 Room 注入测试音频；真实全双工、语义质量、tool call 能力和连续播放仍需现场复测。

## 2026-09-21 Demo 迁移复核与统一测试边界

**本轮只允许验证已接通的级联链路，不能签发完整全双工 PASS。** 代码复审发现并修复：外部 LLM 的 18 条历史切片可能移除 system/Persona；用户设置的追问偏好未传给外置 LLM；合法新插话取消旧轮时 `_finish()` 清空 `self.transcript`，新一轮被错误写成空用户问题；带 `item_id` 的重复 final 会再次调用 LLM；Provider 断线可能留下外部 LLM 后台流继续输出；工具只返回 `tool_calls` 时没有第二次无工具回复；TTS 已空队列的 finish 唤醒不能依赖每段额外创建的小任务。对应增加了假 Provider/假 LLM 回归。

| 子链路 | 当前真实代码 | 尚未交付，勿在统一测试中标 PASS |
| --- | --- | --- |
| 麦克风→Room | 原 Room `MIC_AUDIO` 20ms 帧；浏览器本地电平与设备名；单房间单音源前提不变 | Demo AudioWorklet capture、完整 AEC/回声声学验证、真人语音电平+ASR 对照 |
| Qwen-Audio→ASR/smart_turn | 可选听觉协处理器，`[text,audio]` Session；final 按 item_id 去重并驱动外置 LLM；工作台有 ASR 暂定/最终与 Session 计数 | Demo 的静默 heartbeat、ambient 有意义短句提升、精确超时恢复/在途音频缓存尚未迁移；现场 Provider 尚未验证 |
| 外置回复 LLM | 默认代码配置 `qwen3.7-flash`；流式 text、preview_action 工具模拟与工具-only 追问；系统 Persona/追问偏好保留，8 轮有界历史 | 模型端实际工具调用兼容性/回答效果未经真机 A/B；**真实机器人动作从未执行**，仅模拟结果，不得假称模型工具能力已验收 |
| LLM→TTS→Room | 单机器人独立 TTS 文字队列/背压；修复队列空时迟到 finish；协议仍向所属 `reb_tts` 发布 24k PCM/stop | **仍是逐短语新建 TTS WebSocket 任务**，未迁 Demo 连续双向 TTS Session/接续韵律；真人整句无断流待验 |
| 插话 | 新有效 ASR final 取消旧外置 LLM/TTS，旧回合记 cancelled，保留新文本身份；网页有 stop | **暂定 HOLD → turn_invalid 原位恢复 → 确认打断**尚未完成，`playback.hold_requested` 只是诊断事件；真机扬声器的 Hold/Resume 协议与准确回执待验 |
| 网页听音 | 显式开关订阅当前 Room 的 `reb_tts`，不增加第二 TTS，不冒充真机回执 | 仍逐帧 AudioBufferSource；正常积压超 500ms 会丢队列；Demo Worklet 的预缓冲/断粮重缓冲/可逆 Hold 尚未迁入，**不能当播放稳定性验收** |
| 网页工作台 | 聊天按 turn_id 合并，输入电平，ASR 暂定/最终，当前外置 LLM 型号/状态/错误，TTS/PCM/网页试听/设备回执，Chat/Persona/多轮复跑与开发者事件 | 页面计数≠真人 ASR/播出；没有完整声学回路的可视化，不能替代现场听觉实验 |

**统一测试建议（分离标记）：** 先仅开一个 Room 麦克风、一个 Room 调试扬声器（戴耳机），新 Chat 按「你好→你喜欢喝茶吗→不是茶是咖啡→继续刚才→我说话时试着打断」观察每句 `ASR final → LLM text.delta → TTS PCM → stop/drain`。手机/桌面、短句/长句、静音/噪声、重复 final 与多机器人切换分别记录；**暂不以可逆打断或实体扬声器播完为通过条件**，因为这两项代码尚缺。`127.0.0.1:8780` 本轮审查时不可连接，未重启或操作现场实例；运行时 `.env` 与模型可用性以现场显示为准。

## 2026-09-21 补充：三套陪伴表达风格及当前 Chat 热切换

- 上文有关串联外置 LLM 的记录已过时：当前代码按 `c99adb6` 恢复 Qwen-Audio Realtime 直接生成文本，本次只修改其会话级指令，不新增模型调用。
- `config/personas.toml` 保留 `annie/annie-calm/annie-lively` 三个 ID，改为自然陪伴、温柔知心、活泼俏皮；各自包含简短行为原则和虚构示例，示例不得当作用户历史。
- 原 `persona_id` 是 SQLite 历史/Chat 隔离键，切换它会改变历史查询。新增当前 Chat 的 `persona_style` 用户设置，只改变表达风格；旧 Ledger 和原身份不迁移、不清空。切换当前正在说话的机器人会停止当前回复、重建 Provider Session，直到新 `session.updated` / `session.ready` 才显示已生效。切换另一 Chat 或机器人不会串用风格。
- 网页会话顶部和设置弹窗可切换；无 Provider 密钥或连接失败时显示待生效，不把设置写入成功伪装为云端生效。
- Fake Provider 测试已验证新 Session 指令、原历史、返回旧 Chat 恢复旧风格和机器人隔离；真实 Qwen-Audio 的三人格表现、真人打断、声音体验仍待人工验收，自动化不签发语义质量 PASS。

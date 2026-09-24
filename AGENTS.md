# Annie V2开发入口

- 唯一开发根是本目录；原仓库及其brainv2副本只读冻结，不回写、不撤销已有修改。
- Git提交标题的说明部分和正文统一中文；`fix/feat`等类型、scope及技术标识可保留英文，推荐`fix(phase4b): 修复TTS播放节奏与嘴型同步`。提交前检查语言，只按用户安排提交本任务改动；不为改语言擅自重写历史。完整规则见PROJECT_RULES“Git提交语言”。
- 开始前只按`docs/README.md`给出的优先级读取当前真源；默认先读PROJECT_RULES.md、docs/规划/小安妮V2后端开发总计划.md、docs/规范/项目结构与配置规范.md。只有协议/兼容任务才继续读Phase 0。
- 根目录只保留必要入口/规则文件（README.md、AGENTS.md、PROJECT_RULES.md 等）；规划、进度、发现、交接和评审类 Markdown 必须归档到 docs/ 对应中文目录，文件名优先中文。
- 配置唯一规则：非敏感可提交配置进config/；机器本地覆盖/密钥进根.env（只提交.env.example）；Python/uv/pytest/ruff/SQLite/临时数据按PROJECT_RULES统一落位，禁止各模块自行发明目录。
- 启动器必须真实拉起进程和HTTP验收；`start.sh/start.bat`遇到同一Annie Harbor旧实例时先终止旧PID再拉起新实例，其他程序占用必须明确报错且禁止误杀，禁止静默换端口。
- 当前Phase 4B开发中（用户确认）；保持TTS/嘴型/打断实现继续。新增Phase 4C排在4B之后、Phase 5之前，完善网页基础、Phase 1–4B已有功能及布局/交互验收；不打断4B、不重做已有React工程、不倒填历史验收。Phase 4既有证据见`docs/phase4/2026-09-20/README.md`，4C任务卡见总计划第25节。
- V1外部配置资产已迁到config/与根.env：Provider/模型/Room/视觉/天气/动作等可直接作为后续接线输入；V1 `skills.json` 不迁入，能力在各阶段按最小契约重建；配置存在不代表能力已连接，禁止提前改health能力状态。
- 旧代码只证明已有外部协议和行为，不决定新模块结构；信息不足标待确认，不猜wire字段，不搬RobotBridge。
- apps/runtime负责未来bootstrap；annie-core无SDK/Graph依赖；LangGraph只进annie-brain；第三方SDK只通过annie-adapters接入。
- 前端只在web；Phase 4起原位升级正式React + TypeScript工作台，接入已有多会话/Persona/完整历史/20轮上下文/PCM与诊断，再接Realtime；后续阶段都在同一工作台增加功能。annie-web仍是后端Web接口边界；禁止新增第二份前端或Pipecat空壳。具体交付见总计划第22/25节；本条为规划，不代表已实现。
- sidecars/tts_viseme是未改造的旧listener副本；不向父目录import，不搬venv/模型/真实.env。
- 用户已确认：一机器人一独立Room，一个Room只归属一个机器人；A→roomv2、B→roomv3；各Room完整保留`main/tts/mic/visualdata/vision/sam`六通道且只有一路连续PCM输入。本期不做同房间多机器人/多音源分流；Phase 2只启用mic也不得删除其它五通道。不猜from/conversation_id为设备目标。
- Phase 1–5（含4B/4C）沿用`tests/html/`临时验收页；Phase 6及以后不再新增独立HTML测试/验收页，功能和诊断直接集成现有`web/` React工作台，网页验收也在工作台完成。自动化、WEB-01～08、浏览器及设备/人工验收仍须执行并记录阶段证据，页面可读性遵守PROJECT_RULES第7条。既有临时页不提前删除；按Phase 10最终整合安排、用户确认后清理`tests/html/`及挂载，后续不重建。
- 当前回归：`uv run --locked pytest -q`、`npm --prefix web test`、`uv lock --check`、`uv run --locked ruff check apps packages tests`；启动验收必须额外真实执行`sh start.sh`/空闲端口HTTP。

- 新架构：Room PCM → Audio Input Gate → Qwen-Audio 3.0 Realtime Flash（ASR/smart_turn/对话/Persona/Function Calling）→ 文本 → 独立Qwen-Audio-3.0-TTS-Flash → PCM；同一PCM+文本送ForcedAligner再映射嘴型。普通聊天禁止经过LangGraph；动作/表情/简单工具经Function Calling校验执行。
- 事件走Event Router + Speak Policy；需要发声才提交SpeakRequest，由TurnCoordinator协调，Realtime最终表达。LangGraph仅负责按需多步骤任务/状态/工具与结构化结果；Persona按Session/revision加载，不每轮重发。
- 后续以总计划为准：Phase 4先交付React工作台及现有会话/PCM功能，再验Realtime PCM输入/文本输出与工具契约，4B接独立TTS和最小ForcedAligner闭环，4C完成工作台基础/前序功能/布局整合，再增强Context/记忆；Phase 7完善真实动作/多设备，8接事件策略与按需LangGraph，9完善多头嘴型/观测。实时打断须同步外置TTS、嘴型与Realtime未播上下文。

- 验证产物按PROJECT_RULES第8条收尾：长期测试保留在tests/与web/tests/；一次性复核脚本/日志/测试库放var/tmp/<任务唯一标识>/，由执行者在任务完成前清理。未闭合或并行任务在用的证据只保留最小集合并注明清理条件；禁止删除整个tests/或扫空var/。

- Phase 4C加入GSAP + @gsap/react动效规范（总计划第22.6节），实施时精确锁版并验减少动效、组件清理与实时并发；不影响4B，不并装多套动画库，不用动画延迟业务或伪造波形。
- Phase 4起网页验收在规划阶段写入任务卡：遵守总计划第22.5节WEB-01～08及各阶段网页用例，明确入口/操作/预期/证据；构建或后端测试通过不等于网页验收通过，缺实测/人工确认保留待验。
- 阶段实施必须先读总计划前置效果表及第25节对应任务卡，按第32节模板复述目标、可见效果、输入输出、拟改文件、保留项、不做事项与验收；不把模块堆砌当目标，不自动跨阶段。
- 已确认业务事实不重复问；业务/协议/硬件语义仍不明、删除/迁移配置资产、换模型或扩大范围时必须先问用户具体问题，相关改动等答复。内部实现细节自行处理；未启用配置不等于可删除，KISS/YAGNI不得覆盖用户已确认能力。
- 测试PCM经过真实Room、物理麦克风、真实设备播放分别记证据，不代填人工验收；同Room测试麦克风与机器人音源避免同时发送。现有并行代码/配置改动保留，不为工作区干净清除或提交他人修改。

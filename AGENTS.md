# Annie V2开发入口

- 唯一开发根是本目录；原仓库及其brainv2副本只读冻结，不回写、不撤销已有修改。
- 开始前只按`docs/README.md`给出的优先级读取当前真源；默认先读PROJECT_RULES.md、docs/规划/小安妮V2后端开发总计划.md、docs/规范/项目结构与配置规范.md。只有协议/兼容任务才继续读Phase 0。
- 根目录只保留必要入口/规则文件（README.md、AGENTS.md、PROJECT_RULES.md 等）；规划、进度、发现、交接和评审类 Markdown 必须归档到 docs/ 对应中文目录，文件名优先中文。
- 配置唯一规则：非敏感可提交配置进config/；机器本地覆盖/密钥进根.env（只提交.env.example）；Python/uv/pytest/ruff/SQLite/临时数据按PROJECT_RULES统一落位，禁止各模块自行发明目录。
- 启动器必须真实拉起进程和HTTP验收；`start.sh/start.bat`遇到同一Annie Harbor旧实例时先终止旧PID再拉起新实例，其他程序占用必须明确报错且禁止误杀，禁止静默换端口。
- Phase 1最小骨架及本机审查收尾已通过。Phase 2真实Room PCM Adapter已接入：一个机器人绑定一个Room，Runtime按`room_id`自动连接对应`reb_mic`，roomv2/roomv3真实Java Room数据面隔离已验证；`tests/html/phase2.html`提供真实麦克风回环和多Room隔离人工验收。人工麦克风验收前不进入Phase 3。
- V1外部配置资产已迁到config/与根.env：Provider/模型/Room/视觉/天气/动作等可直接作为后续接线输入；V1 `skills.json` 不迁入，能力在各阶段按最小契约重建；配置存在不代表能力已连接，禁止提前改health能力状态。
- 旧代码只证明已有外部协议和行为，不决定新模块结构；信息不足标待确认，不猜wire字段，不搬RobotBridge。
- apps/runtime负责未来bootstrap；annie-core无SDK/Graph依赖；LangGraph只进annie-brain；第三方SDK只通过annie-adapters接入。
- 前端只在web；annie-web预留后端Web接口边界。禁止新增第二份前端或Pipecat空壳。
- sidecars/tts_viseme是未改造的旧listener副本；不向父目录import，不搬venv/模型/真实.env。
- 多头以robot_id隔离状态与取消；一个机器人只绑定一个Room，Room内`main/tts/mic/visualdata/vision/sam`六条通道都属于该机器人。Phase 2只启用`mic`输入，不把from/conversation_id猜成设备目标。
- 每个实现阶段自动化测试之外，必须在`tests/html/`提供当前阶段的可视化验收页；这些HTML和目录内README都是临时测试资产，所有阶段完成并进入最终整合后直接删除整个`tests/html/`目录。
- 当前回归：`uv run --locked pytest -q`、`node --test web/tests/dashboard.test.mjs`、`uv lock --check`、`uv run --locked ruff check apps packages tests/test_phase1.py tests/test_phase2.py`；启动验收必须额外真实执行`sh start.sh`/空闲端口HTTP。

- 新架构：Room PCM → Audio Input Gate → Qwen-Audio 3.0 Realtime Flash（ASR/smart_turn/对话/Persona/Function Calling）→ 文本 → 独立Qwen-Audio-3.0-TTS-Flash → PCM；同一PCM+文本送ForcedAligner再映射嘴型。普通聊天禁止经过LangGraph；动作/表情/简单工具经Function Calling校验执行。
- 事件走Event Router + Speak Policy；需要发声才提交SpeakRequest，由TurnCoordinator协调，Realtime最终表达。LangGraph仅负责按需多步骤任务/状态/工具与结构化结果；Persona按Session/revision加载，不每轮重发。
- 后续以总计划为准：Phase 4先验Realtime PCM输入/文本输出与工具契约，4B接独立TTS和最小ForcedAligner闭环，再增强Context/记忆；Phase 7完善真实动作/多设备，8接事件策略与按需LangGraph，9完善多头嘴型/观测。实时打断须同步外置TTS、嘴型与Realtime未播上下文。

- 验证产物按PROJECT_RULES第8条收尾：长期测试保留在tests/与web/tests/；一次性复核脚本/日志/测试库放var/tmp/<任务唯一标识>/，由执行者在任务完成前清理。未闭合或并行任务在用的证据只保留最小集合并注明清理条件；禁止删除整个tests/或扫空var/。

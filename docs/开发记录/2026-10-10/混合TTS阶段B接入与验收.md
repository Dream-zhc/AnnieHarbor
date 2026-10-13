# 混合 TTS｜阶段 B 接入与验收

## 交付边界

- 默认仍为原单任务双向流式 TTS。阶段 B 需要同时显式开启 `ANNIE_TTS_ENABLED=1`、`ANNIE_TTS_EXPRESSION_ENABLED=1`、`ANNIE_TTS_MIXED_MODE_ENABLED=1`；缺少表达解析或音色配置则启动时拒绝混合模式。
- 首句文本仍立刻进入现有 Qwen Audio TTS WebSocket 双向任务，**不等待首句结束、LLM 元数据或新增模型推理**。第一句收尾后关闭这个任务，后续每句用独立 WebSocket 完整文本一次提交；执行方式与 DashScope `SpeechSynthesizer.call()` 的单向调用对应：`enable_ssml=true`、一次 `continue-task`、`finish-task`。原始 WebSocket 的 `header.streaming` **按官方规定仍为 `duplex`**，不虚构 `simplex` 枚举。第二句起才附带通过校验的情绪标签与自然语言 `instruction`。
- 同一机器人只有 `SpeechOutput` 发布到 Room；后续 TTS 即使提前开始生成，也必须等待首任务的全部 PCM 发布完再往 Room 发送。所有帧由单一 24 kHz PCM 游标连续编号，同一个 `conversation_id`/`stream_epoch`，不同句子有独立 `segment_id`，`reb_tts.data.text` 保持清洗后的全量前缀；Sidecar 按既有 `segment_id` 对齐，不修改 ForcedAligner 和嵌入式协议。
- 语种、方言、音色锁定整轮；用户明确指定的表达风格优先于 LLM 句级标签。LLM `instruction` 校验后只用于后续句；不允许向工具、Chat 文本、动作字索引传递方括号元信息。
- 新连接全局最多 2 个并发后续句任务；单轮最多 16 个独立后续句任务，再多则合并成最后一次合成请求。句级任务首发失败且无 PCM 时仅重试一次默认风格；若已经发出任一 PCM 后失败则**终止整轮并发布 stop**，禁止重复播音。打断时取消 worker、全部活跃合成 socket，保留原 `tts.playback.stop` / `stream_epoch` 机制，不允许旧音频复活。
- 工作台语音状态区分 LLM 规划的原始情绪、双向首段实际参数、后续句最终实发 `instruction`/标签；逐轮诊断保存附加任务的 `task_id` 和实发参数，不以“已提交”冒充“设备已经播出”。

## 本地验证

- 2026-10-10 软件回归：`uv run --locked pytest -q` **845 passed，42 subtests passed**；`npm --prefix web test` **101 passed**，`npm --prefix web run typecheck`、`npm --prefix web run build`、`uv run --locked ruff check apps packages tests`、`uv lock --check`、`git diff --check` 均通过。
- 第一次全量运行曾出现未改动的 `tests/test_acoustic_sidecar.py::test_queued_expiry_during_connection_open_retires_all` 初始化竞态（`SlowSocket.instances` 尚未创建）；该单项和声学模块全套 25 项复跑通过，第二次全量回归通过。此处没有更改声学旁路，不把单次重试冒充确定性稳定证明。
- `tests/test_tts_mixed_mode.py` 包含多 token 切分、首音不等句末、后续句真实 run-task 字段与 PCM 游标、连续文本与 20ms 帧、同 Room 独立 segment、主动打断/后继轮 epoch、不播重复故障重试、双机器人互不串流、显式偏好优先、长句数上限与诊断状态。
- `tests/test_tts_expression_parser.py` 延续阶段 A 控制字段与 Chat 净化测试；原 `tests/test_phase4b.py` 覆盖关闭模式的兼容性。
- `web/tests/speech-expression.test.mjs` 校验 LLM 生成与音频实发不能混淆；`npm --prefix web run build` 确保正式工作台更新。
- 本地 Fake Socket / Fake Room 测试**不是**真实首音、音色听感、音频拼接无缝、ForcedAligner 全程验收或真机回执证据。

## 用户现场统一测试

1. 保持两个试验开关关闭，重新启动，同一句短文本/多句文本各 20 轮，记录工作台 `tts.first_audio`、`tts.first_published`、真机 `speaker_reported_started`、设备实际打断和 P95。
2. 仅在单机器人试验房间同时开启两个开关，先短句，再“先开玩笑、后认真提醒、最后安慰”的三句文本；确认首音不被元数据拖住、后续句真实情绪区别、朗读无标签、音频没有异常停顿或重复，嘴型与字索引无错位。
3. 边播后续句边打断，随后立即发新一轮；核对旧 `stream_epoch` 不继续发音，下一轮可以正常首播。再进行两机器人并发以及至少每模式 100 轮 P50/P95/P99、资源占用、云端费用、物理扬声器播放回执验证。
4. 若首音 P95/P99 超出原版、出现分句可闻断裂、表情标签被读出来或嘴型错位，则把 `ANNIE_TTS_MIXED_MODE_ENABLED=0` 并重启；不改变其余业务功能。

## 官方依据与已知限制

- [阿里 Qwen-Audio-TTS 客户端事件](https://help.aliyun.com/en/model-studio/qwen-audio-tts-client-events)：原始 WS 指定 `streaming=duplex`。
- [阿里实时语音合成指南](https://help.aliyun.com/zh/model-studio/realtime-tts-user-guide)：情绪和富语言标签仅支持**单向流式**，`enable_ssml=true` 的请求必须只提交一次文本；后续任务按官方 SDK `call()` 的方式构造。
- 尚未进行真实 DashScope 网络调用，服务端是否接受该组合、具体标签音质、首句与下一句的物理停顿长度，需要用户现场验证。**在这些数据到位前，不以静态回归宣布零延迟回退或量产通过。**
- 开工时仓库已有多项跨阶段未提交修改，与实时 TTS、前端文件有重叠；禁止在未隔离变更所有权时执行 `git add -A` 或混合历史提交。

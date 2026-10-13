# 情绪未生效与 TTS 异常｜2026-10-10

## 现场确认

- 服务进程启动前，本机 `.env` 只有 `ANNIE_TTS_ENABLED=1`，没有 `ANNIE_TTS_EXPRESSION_ENABLED`、`ANNIE_TTS_MIXED_MODE_ENABLED`；HTTP `speech-expression` 显示 `llm_expression_enabled=false`、`speech.mixed_enabled=false`。因此工作台显示的“随语义表达”仅是原单任务双向 TTS 的固定基础指令，不能当作 LLM 已生成情绪标记。
- 旧运行时 `annie-a` 保留一条 TTS 错误：`ExceptionGroup: unhandled errors in a TaskGroup (1 sub-exception)`。同一轮 provider 已 `task-started`、发送过 18 次文本、发布约 **996000 个采样（41.5 秒）**，最终没有收到 `tts.task_finished`。从最终已发布 PCM 到报错仅约 241ms，因此是长段合成后中断，非首音初始化失败。历史事件没有保留内部子异常和 provider `error_code`，**不能证明是某个特定云端限额**。

## 修正

- 仅在本机被 `.gitignore` 忽略的 `.env` 中开启上述两个实验开关，不把本机密钥提交或输出。启动服务后 HTTP `/health` 与 `/api/robots/{id}/speech-expression` 核实两个机器人均已启用；`/api/robots/annie-a/diagnostics` 的本轮 Provider `prompt_snapshot.instructions` 已包含新版“多句 TTS 情绪协议”。
- Realtime prompt 改为**首句立即说话、第二句前输出一次已验证 `overall` 和自然语言 `instruction`**；短回复无强制情绪标签，不为等标签增加首音等待。禁用及无模型标签时仍不伪造决策。
- `SpeechOutput._run()` 对 `TaskGroup` 内 text_submit/provider_receive/room_publish 子任务记录具体阶段与底层异常类别/原因；日志与 Web 事件脱敏敏感 token/endpoint；后续若报错可见真实子异常而非笼统 `ExceptionGroup`。无法由此推断此前已发生那次错误的根因。
- `ExpressionStream` 保存每轮最多 32 条有效 `overall`/`instruction`/句级标记与正文位置，保留原始 LLM 指令和拒绝原因。工作台同时区分 **LLM 规划** 与 **真实后续句云端参数**。只有后续句实际使用了经过验证的 `llm_sentence`/`llm_tag` 才报告 `tts_applied`；单纯存在后续句合成任务不算情绪实发。
- 保留单 Room PCM 24 kHz 采样时钟、stream_epoch、原双向首段、ForcedAligner、动作字索引与物理设备播放回执，不启用另一套发布器。

## 验证与剩余风险

- 修复后的专项回归：`tests/test_tts_mixed_mode.py` 与 `tests/test_tts_expression_parser.py` 合计 29 passed；与 Phase 4B 核验合计 58 passed；前端 `npm --prefix web test` **103 passed**，TypeScript typecheck、Vite build、Ruff 通过。
- 全量回归尚不能标记通过：一次完整运行 853 passed / 2 failed（长文本超限下一轮清理、Phase 5 空闲回收事件）；两项独立复跑通过，后续完整运行与另一个 pytest 进程同时运行导致长时间等待，已停止本任务卡住的测试进程，**不掩盖竞争/时序风险**。
- 采用脱离工具调用会话的独立进程启动，最终本机 `/health` 返回 200/ready=true，PID=98330；两个机器人状态 API 均显示 `llm_expression_enabled=true`、`mixed_enabled=true`，两者 diagnostics 的 Provider 提示词快照均包含新协议。
- `tests/test_tts_mixed_mode.py` 增加“默认双向任务真实异常展开”与“错误信息敏感令牌脱敏”回归，`tests/test_tts_expression_parser.py` 补标签位置记录；`web/tests/speech-expression.test.mjs` 核验界面能观察模型标签。
- 正在运行服务的 API 已确认实验开关与 session prompt，但**模型是否按提示输出、官方 TTS 后续句对标签的听感、真实首音 P95/物理扬声器/嘴型衔接仍需要真人测试**；不能把本地测试当作真实音频验收。
- 工作区原有多项未提交文件且 TTS/Realtime 正被并发修改。全量测试曾出现 `test_companion_speech` 与 `test_phase5` 的偶发顺序相关失败，两项单独复测通过；不得掩盖，也不得清理他人的变更。

## 用户复验要点

1. 浏览器强制刷新，工作台中“LLM 情绪协议”应非未启用；发送至少三句情绪明显不同的文字，检查标签日志、原始 `instruction`、后续句实际指令和 `tts_applied`。
2. 分别验证 1 句、短多句、长篇故事和未标记回复；出现错误时提供新增的 `provider_receive`/`text_submit`/`room_publish` 原因。
3. 在实际扬声器播放时测试首音、句间衔接、打断、新一轮恢复与嘴型同步；基于同文本比较 P50/P95/P99。失真或延迟回退时将 `ANNIE_TTS_MIXED_MODE_ENABLED=0` 并重启回到原双向链路。

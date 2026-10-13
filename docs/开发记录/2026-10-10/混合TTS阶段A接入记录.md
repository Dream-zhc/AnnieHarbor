# 混合 TTS｜阶段 A：增量情绪解析与观测

## 本次范围

- 依照根工作区 `tts_emotion_stream_demo.py` 的首句正文优先、随后方括号控制协议，增加 `speech_expression.py`。只从**现有** Qwen Realtime 的 `response.text.delta` 增量移除控制字段，净化正文继续发往原 Chat、TTS、动作消费者。
- 解析 `overall/language/dialect/pace/instruction/sentence_instruction` 及官方情绪/拟声标签；跨 token、无效枚举、过长指令、残缺标签测试覆盖。原文和校验拒绝结果独立于最终 TTS `run-task` 参数。
- 现有工作台语音状态页区分 LLM 原始 `instruction` 与 TTS 真正实发 `instruction`，并明确显示“已解析（未送入TTS）”。
- 配置 `ANNIE_TTS_EXPRESSION_ENABLED=0` 默认关闭。为 1 时仅打开模型输出协议与净化/观测，**不改变**主项目单个 TTS duplex provider task、音色、run-task 参数、Room 24kHz PCM/20ms 发布、stream_epoch、ForcedAligner、播放回执或打断管理。
- 不引入新热路径 LLM、第三方 SDK、额外 TTS websocket、音频发布器或语种偏好写入；独立 Demo 保持在根测试目录，不移入生产代码。

## 软件证据（2026-10-10）

| 检查 | 结果 | 限制 |
| --- | --- | --- |
| `uv run --locked pytest -q` | 834 passed，42 subtests passed | 假模型/本地合同，不是现场交互 |
| `uv run --locked pytest -q tests/test_tts_expression_parser.py` | 16 passed | 包含拆分 token、拒绝与双机器人隔离 |
| `npm --prefix web test` | 100 passed（包含 LLM 原始指令与 TTS 实发证据分离测试） | 不是 Chrome 真人互动 |
| `npm --prefix web run typecheck` | 通过 | 同上 |
| `uv run --locked ruff check apps packages tests` | All checks passed | 静态检查 |
| `uv lock --check`、`git diff --check` | 通过 | 无 provider / 真机证据 |
| 本机解析微基准，10万条短 delta | 约 0.85 μs/条 | 仅解释器本机，非首PCM/P95、非模型延迟 |

## 不能宣布通过的内容

1. **阶段 B 尚未实施**：后续句单向 TTS、句级自然语言 instruction/官方标签实发、任务并发排序、嘴型字索引重映射及失败降级均未接线；现阶段没有改善真实 TTS 听感的声称。
2. **阶段 C 待验**：现网 Realtime 模型是否按 Demo 协议稳定生成标签未知；至少每模式单头 100 轮、双头并发、P50/P95/P99 首音、物理扬声器回执及长时资源记录缺失。不因本地测试通过而放开默认开关。
3. **性能约束**：功能关闭时 `response.text.delta` 的文本/TTS发送路径保持原样；开启时仅做同步有限长度解析，没有额外 await。未用实测设备 E2E 数据证明零延迟回退。
4. **Git 隔离**：开工前 `main` 已有多批未提交 QSR、唤醒、音色与网页改动，且本任务修改与这些文件重叠；不得以一次 `git add -A` 把他人工作合并提交。单独分阶段提交需先隔离现有改动的所有权。

## 后续正式验收门槛

先证明 Realtime 输出协议稳定、没有元数据朗读/历史污染且首段不等待句末，再开发 opt-in 混合任务。阶段 B 单元与设备时间轴通过后，对同一文本、音色、语言做原主链与混合模式交替压测。P95/P99 未证实不回退则维持原双向 TTS 为默认。

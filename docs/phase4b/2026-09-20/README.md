# Phase 4B 阶段记录（未完成验收）

## 范围与现状

- Phase 4 物理麦克风 → smart_turn → ASR → 文字闭环仍待人工验收；用户已明确安排继续开发 4B，不倒填 Phase 4 PASS。
- 沿用 `config/providers.toml` 已有 `qwen-audio-3.0-tts-flash`、小安妮音色、24kHz，不改配置资产、不改 Realtime text-only、六通道、单机器人独占单 Room。
- 默认不向真实设备发布：仅本机 `.env` 显式 `ANNIE_TTS_ENABLED=1`，且 Realtime Key/Room 已配置，才创建每头独立 TTS 输出；生产网页不能自行开启 Provider 或持有 Key。

## 已实现与分层证据

- 独立 DashScope TTS WebSocket `run-task→task-started→continue-task→finish-task→task-finished`，binary PCM；与 Qwen Realtime 的 `session.update` 协议分离，保留单一 TTS 音频合成所有权。
- 单机器人有界分句队列、首片段等待、TTS 合成超时/失败可见；同一 PCM 按 24kHz S16LE mono/20ms 切片并按采样时钟限速，reb_tts 使用现有 JSON/Base64，不把输入 MIC_AUDIO BinaryFrame 当输出协议。
- 生成、首 PCM、Room 发布、设备已播（unknown）分开。Ledger 只写 `assistant_published_text`，无设备回执时不填写 `assistant_spoken_text`；停止帧只表示已发布停止请求，不能写“设备已停”。
- 取消/重建 Session 中止 TTS 与排队片段，带 generation 检查、按当前 Room 发布 stop；原有六通道与回合 Ledger 保留。
- React 工作台新增 TTS 状态/音频及嘴型证据说明；正式验收入口 `/phase-tests/phase4b.html`。
- 用已配置音色只做一次真实独立 Provider 测试（未推 Room）：输入“你好。”，得到 **53,760B binary PCM**，收到 `task-finished`。这不证明实际设备起播。
- 真实 Room 非播放握手：`roomv2/reb_tts` 与 `roomv3/reb_tts` 均返回身份匹配的 WELCOME。**没有发送任何实际 PCM/stop 到现场设备**，不冒充完整 Room 发布证据。

## 未完成 / 不得标记 PASS

- V2 主环境目前缺少 `numpy`、`mlx_qwen3_asr`、`qwen_asr`、`pypinyin`、`g2pw`；旧侧车仍为单头 listener，不能无改造直接声称 ForcedAligner 多头嘴型可用。不能用文本平均时长伪造 `mouth_script` 声学边界。
- 设备确切播放/停播回执、AEC 归属、实际音画时间映射与误打断还没有真实设备证据。未有回执仍记 unknown。
- 不擅自启用旧单头 sidecar，也不添加第二套合成链路。未验证物理麦克风、两头真实同时播放、真实停播和嘴型。

## 回归

    uv run --locked pytest -q
    uv lock --check
    uv run --locked ruff check apps packages tests
    npm --prefix web test
    npm --prefix web run typecheck
    npm --prefix web run build

本次最终自动化：Python **91 passed / 2 条既有依赖弃用 warnings / 25 subtests**；其中 Phase 4B 专项 6 passed，前端 4 passed；Ruff、uv lock、TypeScript、Vite 构建及 diff 检查通过。未做物理播放、AEC 或 ForcedAligner 人工验收。

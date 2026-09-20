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

## 播放回执与嘴型返工（本次）

- 设备团队执行协议：`播放端WebSocket协议.md`。同 Room 的 reb_tts 旧 PCM JSON 加 `conversation_id / stream_epoch / start_sample / end_sample / stream_final`；扬声器向 reb_main 发布 started/progress/stopped/drained，报告真实输出回调 24kHz 等价 `played_sample_count`，不是接受/入队 PCM 个数。取消/停止与真正停播的时间、样本游标分别记录。
- Runtime 每头独立订阅 reb_main 播放回执与 reb_sam 嘴型事件；拒绝串 Room/旧 turn 与 epoch/倒退 seq 或游标/超已发布范围；browser_test 与 speaker 分源。一份音频只合成一次，继续广播现有 reb_tts，老侧车只消费同一 PCM+权威文本，不调用 TTS。
- 已完成网页测试扬声器功能接入正式 React 工作台：用户手势启动 AudioContext、监听 reb_tts+reb_sam、发送 reb_main 的 browser_test 游标，监听 stop 清播放节点、嘴型按播放游标显示；关闭页面/断 WS 不伪造设备已停。真机回执目前 **没有现场证据**。
- 只有 speaker 报告完整 drained 且匹配 completed/完整样本，才把整段升格 `assistant_spoken_text` 和有效上下文；浏览器测试的 drained、部分 stopped、仅 Room 成功发送都不能认定扬声器已播放完整文本。设备消息属于未经认证的来源自报，今后若需强证明需在 Room 接入设备身份认证。
- 已增加按 Room 单实例启用旧 ForcedAligner listener 的开关 `ANNIE_VISEME_ENABLED=1`：每头独立进程/单例端口/房间 URL；缺 sidecar Python/依赖启动前明确失败，模型未输出 mouth_script 前不能标已对齐。**本机 sidecars/tts_viseme/.venv311 尚不存在，真实声学嘴型与物理停播/AEC 未通过。** 不擅自下载超大模型或启用真机扬声器。

本机需要模型和依赖时，在显式允许下载及确认磁盘空间后执行（Mac 示例，不能把这些依赖塞进主 Runtime）：

    uv venv --python 3.11 sidecars/tts_viseme/.venv311
    uv pip install --python sidecars/tts_viseme/.venv311/bin/python -r sidecars/tts_viseme/requirements-v3.txt

然后在根 `.env` 先开启 `ANNIE_TTS_ENABLED=1`；Sidecar 环境/模型可用后再开启 `ANNIE_VISEME_ENABLED=1`。Sidecar 首次加载可能下载模型；`sidecar_started_unverified` 只表示进程已启动，收到当前流真实 `mouth_script` 才表示有对齐数据。服务端写 `var/logs/viseme-<robot_id>.log`，不能泄漏 Provider 密钥。

## 未完成 / 不得标记 PASS

- V2 主环境目前缺少 `numpy`、`mlx_qwen3_asr`、`qwen_asr`、`pypinyin`、`g2pw`；旧 listener 已可按 Room 独立启动，但本机 Sidecar 环境/模型缺失，不能声称多头声学嘴型已通过。不能用文本平均时长伪造 `mouth_script` 边界。
- 设备确切播放/停播回执、AEC 归属、实际音画时间映射与误打断还没有真实设备证据。未有回执仍记 unknown。
- 默认不开启 Sidecar 或真机扬声器，亦不添加第二套合成链路。未验证物理麦克风、两头真实同时播放、真实停播和嘴型。

## 回归

    uv run --locked pytest -q
    uv lock --check
    uv run --locked ruff check apps packages tests
    npm --prefix web test
    npm --prefix web run typecheck
    npm --prefix web run build

本次最终自动化：Python **91 passed / 2 条既有依赖弃用 warnings / 25 subtests**；其中 Phase 4B 专项 6 passed，前端 4 passed；Ruff、uv lock、TypeScript、Vite 构建及 diff 检查通过。未做物理播放、AEC 或 ForcedAligner 人工验收。

本轮播放事件返工最终复核另行追加于下方，不覆盖前次 91 项的历史记录；浏览器真机实操和实际扬声器/AEC/模型声学对齐仍分别标待验。

本轮返工验证：Python **98 passed / 2 条既有依赖 warning / 25 subtests**，前端 **6 passed**；Ruff、uv lock、TypeScript、Vite 构建、diff 检查均通过。另以 TTS/Sidecar 关闭模式在独立端口真实启动 Runtime，React `/`、`/phase-tests/phase4b.html`、`/health` 均返回 HTTP 200，两个 Room 的 MIC 输入订阅正常，SIGTERM 优雅完成。此项**没有**打开真机音频、未作浏览器 GUI 试听、未下载 ForcedAligner 模型，待验项不倒填。

追加取消与发送并发边界：Room PCM 发送尚未返回时，停止帧仍预留正在发送的末尾样本，防止播放端真实收到了音频、却被不完整的停止游标拒绝；专项已回归。**最终完整回归 99 passed / 2 条既有 warning / 25 subtests，前端 6 passed，其他检查通过**。上段 98 是本次加测前的中间记录，不作为最终数。

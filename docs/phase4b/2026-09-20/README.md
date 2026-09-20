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

## 2026-09-20 用户明确授权开启 4B 试听

- 根目录机器本地、Git 忽略的 `.env` 已显式设置 `ANNIE_TTS_ENABLED=1`、`ANNIE_VISEME_ENABLED=1`，原密钥与其他本地配置未动。通过 `sh start.sh` 按已验证 Annie Harbor PID 替换旧 Runtime；HTTP 健康确认两头 Realtime connected、独立 TTS idle，4B HTML 经 Chromium 确认「开启浏览器测试扬声器」可点击、reb_tts 真 Room 已订阅、真实文字测试按钮在订阅后可点击。
- 新建项目内忽略的 `sidecars/tts_viseme/.venv311` 并安装原 `requirements-v3.txt`。修复 sidecar Python 的 `Path.resolve()` 跟随 uv venv/bin/python 链接跳出 venv、导致 preflight 误报依赖全缺的缺陷；专项加入虚拟环境符号链接回归。
- 从当前机器完整的 Qwen3-ForcedAligner 缓存**读取并克隆一份独立快照**到本项目 `var/models/huggingface`；未更改/依赖旧工程代码或旧模型目录，MLX 用项目内缓存离线预热通过。Sidecar 独立使用 `37759/37760`，避开仍由旧工程拥有的 `37659`，不误杀旧进程。模型加载和 G2PW 初始化可延迟，请以 `var/logs/viseme-annie-{a,b}.log` 的 reb_tts/reb_sam subscribed 与真实 mouth_script 作为各自验收证据。
- **未替用户提交真实语音或文字，也未把 Provider/Room 接线记成设备完整播放/声学嘴型 PASS**；监听已建立、但未收到真实 TTS 回合时的 viseme `not_connected` 和设备播放 `unknown` 均保持原语义。

本轮最终接线检查：服务 PID 58248，健康为 ready，annie-a / annie-b 的 Realtime 都是 connected、TTS 都是 idle；两条 sidecar 进程 running，日志分别确认 roomv2、roomv3 的 reb_tts/reb_sam **connected + subscribed**。浏览器打开 4B HTML 并实际订阅 reb_tts 成功，测试文字按钮已解锁（**尚未点发送**）；设备回执保持 unknown，嘴型未收到实际 mouth_script 前仍为 not_connected。回归：Python **102 passed**（2 条既有 warnings、25 subtests），前端 **11 passed**，类型检查/构建/uv lock/Ruff/diff 检查通过。Sidecar G2PW 启动有 8 秒预热超时提示（后台加载）；真实 PCM/TTS 试听与时间戳嘴型仍由用户本轮操作验收。

## Phase 4 返工：连续播放、工作台整合与实体闭环验收门槛

原 4B HTML 每 20 ms 新建一个 `AudioBufferSourceNode`，并以 `max(currentTime+80ms, plannedAt)` 排帧。真实 WS/TTS 抖动超过 80ms 时会产生时间空洞，可能造成用户听见的杂音、断裂；**这是已确认的播放端风险，并不证明用户那次实际杂音的唯一来源**。工作台和独立 4B HTML 现共用静态 `/pcm-playback-processor.js`：AudioWorklet 连续环形 PCM、启动前 120ms 缓冲、设备采样率插值、按实际消费样本发 `browser_test` 回执；单一真实 reb_tts 流，不合成另一轨。启动阶段直接 `AudioContext.resume`，避免浏览器用户激活失效。

工作台当前对话直接集成真实 Room 音频输出、音量、本地关闭、PCM 格式/采样游标/欠载/源峰值/削波样本以及实时 mouth_script。Phase 4B 独立 HTML 保留专项故障复现，不再要求正式对话跳转。端口、模型、声学侧车、外部 Room 协议不变。真实 Room PCM 格式锁定为 **Base64(raw 24 kHz mono S16LE)、每 960B/20ms、start_sample/end_sample 连续**，与 16k MIC_AUDIO 不得混用。Source 高峰值/削波提示上游失真；零削波而 Worklet 欠载意味着发布/网络抖动；两者都不是物理扬声器已正常的证据。

另一实体闭环边界：浏览器与物理扬声器同时订阅 Room 时，两个回执源需独立 seq/样本游标；之前 `browser_test` 先开始会锁定 `playback_source`，拒绝真实 speaker。现在各源分别校验，speaker 优先呈现，其真实 drained 才能写入 `assistant_spoken_text`；browser_test 永远不能替代真机回执。

**本阶段不得凭自动化 PASS 宣称最终 Phase 4 PASS。** 实体验收记录至少包括：三轮连续「真实麦克风 → ASR → Realtime → TTS → 物理扬声器 + reb_sam 嘴型」，麦克风和扬声器同时开时用户插话实际截断扬声器且嘴型停止、恢复下一轮；每轮收集原始 TTS 是否削波、浏览器欠载次数、真机播放报告、嘴型事件与实际延迟。浏览器音频截图与 `browser_test_reported_drained` 只证明浏览器试听，不证明外部扬声器/硬件 AEC、被打断后的喇叭已经停播。无真实回执则持续 unknown，Phase 4 状态保留「实体闭环待验」。

## Phase 4 稳定性返工：语音、转写与抖动

- 根据工作台截图，浏览器测试播放器出现欠载 8 次、源峰值 100%、削波 681 样本；这些是该轮观察值，不能据此单独判定麦克风回授或提供方音质为唯一根因。削波发生于收到的 PCM，网页调低音量不能修复已经削平的波形；已在正式工作台增加提示。
- TTS 发布允许最多约 240ms 的采样时钟提前量，避免刚好 1 倍速逐帧发布完全没有网络/调度抗抖动空间；保持 24kHz 单声道、唯一 TTS 产物和播放游标协议。浏览器 Worklet 不再因只剩一个有效插值样本而提前报告欠载。
- 不再逢逗号即启动独立 TTS 任务；短片段先合并，强标点优先，首段等待/最大段长适当调节。代价是极短非完整语句可能多等约 180ms；不宣称提供方语速、音色可由本次代码改动完全校准。
- 音频最终转写可纠正已有回合的部分文字，也可在模型回复结束后按原 turn_id 补写 Ledger 和有效上下文；网页显示暂定转写、最终转写，并刷新对话历史。原本缺字若来自提供方最终 ASR 自身仍需真语音证据确认。
- 本次回归：Python 107 passed、25 subtests、2 条既有第三方弃用 warning；前端 16 passed，TypeScript/Vite 构建通过。真实网页耳听、物理扬声器、麦克风全双工、AEC、连续回合欠载与削波复测 **待人工验收**；不得写 Phase 4/4B 完成。

## 2026-09-20 声音仍有杂音、启动后整机卡顿：再诊断及减负

- 新截图：89,520 已播样本、欠载 0、源峰值 100%、近满幅样本 386；麦克风仍在发送。之前的 240ms 发布余量改善了欠载，不代表消除了可听失真。
- 新增独立实测 `uv run --locked python tests/manual_tts_pcm.py`，仅发一次相同配置音色的 TTS API 调用，不经 Room、麦克风、浏览器或真机：62,640 样本/2.610s、peak=32767、近满幅样本 162。由此可确认至少部分近满幅来自 Provider 原始 PCM；近满幅不等同于全部杂音的声学根因。未收集可耳听音频，不能宣称已修复 TTS 原始音质。
- 本机 16GB 内存，历史 sidecar 日志显示两个独立 Qwen3-ForcedAligner-0.6B 模型分别预热、G2PW 单次最长约 35s，造成明显资源争用风险。本次将可选 G2PW 改为默认不加载（保留 pypinyin+ForcedAligner；有需求可设置 `TTS_VISEME_G2PW_ENABLED=1`）。新增 `ANNIE_VISEME_ROBOTS` 可按 robot_id 限制 sidecar 数量；本机忽略版本管理的 `.env` 已限定为 `annie-a`，**annie-b 仍保留 Realtime、TTS 与 Room 能力，但暂时不运行嘴型侧车**。如需两头同时声学嘴型，须移除该限制并接受双模型内存/算力开销或另行设计模型共享，不能冒充双头嘴型已通过。
- 不改变模型、音色、采样率或六通道合同；没有擅自把已经饱和的 PCM 降音量伪称去噪。本次没有在用户机器上完整复现“正在运行时”的 CPU/内存曲线：观察时 Runtime 已停止；仍需启动后实际对照资源和耳听判断是否达到稳定目标。
- 为排除 Room/浏览器/麦克风影响，第二次 Provider-only 取样另存原生 24kHz/mono/S16LE WAV 到本机 `var/tmp/phase4-tts-noise/provider-only.wav`：62,640 样本/2.610s、peak=32767、近满幅 148。该临时文件仅用于与用户网页播放耳听对比，问题关闭后删除；可复跑 `uv run --locked python tests/manual_tts_pcm.py --save-wav`（每次产生一次实际 TTS 调用）。
- 本轮回归 108 passed、25 subtests、2 条既有弃用 warning；前端 16 passed，类型检查与构建通过，uv lock/Ruff/diff check 通过。单侧车和无 G2PW 的真实启动资源曲线、Provider WAV 与网页声质对照仍待实测，不能宣称已消除卡顿或杂音。

# 云端 AEC｜小安妮 A 单头接线与验收

## 已接入的软件链路

Windows 2.4GHz USB 无线麦克风 → 唯一网页 Room `reb_mic` 原始 `MIC_AUDIO`（16kHz、s16le、mono、20ms、640 字节）→ BrainV2 唯一 `RoomPcmConnection` → `CloudAec`（独立线程，2×10ms WebRTC APM）→ 原有 `AudioInputGate` → sherpa-onnx 唤醒与 Qwen Realtime smart_turn。**禁止再建第二个麦克风发布者、第二个 PCM 上行或在 DSP 中异步等待 Room I/O。**

参考音频来自现有 `SpeechOutput` 已成功发往 `reb_tts` 的 `sample_cursor_v1`、24kHz PCM 副本；该副本仅进入本进程有界参考缓冲，**不二次发布 TTS**。沿用上传的 `aec_room_bridge/aec_backend.py` 对 `aec-audio-processing 1.0.1` 的 WebRTC APM 绑定和 `resample.py` 的 `soxr` 流式转换。没有复用旧项目的本地 PortAudio 播放器、`transactional_v1` 去重或 `reb_main` 旧回执格式。

## 实体扬声器必须回传的进度

只有原有 `playback_receipt` 验证通过、且 `source=speaker` 的 `tts.playback.*` 状态才能推动云端 AEC；网页 `source=browser_test` 或旧代次/错误事务的回执一律不能替代实物。进度必须描述**声卡实际消耗的采样数，而不是写入缓冲区的数量**。

```json
{
  "type": "tts.playback.progress",
  "data": {
    "protocol_version": 1,
    "conversation_id": "本轮 TTS conversation_id",
    "stream_epoch": 12,
    "played_sample_count": 48000,
    "sample_rate": 24000,
    "seq": 23,
    "source": "speaker",
    "sender": "speaker",
    "playback_id": "设备本次播放唯一 ID"
  }
}
```

- 建议**约 100ms 一次**上报 `tts.playback.progress`；当前处理窗口最多接受**850ms 内**的回执。`started`、`held`、`resumed`、`stopped`、`drained` 应在实际生效时立即发送；`held`/`resumed` 需要已下发的 `command_seq`。这些报文在现有 `reb_voice` 或既有 `reb_main` 被同一个 BrainV2 运行时验证，不另建 WebSocket 监听器。
- 新播放必须更换播放 ID 或音频代次，`seq` 单调递增，`played_sample_count` 不能回退或超过实际本次音频帧数；冻结、渐弱、停止必须依据实体播放结果反馈。
- 保留最近两个有界 TTS 参考时间轴：**发送了下一轮 TTS 不代表实体扬声器已切换**。只有下一轮实体 `started/progress/resumed` 回执验证后才将 AEC 参考切换到新时间轴；迟到的上一轮 `stopped` 不会错误停止当前参考。
- 服务端用最后一份已确认的实体播放游标及到达时间估计参考播放头，允许有界 ±10ms/20ms 的微调，再由 APM 估计残余回声延迟。`ANNIE_CLOUD_AEC_SYNC_OFFSET_MS` 是**有待现场校准**的跨设备粗对齐量，不能凭此宣称已经精确同步不同设备时钟。

## 行为及失败边界

1. 参考已验证且落在当前有界播放缓存内：每 20ms 进行 2 次 APM 10ms 处理，交付 640B 清洁 PCM。
2. 没有 TTS、扬声器暂停或结束：原始 PCM 直通同一个 InputGate，确保用户仍能唤醒/追问，不冒充 AEC 处理。
3. 扬声器未提供有效回执、报告超过 850ms、参考失配或处理异常：**显式记为旁路**，原始 PCM 仍沿唯一原始通路传递，不谎称消回声；当前尚不能保证此时不会误打断，硬件必须补齐回执后才验收。
4. mic Room 断线、Chat/Persona 代次更换、机器人停止：清空参考播放头/会话身份，旧异步输出不能进入新代次。
5. DSP 线程和输入队列均有界，满载时优先丢掉陈旧工作并累加掉帧计数，不在 Room、sherpa-onnx、Qwen 事件循环等待原生处理。

## 部署与观察

依赖 `aec-audio-processing==1.0.1`、`soxr` 已进入 `apps/runtime/pyproject.toml` 和 `uv.lock`；`uv sync --locked` 安装。已在 `.env` 为 **annie-a** 开启：

```text
ANNIE_CLOUD_AEC_ENABLED=1
ANNIE_CLOUD_AEC_ROBOTS=annie-a
ANNIE_CLOUD_AEC_SYNC_OFFSET_MS=80
ANNIE_CLOUD_AEC_DELAY_MS=0
```

`GET /api/robots/annie-a/inputs` 的 `aec` 字段及数据大屏显示：`status`、`received`、`processed`、`bypassed`、`missing_reference`、`drops`、`errors`、`receipts`、`speaker_played_samples`、`speaker_receipt_age_ms`、`processing_p95_ms`。其中 `reference_aligned` 表示软件参考时序可用，不是声学消除质量已经达标的证明。

浏览器 `getUserMedia` 已请求 `echoCancellation:false`、`noiseSuppression:false`、`autoGainControl:false`，AudioWorklet 增益固定为 1.0。2.4GHz USB 接收器自身可能已有硬件 DSP，网页开关不能关闭硬件内部处理。

## 验收步骤（待真实扬声器与 Room 接入后执行）

1. 扬声器独立播放 30 秒，核查 `receipts` 持续增加、`speaker_receipt_age_ms` 不长期大于 850ms、`processed` 在播放时上升、`missing_reference` 接近零。
2. 麦克风保持打开；仅机器人 TTS 播放时检测误唤醒及误打断，再测试人类 20 次真实插话的识别、暂停/取消和实时延迟。
3. 分别比较云端 AEC 开/关下的原始/处理音频（现场经同意的录音），检查人声保真、回声衰减以及双讲是否被抑制。**不能仅凭输出能量降低证明消回声**。
4. 改变扬声器音量、USB 无线麦克风位置、网络抖动；测试暂停、恢复、网络断线重连与会话切换，检查 AEC 有没有继续喂旧轮次的参考。
5. 至少长时间运行并复测处理线程队列、`processing_p95_ms` 和 Room 输入时效性。若需要进一步自动时钟漂移补偿，基于上述真实数据增量调整，先不引入额外声音发布链路。

播放进度消息目前沿用 Room 原有 `source`/`sender` 校验和 TTS 事务验证。它们不是对物理扬声器的加密身份认证；部署时应隔离 Room 发布权限，只允许可信设备使用 `source=speaker` 上报。非可信 Room 应增加独立设备身份校验，不能仅凭 JSON 字段判定来源。

## 结果边界

已验证原生 APM 可构建调用、参考重采样、回执验证/过期旁路、单路 Room 入口与清洁 PCM 出口、软件代次隔离。全项目 **413 项 Python 测试、44 项前端测试、Chromium 浏览器回归通过**；WebRTC APM 本机 500 次静音 10ms 帧微基准的 p95 约 **0.119ms/帧**（仅 native DSP 处理，不含 Room、重采样、声学消回声效果；非物理实测）。**尚无嵌入式真实 DAC 进度与远端麦克风声学实测**；不能宣称稳定消除已经通过物理验收。

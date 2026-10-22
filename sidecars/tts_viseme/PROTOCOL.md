# TTS Viseme sidecar 协议

其他项目接入、双向心跳、离线判定、**单进程双房间组**及**附加房间（默认同时监听 room/roomv2/roomv3）**配置，请先看同目录 `外部项目接入协议.md`。

独立旁路模块。主项目负责 TTS 播放；本模块只听真实房间、只发嘴型。

默认房间：

```text
ws://192.168.1.47:8090/robot/room/reb_sam
ws://192.168.1.47:8090/robot/room/reb_tts
```

## 禁止事项

- 本模块不得再调用任何 TTS API，不得生成第二份 PCM。
- 不得把 PCM 发到 `reb_sam`。
- `at_sample` 必须来自 `reb_tts` 真实 PCM sample clock。

## reb_sam：主项目流式正文

主项目发送：

```json
{
  "type": "llm_delta",
  "sender": "robot_brain",
  "initiator": "main_brain",
  "conversation_id": "conversation-xxx",
  "turn_id": "turn-xxx",
  "seq": 12,
  "text": "常",
  "final": false,
  "ts": 1787041746190
}
```

流结束必须再发：

```json
{
  "type": "llm_delta",
  "sender": "robot_brain",
  "initiator": "main_brain",
  "conversation_id": "conversation-xxx",
  "turn_id": "turn-xxx",
  "seq": 23,
  "text": "",
  "final": true,
  "ts": 1787041746300
}
```

`llm_delta` 只用于提前缓存 / 提前 G2P。最终和 PCM 对齐时，以 `reb_tts.data.text` 为权威文本。

## reb_tts：真实播放 PCM

主项目现有帧（不要改播放协议）：

```json
{
  "type": "tts",
  "ts": 1787041746190,
  "data": {
    "audio": "<base64 pcm>",
    "format": "pcm_s16le",
    "sample_rate": 22050,
    "channels": 1,
    "text": "实际 TTS 文本",
    "seq": 12,
    "final": false,
    "turn_id": "...",
    "conversation_id": "...",
    "source": "..."
  }
}
```

模块必须：`JSON -> data -> base64 decode audio -> PCM S16LE`。

打断 / 停止至少识别：

```text
data.stop == true
type == tts.playback.clear
conversation_id 变化
stream_epoch 变化
```

## reb_sam：嘴型输出

模块输出 `mouth_script`（逐音节）和整段结束时的 `mouth_sync_summary`。`mouth_script` 字段保持兼容：

```json
{
  "type": "mouth_script",
  "sender": "stream_room_v7",
  "initiator": "tts_viseme",
  "stream_id": "tts-viseme-v7-62ad021b72",
  "seq": 20,
  "script_id": 1,
  "viseme": "a",
  "char": "常",
  "pinyin": "chang2",
  "final": "ang",
  "at_sample": 121867,
  "at_ms": 5526.85,
  "start_sample": 121867,
  "end_sample": 123200,
  "duration_ms": 60.45,
  "lead_est_ms": 4757.2,
  "timing_source": "qwen3_forced_aligner_online",
  "pronunciation_reason": "consensus",
  "conversation_id": "...",
  "turn_id": "...",
  "from": "anonymous-db59dd0b",
  "ts": 1787041746190,
  "room": "main"
}
```

`timing_source`：在线闭合为 `qwen3_forced_aligner_online`，TTS `final` 冲刷尾音节为 `qwen3_forced_aligner_final`。时间边界由 `Qwen3-ForcedAligner-0.6B` 给出；G2PW 只负责字音/viseme 映射，不再估算时间。

嵌入式触发条件：

```text
played_sample_count >= at_sample
```

## 发送节奏

闭合一个音节就入队一条 `mouth_script`，不要等窗口攒一波。房间发送按 `at_sample` 时间轴逐条放行：第一条立刻发，后续保持音节间隔。打断 / `tts.playback.clear` 时丢弃未发送队列。整段 TTS `final=true` 后再发 `mouth_sync_summary`。

TTS `final` 后再发一条观测汇总（不进主链，观测台「口型对齐」层用）：

```json
{
  "type": "mouth_sync_summary",
  "sender": "stream_room_v7",
  "initiator": "tts_viseme",
  "conversation_id": "...",
  "turn_id": "...",
  "stream_id": "tts-viseme-v7-62ad021b72",
  "audio_total_ms": 1820.4,
  "seq_count": 12,
  "seq_chars": "阿姨今天",
  "llm_chars": "阿姨今天",
  "tts_chars": "阿姨今天",
  "expected_syllable_count": 4,
  "mouth_event_count": 4,
  "duration_ms_sum": 1640.0,
  "first_mouth_start_ms": 80.0,
  "covered_ms": 1640.0,
  "internal_gap_ms_sum": 0.0,
  "max_internal_gap_ms": 0.0,
  "idle_gap_count": 0,
  "idle_gap_ms_sum": 0.0,
  "coverage_ratio": 1.0,
  "last_mouth_end_ms": 1720.0,
  "speech_active_end_ms": 1720.0,
  "duration_vs_last_end_ms": 0.0,
  "duration_matches_last_end": true,
  "max_single_viseme_ms": 265.0,
  "max_single_viseme_char": "天",
  "max_single_viseme_viseme": "e",
  "over_800ms_count": 0,
  "synthetic_stretch_count": 0,
  "forced_fill_ms": 0.0,
  "unaligned_tail_ms": 0.0,
  "alignment_source": "qwen3_forced_aligner_0.6b+g2pw+vad_idle",
  "dropped_count": 0,
  "dropped_chars": []
}
```

权威音节数来自 `reb_tts.data.text` 规范化后的汉字。`final` 后优先做到 `mouth_event_count == expected_syllable_count`，但**不能为了凑齐数量伪造时间边界**。如果真实 PCM 暂时/最终不足以支持某个边界，允许保留 `dropped_chars` / `unaligned_tail_ms` 供观测；这比把剩余时长平均切分、塞给最后一个字或强制拉到 `speech_active_end` 更正确。

每个 `mouth_script` 的 `start_sample/end_sample` 必须来自真实 PCM 声学边界，`duration_ms` 只能由 `(end_sample-start_sample)/sample_rate` 派生，禁止 clamp、平均摊时长或改音频播放速度。已经真正发送/播放的过去事件不可回改；尚未发送的未来尾巴允许随更多 PCM 用同 `stream_id + seq` replacement 重算。

真实发音之间可以存在静音/停顿，因此不再要求 `mouth[n].end_sample == mouth[n+1].start_sample`，也不再把 `last_mouth_end_sample == speech_active_end_sample` 当硬验收条件。静音不能归到上一个汉字/viseme；`internal_gap_ms_sum`、`coverage_ratio`、`duration_matches_last_end` 只用于观测，不能为了让它们“全绿”修改声学边界。硬失败指标是 `synthetic_stretch_count != 0` 或 `forced_fill_ms != 0`。嵌入式调度仍按 `played_sample_count >= start_sample`，不要拿 `duration_ms` 累加成第二套播放时钟。

当前实现保留 VAD 作为语音活动/尾静音观测，并把相邻 ForcedAligner 字边界之间 `>=240ms` 的空档统计为 `idle_gap_*`。这些 idle 区间不生成伪造口型，也不会被并入前一个字。在线对齐采用有限音频/文本滑窗；轻微的相邻边界重叠保持原始时间戳，不做裁剪或平移。

## 自消费保护

`llm_delta` 与 `mouth_script` 同房间。listener 只处理 `llm_delta`，必须忽略自己发出的 `mouth_script`。

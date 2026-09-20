# 播放端与小安妮 V2 的 WebSocket 音频事件协议 v1

适用：一机器人独占一 Room；A=roomv2，B=roomv3。每个 Room 仍有 main / tts / mic / visualdata / vision / sam 六通道。**协议帧本身不提供认证，Room 连接归属不能替代设备身份认证。** `source=speaker` 是播放端自报，UI/账本只能写 `speaker_reported_*`，不能据此宣称已做声学测量。

## 1. Brain → Room reb_tts：唯一音频真源

沿用 `type=tts`、`data.audio` 为 Base64 的 **24kHz / S16LE / 单声道** PCM，20ms 完整帧 960B，最后可短帧。勿改成 MIC_AUDIO BinaryFrame。片段 final 只结束当前合成片段；流结束另发空 PCM 的 `stream_final=true`。

```json
{
  "type": "tts", "ts": 1780000000000,
  "data": {
    "audio": "<base64 PCM>", "format": "pcm_s16le",
    "sample_rate": 24000, "channels": 1,
    "text": "你好。", "seq": 1, "final": false,
    "conversation_id": "<turn_uuid>", "turn_id": "<turn_uuid>-1",
    "source": "robot_main_brain",
    "playback_protocol": "sample_cursor_v1", "protocol_version": 1,
    "stream_epoch": 1, "start_sample": 0, "end_sample": 480,
    "audio_total_samples": 480
  }
}
```

`start_sample` 包含、`end_sample` 不包含；采样点自本轮第一个 PCM 起累计，不按每片段重置。相同 `conversation_id + stream_epoch` 下，下一帧 `start_sample ==` 上一帧 `end_sample`；声学嘴型 `mouth_script.at_sample` 也使用此坐标系，触发条件 `played_sample_count >= at_sample`。不能以 PCM 发布时刻或文本字数代替扬声器播放时钟。

停止指令使用现有消费者支持的 `type=tts, data.stop=true, data.final=true`，并携带停止的 `conversation_id`、`stream_epoch`、`reason` 和上次累计样本位置；**收到停止指令应立即废弃该 epoch 的未播队列并请求停止/清空 DAC 缓冲**，仅接收到 stop 不得立刻虚报 stopped。旧 epoch 的迟到音频不再入队。正常结束 `stream_final=true` 不等于实际播放完毕。

## 2. 扬声器 → Room reb_main：真实播放游标报告

播放端单独连接 **同一 Room 的 `reb_main`**，在 WebSocket WELCOME 后上报。Brain 订阅 `reb_main`，校验所属 Room 与当前/待停止的 `conversation_id + stream_epoch`。回执必须包含以下字段；`seq` 为同一 `playback_id` 下递增正整数，`played_sample_count` 递增且不超该流已发布采样总量。

```json
{
  "type": "tts.playback.started",
  "data": {
    "protocol_version": 1,
    "conversation_id": "<turn_uuid>",
    "stream_epoch": 1,
    "playback_id": "speaker-session-unique-id",
    "source": "speaker",
    "sample_rate": 24000,
    "played_sample_count": 0,
    "seq": 1
  }
}
```

| type | **何时**发 | played_sample_count |
|---|---|---|
| `tts.playback.started` | **第一帧真正提交到扬声器输出回调 / DAC 播放时钟已启动**，不是收到 PCM 或入播放队列时 | 首个开始位置，通常 0 |
| `tts.playback.progress` | 输出回调持续消费，建议每 40–100ms（及关键嘴型边界）上报 | 实际消耗的**累计** 24kHz 等价采样数 |
| `tts.playback.stopped` | stop 执行后，待播缓存已清、扬声器输出确实停止；必须在清队列/驱动停止**之后**发 | 截断时真实已播累计采样数；允许小于已发布总量 |
| `tts.playback.drained` | 收到 `stream_final=true` **且最后样本已从输出设备播放完成**之后发 | 必须等于该流最终已发布采样总量 |

如设备以 48kHz 输出，按真实硬件消耗样本映射到 **24kHz 等价** 游标，不得按收到多少 bytes、调度任务运行时间或网络到达时间计算。重采样/缓冲路径须在驱动层给出最终消耗点。报告的 started/stopped 仍是**驱动层证据**，不是麦克风声学实测；网页 `source=browser_test` 只代表浏览器 AudioContext 进度，不代表机器人扬声器。

`stopped` 可附可选 `reason`（如 `cancel`/`session_switch`），但不得因此改变严格样本字段。Room 断网、页面关闭、超时不构成任何停止回执；Brain 维持 unknown/pending，不能凭超时标记停止成功。若设备无法完成停止，应报告真实错误到设备日志，不得发送伪造 `stopped`。

## 3. Brain 内部状态/账本及迟到保护

`tts.stop_requested`（本地取消）、`tts.stop_published`（已向 reb_tts 成功发 stop）、`tts.playback.stopped`（reb_main 收到设备自报停止）分别记录时间；不得合并。`tts.playback.started/progress/drained` 一样由回执推动，不由 Room.send 成功推断。

旧 cid/epoch、重复/倒退 seq、倒退游标、越过已发布样本、不同 playback_id/source 的冒名游标均拒绝。浏览器与扬声器身份不能混作一条流的证据。只有扬声器自报 **整个流 drained** 且相符的 completed Ledger，才可能填 `assistant_spoken_text`；部分停止没有逐字声学边界，已生成未播文字不得写为已说出口。实际扬声器事件尚未验收时仍为待验。

## 4. ForcedAligner Sidecar → Room reb_sam

沿用 `sidecars/tts_viseme` 原有 listener：**它只听 reb_tts 同一份真实 PCM + 权威 data.text**，只生成 `mouth_script` 并向所属 Room 的 reb_sam 发布，不调用第二次 TTS，不把嘴型当作重新合成的音频。每机器人分别使用 `--tts-url` / `--sam-url` 与不同 `TTS_VISEME_SINGLETON_PORT`。Brain 和网页消费者以当前 cid、24kHz 累计 sample cursor 筛选，不能用基于文字平均时长的占位脚本充数；打断/新轮丢弃旧 epoch 和等待中的口型。

默认关闭：`ANNIE_VISEME_ENABLED=1` 才尝试启动；必须先安装本地 Sidecar 的依赖、ForcedAligner 模型与 Python 解释器；模型未就绪、算法未真正输出 `mouth_script` 时 health 不能标已对齐。真正扬声器应将 `mouth_script.at_sample` 映射到同一输出游标驱动表情，而不是按 Brain 发送时间执行。

## 5. 网页联调（非真机）

正式工作台选机器人 →「开启网页测试扬声器」（浏览器显式用户手势）→ 根据 Room reb_tts PCM 播放，用 AudioContext 游标在 reb_main 上报 `source=browser_test`；订阅 reb_sam 的 `mouth_script`，按相同游标显示口型；按「取消机器人回复」观察 `stop_requested → stop_published → browser_test_reported_stopped`。浏览器关闭/WS 断开时维持 unknown，不冒充停止回执。网页测试时避免同 Room 真机扬声器同时放音和麦克风自回授。

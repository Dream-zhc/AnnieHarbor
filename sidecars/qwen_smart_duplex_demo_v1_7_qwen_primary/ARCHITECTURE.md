# Demo Architecture v1.7

## 主链路

```text
Browser microphone
  ├─ browser AEC / noise suppression
  ├─ 20ms PCM16 → Qwen-Audio Realtime smart_turn
  └─ local dBFS / SNR → metrics only

Qwen-Audio transcript
  → external streaming LLM
  → adaptive phrase chunker
  → Qwen-Audio-TTS duplex stream
  → stateful 24kHz → AudioContext-rate resampler
  → reliable jitter queue
  → AudioWorklet
  → GainNode
  → speaker
```

## Qwen 主导打断状态机

```text
PLAYING
  │
  ├─ local dBFS/SNR spike
  │     └─ no state change
  │
  └─ Qwen input_audio_buffer.speech_started
        ├─ GainNode → 0
        ├─ Worklet → HELD
        ├─ playback head stops
        └─ preserve all queued and incoming TTS
               │
               ├─ turn_invalid / ambient filler
               │     └─ RESUME from exact sample position
               │
               └─ stable ASR prefix / valid final
                     ├─ generation barrier
                     ├─ clear entire playback queue
                     ├─ cancel external LLM
                     └─ cancel external TTS
```

`input_audio_buffer.speech_started` 是 Qwen 云端 VAD 事件，不等于有效语义轮次。有效性由后续 `turn_invalid`、ambient 事件、ASR 增量和 Final 共同裁决。

## 为什么暂停时不能裁剪队列

旧逻辑：

```text
HELD → keep latest 240ms → resume
```

如果 Hold 是误判，暂停期间生成的中间音频已经被删除，恢复后必然缺字或跳句。

v1.7：

```text
Qwen HELD → preserve exact queue → resume or clear
```

暂停通常只持续几百毫秒；即使 TTS 在此期间继续生成，30 秒可靠队列也足以容纳。有效插话一旦出现稳定 ASR 前缀，队列立即清空。

## 正常播放状态机

```text
GENERATION START
  → PREBUFFERING (default 180ms)
  → PLAYING
       ├─ burst audio → append without normal drops
       ├─ starvation → REBUFFERING (default 100ms)
       │                 → PLAYING
       ├─ Qwen hold → HELD without consumption or trimming
       └─ source_finished + queue empty → ENDED / drained ACK
```

## TTS 指标分解

```text
run-task
  │
  ├─ waiting for LLM/chunker
  │      └─ first continue-task text
  │
  └─ TTS synthesis
         └─ first binary PCM
```

页面分别显示：

- task → first text；
- first text → first audio；
- task → first audio；
- audio delivery speed；
- player underflow count。

只有发生播放器 underflow/rebuffering，才能说明音频下发没有持续跟上播放。

## 自适应短语切分

```text
first phrase:
  early natural comma after >= 6 chars
  otherwise target ≈ 10 chars

following phrases:
  natural comma after >= 10 chars
  otherwise target ≈ 28 chars
```

首段较短以降低启动延迟，后续段较长以维持韵律上下文。

## 事件顺序

后端文本事件和二进制 PCM 使用同一个发送锁：

```text
playback.started
binary PCM ...
binary PCM ...
playback.source_finished
```

浏览器仅在 Worklet 真正耗尽队列后发送 `client.playback.drained`。TTS `task-finished` 只表示云端不再产生音频，不代表扬声器已播完。

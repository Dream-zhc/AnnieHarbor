# Qwen Smart Duplex Demo v1.7

这是一个浏览器端全双工级联语音 Demo：

```text
浏览器麦克风（AEC / 降噪）
  → Qwen-Audio-3.0-Realtime-Flash（ASR + smart_turn）
  → 自定义流式 LLM（默认 qwen3.7-flash）
  → Qwen-Audio-3.0-TTS-Flash（双向流式）
  → AudioWorklet 可靠播放
```

Qwen-Audio 负责听觉和轮次裁决；最终文本来自外部 LLM，最终声音来自独立 TTS。

## v1.7 修复了什么

出现以下日志时：

```text
本地候选插话：-43.0 dBFS / SNR 61.0 dB
候选插话：local，本地立即静音
推测性静音期间仅保留最近 240ms 音频
拒绝打断并续播：local_release
```

旧逻辑会在本地噪声触发后冻结播放器，同时把暂停期间的 TTS 队列裁剪到 240ms。即使随后判断为误触发并恢复，回答中间的大量音频已经被永久丢弃，因此表现为：

- 一句话没有播放完；
- 突然跳到句尾；
- 没有用户插话却像被打断；
- 播放听起来断续。

这不是 Qwen-TTS 没有生成完整音频，而是本地 speculative hold 主动删掉了音频。

## 默认打断策略：Qwen smart_turn 主导

v1.7 默认：

```env
BARGE_MODE=qwen
```

行为如下：

```text
本地 Mic dBFS / SNR
  → 只展示指标，不改变播放器

Qwen input_audio_buffer.speech_started
  → 立即暂停播放器
  → 完整保存已排队和后续到达的 TTS
  → 暂不取消 LLM/TTS

Qwen turn_invalid / ambient（嗯、啊、噪声）
  → 从原采样点继续播放
  → 不缺字，不跳句

Qwen 稳定 ASR 前缀 / 有效 Final
  → 清空播放器
  → 取消旧 LLM
  → 取消旧 TTS
  → 启动新一轮
```

本地阈值仍显示在页面中，方便观察 AEC 和环境噪声，但在 `qwen` 模式下不会触发暂停、静音或清缓存。

### 其他 A/B 模式

| 模式 | 行为 |
|---|---|
| `qwen` | 推荐。Qwen 起声时暂停并完整保留；语义确认后再硬切 |
| `cloud-hard` | Qwen `speech_started` 立即永久清空，最快，但“嗯/啊”也可能截断 |
| `hybrid` | 旧本地候选 + 云端硬切，仅用于对照 |
| `semantic` | 旧语义 Hold 对照 |
| `hard` | 本地候选即永久清空，误杀风险最高 |
| `duck` | 旧版降低音量 |

## TTS 延迟与“是否跟不上播放”

旧页面的：

```text
TTS run → 首音频
```

不是纯 TTS 模型延迟。`run-task` 在 LLM 开始之前已经启动，因此该指标同时包含：

1. 等待 LLM 首字；
2. 等待文本累积成首个可播短语；
3. TTS 收到首段文字后的合成延迟。

v1.7 把它拆成：

- **TTS task → 首文本提交**：主要反映 LLM 和分句器等待；
- **TTS 首文本 → 首音频**：更接近 TTS 模型真实首包延迟；
- **TTS 音频下发速度**：`2.0×` 表示音频以约两倍实时速度下发；
- **播放器断粮次数**：只有这个数字增加，才说明 TTS/网络一度跟不上播放。

如果日志没有 `REBUFFERING`，断粮次数为 `0`，则不能仅根据“首音频 600~700ms”判断 TTS 跟不上实时播放。

## 自适应分句

默认建议：

```env
FIRST_TTS_CHARS=10
NEXT_TTS_CHARS=28
```

策略：

- 首个自然逗号达到约 6 个有效字符即可提交，降低首音频等待；
- 后续短语更长，给 TTS 更多上下文，减少韵律碎片；
- 句号、问号、感叹号优先作为完整边界；
- 没有标点时才按目标长度切分。

分句确实会影响首音频延迟和语气连续性，但不会再导致“一句话被截断”。截断问题来自旧 Hold 裁剪逻辑。

## 播放器参数

```env
PLAYBACK_MAX_QUEUE_MS=30000
PLAYBACK_PREBUFFER_MS=180
PLAYBACK_REBUFFER_MS=100
```

- 正常播放和 Qwen 暂停期间都完整保存音频；
- 30 秒是灾难保护上限，不是目标延迟；
- 首播先积累 180ms，吸收网络抖动；
- 播放中断粮后积累 100ms 再恢复，避免碎片化卡顿；
- 真正确认打断时仍会立即清空整个队列。

## 从旧版迁移

复制旧 `.env` 后，至少增加或修改：

```env
BARGE_MODE=qwen
FIRST_TTS_CHARS=10
NEXT_TTS_CHARS=28
PLAYBACK_MAX_QUEUE_MS=30000
PLAYBACK_PREBUFFER_MS=180
PLAYBACK_REBUFFER_MS=100
```

旧的 `BARGE_ABSOLUTE_DBFS`、`BARGE_MIN_SNR_DB` 等参数可以保留；在 `qwen` 模式下只用于页面观测，不参与裁决。

## 运行

### macOS / Linux

```bash
chmod +x run.sh
./run.sh
```

首次运行会创建 `.env`。填写 API Key 后再次运行，访问：

```text
http://127.0.0.1:8765
```

### Windows

双击 `run.bat`。首次运行生成 `.env`，填写后再次双击。

## 推荐测试

### 1. 不插话完整播放

让 AI 回答两到三句话，不发出任何声音。

预期：

- 不出现 `本地候选插话`；
- 不出现 `推测性静音期间仅保留最近 240ms`；
- 播放器断粮次数为 `0`；
- 整句话完整播放。

### 2. 无意义附和

AI 说话时说“嗯”“啊”。

预期：

```text
Qwen speech_started
→ 暂停
→ turn_invalid / ambient
→ 原位续播
```

不会丢掉暂停期间音频。

### 3. 有效插话

AI 说话时说：

```text
不对，我想问的是青海电信。
```

预期：

```text
Qwen speech_started → 暂停
稳定 ASR 前缀 → 清空旧回答
ASR Final → 启动新 LLM/TTS
```

## 本地验证

```bash
python -m compileall -q .
pytest -q
node --check static/app.js
node --check static/player-worklet.js
node tests_js/player_worklet_test.js
node tests_js/streaming_resampler_test.js
python smoke_test.py
```

## 关键文件

```text
session.py                 Qwen 主导打断状态机
static/player-worklet.js   完整暂停队列、可靠播放、抖动缓冲
static/app.js              本地声级观测、播放控制、指标面板
tts_client.py              TTS 首文本/首音频/下发速度指标
utils.py                   自适应自然短语切分
qwen_audio_sensor.py       smart_turn、ASR、心跳和重连
```

## 级联架构的边界

端到端语音模型可以在同一个模型内部联合处理“听到什么、是否打断、已经说到哪里、后续该如何重写”。本 Demo 的 ASR、LLM、TTS 是三个独立系统，因此无法完全复刻原生 Speech-to-Speech 的联合状态。

对于可替换 LLM/TTS 的级联架构，当前最稳妥的折中是：

> Qwen 起声事件负责暂停，Qwen 语义结果负责最终裁决；暂停期间完整保存外部 TTS，避免误判造成不可逆内容丢失。

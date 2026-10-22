# Qwen TTS → 6 口型 → WebSocket POC

> **注意：此 README 以下是早期 POC 历史说明，不是当前常驻服务安装/通信规范。** 当前独立 Windows 服务请运行 `start_win.bat`，其他项目接入请阅读同目录 [`外部项目接入协议.md`](外部项目接入协议.md)，完整口型时间轴字段见 [`PROTOCOL.md`](PROTOCOL.md)。勿照以下旧 POC 用 Allosaurus 或 Python 3.10 重装现有 ForcedAligner 服务。

这是一个**独立验证脚本**，不修改现有机器人项目。

## 固定约定

| 口型 | script_id |
|---|---:|
| a | 1 |
| o | 2 |
| e | 3 |
| i | 4 |
| u | 5 |
| ü | 6 |

默认目标 WebSocket：

```text
ws://192.168.1.47:8090/robot/room/reb_sam
```

默认测试文本：

```text
阿。哦。鹅。衣。乌。鱼。阿姨今天和我一起出去，我觉得非常有趣。
```

## POC 流程

```text
固定文本
  ├─ g2pW → 拼音 → 预期 a/o/e/i/u/ü 序列（WHAT）
  │
  └─ Qwen-Audio-TTS → 流式 PCM 22050Hz s16le
                         ↓
                     Allosaurus
                         ↓
                  phone + 近似时间（WHEN）
                         ↓
              单调序列对齐 / 6-viseme mapper
                         ↓
        script_id + start_sample + duration_samples
                         ↓
             实时回放 + WebSocket 发送
```

第一版故意把“**文本决定是什么口型**”和“**音频决定什么时候触发**”分开。
Allosaurus 即使把相近元音识别错，文本序列仍然能约束最终输出。

## 1. 环境

推荐 Python 3.10 / 3.11，新建虚拟环境。

Windows:

```bat
py -3.10 -m venv .venv
.venv\Scripts\activate
python -m pip install -U pip
pip install -r requirements.txt
```

macOS / Linux:

```bash
python3.10 -m venv .venv
source .venv/bin/activate
python -m pip install -U pip
pip install -r requirements.txt
```

> Allosaurus 会安装 PyTorch，首次运行还可能下载其 phone recognizer 模型。
> g2pW 首次运行也可能下载/初始化模型，所以第一次会明显慢一些。

## 2. 配置

直接把你现有项目的 `.env` 复制到这个目录即可。脚本读取：

```text
DASHSCOPE_API_KEY
ALIYUN_WORKSPACE_ID
TTS_MODEL
TTS_VOICE
TTS_INSTRUCTION
TTS_RATE
TTS_PITCH
TTS_VOLUME
```

不要把真实 API Key 提交到 Git。

## 3. 第一遍先不连嵌入式

```bash
python qwen_tts_viseme_poc.py --no-ws
```

应看到：

```text
[G2P     ] ...
[EXPECTED] a o e i u ü ...
[TTS     ] PCM chunk ...
[PHONE   ] ...
=== 6-VISEME TIMELINE ===
#00  a -> script_id=1 start=...
#01  o -> script_id=2 start=...
...
```

同时生成：

```text
output/qwen_tts_output.pcm
output/qwen_tts_output.wav
output/viseme_timeline.json
```

先听 `qwen_tts_output.wav`，再检查 `viseme_timeline.json`。

## 4. 连 192.168.1.47 测试

```bash
python qwen_tts_viseme_poc.py
```

脚本会：

1. 调阿里 Qwen-Audio-TTS，流式收到 PCM；
2. 生成六口型时间轴；
3. 本机播放同一段 PCM；
4. 按同一时间轴向 WebSocket 发 script_id。

默认发送完整 JSON：

```json
{
  "type": "mouth_script",
  "sender": "qwen_tts_viseme_poc",
  "initiator": "tts_viseme",
  "stream_id": "viseme-poc-xxxx",
  "seq": 3,
  "script_id": 4,
  "viseme": "i",
  "start_sample": 18234,
  "duration_samples": 2960,
  "start_ms": 826.94,
  "duration_ms": 134.24,
  "source_phone": "i",
  "detected_viseme": "i"
}
```

### 如果房间协议暂时只接受 script_id

只发：

```json
{"script_id":4}
```

运行：

```bash
python qwen_tts_viseme_poc.py --payload-mode simple-json
```

如果只接受纯文本数字：

```bash
python qwen_tts_viseme_poc.py --payload-mode plain
```

会直接发送：

```text
4
```

## 5. 时间调试

当前 POC 同时提供：

- `start_ms`：方便人看日志；
- `start_sample`：最终集成真正应该使用的时间轴。

22050 Hz 下：

```text
start_sample = 22050
```

就是 PCM 的第 1.000 秒。

如果实际观察到嘴型总是慢约 80ms，可以先测试：

```bash
python qwen_tts_viseme_poc.py --lead-ms 80
```

这只是 POC 的 wall-clock 调参。

**正式集成不要靠 `lead-ms`。**

正式版本应该让嵌入式按：

```text
played_sample_count >= start_sample
```

触发 `script_id`，使声音和嘴型共用 PCM sample clock。

## 6. 为什么第一版不是“收到一小块 PCM 就立刻 Allosaurus”

Allosaurus 的 timestamp 来自 CTC，本身是近似值，而且它原生接口面向 WAV/一段音频，不是严格的低延迟 streaming phone API。

因此第一版先做：

```text
Qwen TTS 流式收 PCM
→ 完整 PCM 做 phone timestamp
→ 实时回放 timeline 给嵌入式
```

先回答最重要的问题：

> 六个口型的映射 + 时间轴，在你现有模型声音上看起来是否可用？

如果这个 POC 看起来可用，第二版再把 Allosaurus 改成：

```text
1.5~2.0 秒滑动窗口
+ 300~500ms lookahead buffer
+ 去重
+ 增量单调匹配
```

这样才是真正的边合成边分析边发送。

## 7. 当前协议未知点

你只提供了 WebSocket URL，还没有提供 `/robot/room/reb_sam` 的既有消息 schema。

所以 POC 提供三种 payload：

```text
full-json
simple-json
plain
```

如果现有 room server 有固定字段（例如 `type/action/data/room_id`），只需要修改脚本中的：

```python
WebSocketSender._send_obj()
```

以及 `send_event()`，音素和时间算法不需要改。

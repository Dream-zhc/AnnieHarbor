# 日志补全；旁路 ASR 情绪/方言与 TTS 拟声实测（2026-11-20）

## 日志补全

此前打断时序、播放到哪、被丢掉的话、传感器取舍、动作回执、每段语气、待机状态变化只发到工作台实时流，不落日志。现在：

- `wake_state robot=… from=… to=… expirations=…`：每次待机/唤醒/跟进/对话状态变化（`presence.py`）。
- `event robot=… kind=…`：工作台事件里挑出的这些也写一行（`realtime.py` `LOGGED_EVENTS`）：
  - 打断与播放：`playback.hold_requested/resume_requested/interrupt_confirmed`、`turn.cancel_requested`、扬声器回执 `tts.playback.held/resumed/stopped/drained/control_error`，带 `played_ms`（她这句已播多少）、`total_ms`、`since_user_speech_ms`（用户开口后多久）、`reason/source`；
  - 被丢掉或忽略的话：`transcript.dropped/ignored/ambient`，全文（前 200 字）和原因；
  - 每轮结束：`turn.done`，状态、取消来源、首字耗时、回复字数与前 200 字；
  - 传感器取舍：`event.policy`（事件、决定、原因，如 habituated）；
  - 动作：`action.state`（略过 reserved/awaiting/send_unknown 中间态），脚本、状态、call id；
  - 每段语音合成：`tts.run_task`，情绪、发声指令、语速/音调/音量、段号；
  - 唤醒：`wakeword.detected/sleep_command/response_rejected/transcript_ignored`；
  - 旁路 ASR：`acoustic.observed`，语种、情绪。
- `turn_started` 行加上用户原话（前 200 字）。
- 测试：tests/test_event_logging.py。

## 现状核对（供决策）

- 每次回复最多配 3 个说话动作（`[motions] per_reply`）。11 月 962 次回复里 238 次配了 1 个、11 次配了 2 个、没有配 3 个的，约四分之三不配。动作按句由决策模型挑，开始时间对到那句第一个字的对齐时刻（有 ForcedAligner 对齐点就用，没有按每秒 4.3 字估）；不是音素级。嘴型才是音素级（外部 tts_viseme）。
- 11 月 962 轮里只有约 10 轮记成被打断；986 轮里 316 轮没有扬声器回执（不知道实际播了多少）。

## 旁路 ASR（真实接口，`var/tmp/20261120-tts-asr/probe.py`）

旁路是 `qwen3-asr-flash-realtime`（10-09 接入，`ANNIE_ACOUSTIC_SIDECAR_ENABLED` 默认关）。用线上复刻音色合成的样本喂给它：

| 样本 | 情绪 | 语种 | 转写 |
| --- | --- | --- | --- |
| 开心/难过/生气/惊讶/平静（发声指令合成） | happy/sad/angry/surprised/neutral，5/5 对 | zh | 正确 |
| 四川话、河南话 | neutral | zh | 基本正确（“得劲儿”→“带劲”） |
| 粤语 | neutral | yue | 正确（繁体） |

- 情绪能识别（7 类：surprised/neutral/happy/sad/disgusted/angry/fearful）；这些是演出来的情绪，真人更含蓄，待现场录音再测。
- “方言识别不准”的原因：它只报语种，普通话和四川话、闽南语、吴语都报 `zh`，本来就分不出河南/陕西/四川；河南/陕西只能靠方言词推断。
- 百炼现有 ASR 里，带情绪的只有 Qwen-ASR 系列（`qwen3-asr-flash-realtime`）；方言覆盖最广的是 `fun-asr-realtime`（七大方言体系 + 20 多种地区口音，含河南、陕西），但不带情绪；没有哪个模型输出地域方言标签。

## TTS 拟声标签（同一脚本）

线上复刻音色、`qwen-audio-3.1-tts-plus`、现有双向流式：`[laughing]`、`[giggles]`、`[sighing]`、`[gasp]` 都被合成成声音而不是念出来（ASR 转写里没有标签词；同一句加 `[laughing]` 长 0.24 秒、`[giggles]` 长 0.72 秒）。10-09 记录的“标签只支持单向流式”已不成立。样本 `var/tmp/20261120-tts-asr/tag_*.wav` 待用户试听，听完删除。

## 验证

`uv run --locked pytest -q` 1525 passed；ruff、`npm --prefix web test`（142/0）通过；隔离根 `sh start.sh`：工作台 200、`/health` 503（Room 故意连不上），日志无新增异常；进程端口已释放，隔离根与控制台日志已删除。

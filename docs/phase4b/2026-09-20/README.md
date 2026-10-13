# Phase 4B｜独立TTS与嘴型阶段摘要

> 2026-10-10精简历史返工过程。本文保留当时的软件结论和未闭合证据，不定义今日配置或自动推进开发。**真实设备/声学闭环仍待统一验收。**

## 保留的软件与协议边界

- 沿用独立Qwen TTS、复刻音色、24kHz与Realtime text-only；一机器人独占一个Room，六通道全保留，单份音频只合成一次。TTS开关和模型值查当前配置，不从历史本机启用状态推定。
- TTS独立WebSocket使用 `run-task → task-started → continue-task → finish-task → task-finished`与binary PCM，不与Realtime `session.update`混用。每机器人分句/背压、超时、generation及取消独立。
- 同一PCM按24kHz/mono/S16LE、20ms/960B和连续样本游标发布至 `reb_tts`，JSON/Base64输出不当16kHz输入MIC_AUDIO。采样时钟流控与有界缓冲保留，不能靠删控速换首响。
- 播放/取消字段、真实回调消费样本及回执要求见[播放端WebSocket协议](播放端WebSocket协议.md)；后续Hold/Resume与 `reb_voice`见[语音信令](../../phase5/2026-09-21/语音信令与全双工.md)。保留 `conversation_id/stream_epoch/start_sample/end_sample/stream_final`，各回执源分别校验turn/Room/epoch/seq/游标及发布范围。
- 生成完成、首PCM、Room发布、设备已播分列。只有匹配且完整的真实speaker drained才升格 `assistant_spoken_text`与有效助手上下文；browser_test、部分stopped或仅发送成功不替代。stop请求已发不等于设备已停；在途PCM末样本也纳入停止游标边界。
- ForcedAligner只消费同一真实PCM与权威文本，不再调用TTS、不按文字平均时长伪造嘴型。`sidecar_started_unverified`不是已对齐，必须看到本流真实mouth_script。
- Sidecar依赖独立安装，不塞进主Runtime；虚拟环境Python不能因解析符号链接跳出venv而误报依赖缺失。模型/缓存使用项目内资产，不依赖旧仓库或搬旧venv/真实.env。
- 历史双侧车预热出现资源争用风险，后续收敛为默认首机器人/本机annie-a单侧车；B的Realtime/TTS/Room保留，不声称双头嘴型已完成。G2PW默认不加载、可显式开启；当前环境与启动方式查sidecar说明及配置。

## 历史实测结论

| 证据 | 实际结果及边界 |
| --- | --- |
| 独立真实TTS Provider，不推Room | 输入“你好。”返回53,760B PCM与task-finished；只证明合成合同，不证明扬声器起播。 |
| Room非播放握手 | roomv2/roomv3的reb_tts身份匹配WELCOME；当次未发送PCM或stop。后续真实订阅接线不等于设备播放通过。 |
| 独立启动与HTTP | 关闭TTS/sidecar的隔离Runtime曾返回页面与health 200并正常退出；当时临时HTML已于2026-10-08移除，不恢复。 |
| 当时最终自动化 | Python108项、25子测试、2项第三方弃用警告；前端16项，类型/构建/Ruff/锁/diff通过。是历史版本结果，不是当前计数。 |
| Provider-only原始PCM | 62,640样本/2.610s，peak32767，近满幅162；第二次近满幅148。部分近满幅来自Provider，但不能据此确认全部杂音根因或宣称音质已修复。 |
| 网页观察 | 曾有欠载8次/削波681样本；另次欠载0/近满幅386样本。消除欠载不等于消除失真，调低网页音量不能修复已削平的上游PCM。 |

Provider-only第二次WAV仍在本机 `var/tmp/phase4-tts-noise/provider-only.wav`，仅用于未闭合的耳听对照，问题确认关闭后再清理。旧 `tests/manual_tts_pcm.py`探针已删除，历史命令不再作为可运行入口。

## 已收敛的返工与现行入口

- 历史逐帧AudioBufferSourceNode存在抖动空洞风险，曾改用AudioWorklet连续缓冲与实际消费游标；两者均不能代替物理播放证据。网页播放器经历移除与后续按用户要求重新接入，**以当前React工作台实现为准**，不保留相互矛盾的中间操作指南。
- 发布曾增加约240ms采样提前量，短文本合并、强标点优先；最终ASR可按原turn补正Ledger。以上调整不证明麦克风/AEC或音色问题全部解决。
- Realtime流改为有界SSE、常规健康状态降频，诊断按需读取；真实回执、取消、generation和游标校验保留，不以删业务边界冒充减负。
- 正式操作统一 `/?view=workbench`；旧 `/phase-tests/`及独立HTML不恢复。后续上下文/语音返工见[Phase 5摘要](../../phase5/2026-09-20/README.md)，AEC见[接线与待验](../../开发记录/2026-10-08/云端AEC接线与真机联调.md)。

## 统一验收仍待完成

真实麦克风→smart_turn/ASR→Realtime→独立TTS→实体扬声器及嘴型连续多轮；同Room一路音源，验证有效插话停旧声/嘴型、误Hold续播、真实起停播回执、音画映射、削波/欠载与耳听、单侧车资源曲线及长时间运行。browser_test截图不证明实体播放、AEC或设备已停；无回执继续unknown。

长期回归保留 `tests/test_phase4b.py`和相关Realtime/语音前端用例；常规检查见[项目入口](../../../README.md)。自动化不代签人工验收。

# 外部协议清单

日期：2026-09-17。这里只记录仓库已证明的消息形状，不冒充房间服务端完整schema。必填是已核实producer/example形状；consumer的兼容输入路径另行注明。字段类型/可选项同 `external_contract.json`。

## 1. 房间与职责

| 房间 | 当前默认URL | 内容 |
|---|---|---|
| `reb_main` | `ws://192.168.1.47:8090/robot/roomv2/reb_main` | ASR/传感器/robot_event/cancel/action_trigger/robot_command/播放回执 |
| `reb_tts` | `ws://192.168.1.47:8090/robot/roomv2/reb_tts` | JSON/Base64 TTS PCM、expression、播放control/status |
| `reb_sam` | `ws://192.168.1.47:8090/robot/roomv2/reb_sam` | llm_delta/viseme_control、mouth_script（summary为文档预期，当前发送链有丢弃问题） |
| `reb_visualdata` | `ws://192.168.1.47:8090/robot/roomv2/reb_visualdata` | 结构化vision.perception与legacy face/object/gesture；视觉命令及状态结果 |
| `reb_vision` | `ws://192.168.1.47:8090/robot/roomv2/reb_vision` | 二进制裸JPEG/PNG或Room BinaryFrame包裹图像 |
| `reb_mic` | `ws://192.168.1.47:8090/robot/roomv2/reb_mic` | mic.start + MIC_AUDIO BinaryFrame；首版V2非对话入口 |

Web文本入口：旧版`/ws/voice`的`text_input`，进入同一主脑链。reb_face/reb_object仅兼容配置，不代替当前reb_vision。

## 2. 已证明消息与字段

### subscribe

房间：`all`；方向：client → room。

| 字段 | 类型 | 当前记录 |
|---|---|---|
| `type` | `str` | 已验证形状必有 |
| `data.types` | `list` | 已验证形状必有 |

发送 SUBSCRIBE/data.types=["*"]；WELCOME/ACK 等不进 Brain；服务端源码缺失。

证据：[src/robot_brain/robot/bridge/service.py](/Users/dream/Documents/zhc/python/zh_Live-robot-terminal-brain/src/robot_brain/robot/bridge/service.py#L2344)。

### asr

房间：`reb_main`；方向：ASR → Brain。

| 字段 | 类型 | 当前记录 |
|---|---|---|
| `type` | `str` | 已验证形状必有 |
| `text` | `str` | 可选/兼容 |
| `data.text` | `str` | 可选/兼容 |
| `data.payload.text` | `str` | 可选/兼容 |
| `from` | `str` | 可选/兼容 |
| `ts` | `int` | 可选/兼容 |
| `event_id` | `str` | 可选/兼容 |
| `asr_turn_token` | `str` | 可选/兼容 |

asr/ASR_RESULT；文本字段 text/result/asr/transcript/content，逐字段按 payload→data→顶层取首个非空值；文本位置择一。from 是来源，不等于输出目标。

证据：[src/robot_brain/robot/bridge/room_protocol.py](/Users/dream/Documents/zhc/python/zh_Live-robot-terminal-brain/src/robot_brain/robot/bridge/room_protocol.py#L44)。

### text_input

房间：`/ws/voice`；方向：Web → Brain。

| 字段 | 类型 | 当前记录 |
|---|---|---|
| `type` | `str` | 已验证形状必有 |
| `text` | `str` | 已验证形状必有 |

空文本不创建回合。

证据：[src/robot_brain/interfaces/legacy_application.py](/Users/dream/Documents/zhc/python/zh_Live-robot-terminal-brain/src/robot_brain/interfaces/legacy_application.py#L2278)。

### robot_event

房间：`reb_main`；方向：device/algorithm → Brain。

| 字段 | 类型 | 当前记录 |
|---|---|---|
| `type` | `str` | 已验证形状必有 |
| `data.event_type` | `str` | 已验证形状必有 |
| `data.payload` | `dict` | 已验证形状必有 |
| `data.source` | `str` | 可选/兼容 |
| `data.event_id` | `str` | 可选/兼容 |
| `data.ts_ms` | `int` | 可选/兼容 |
| `data.ttl_ms` | `int` | 可选/兼容 |
| `data.confidence` | `number` | 可选/兼容 |

这里锁定一个已支持的 envelope 形状；event_type/source/payload 也有顶层兼容路径。算法专用业务字段没有单独证据，待确认。

证据：[src/robot_brain/brain/context/protocol_adapter.py](/Users/dream/Documents/zhc/python/zh_Live-robot-terminal-brain/src/robot_brain/brain/context/protocol_adapter.py#L248)。

### pressure

房间：`reb_main`；方向：device → Brain。

| 字段 | 类型 | 当前记录 |
|---|---|---|
| `type` | `str` | 已验证形状必有 |
| `level` | `int` | 已验证形状必有 |
| `id` | `int` | 可选/兼容 |
| `sensor_type` | `str` | 可选/兼容 |
| `text` | `str` | 可选/兼容 |
| `data` | `dict` | 可选/兼容 |
| `from` | `str` | 可选/兼容 |
| `room` | `str` | 可选/兼容 |
| `ts` | `int` | 可选/兼容 |

顶层与data兼容；pressure 0–5（0无压力），id=1额头/2鼻子/3脸颊/4肩膀拍打；gas/touch归一到0/1；V2第一版仅更新WorldState，不自动开口。

证据：[src/robot_brain/robot/bridge/room_protocol.py](/Users/dream/Documents/zhc/python/zh_Live-robot-terminal-brain/src/robot_brain/robot/bridge/room_protocol.py#L67)。

### touch

房间：`reb_main`；方向：device → Brain。

| 字段 | 类型 | 当前记录 |
|---|---|---|
| `type` | `str` | 已验证形状必有 |
| `level` | `int` | 已验证形状必有 |
| `id` | `int` | 可选/兼容 |
| `sensor_type` | `str` | 可选/兼容 |
| `text` | `str` | 可选/兼容 |
| `data` | `dict` | 可选/兼容 |
| `from` | `str` | 可选/兼容 |
| `room` | `str` | 可选/兼容 |
| `ts` | `int` | 可选/兼容 |

顶层与data兼容；pressure 0–5（0无压力），id=1额头/2鼻子/3脸颊/4肩膀拍打；gas/touch归一到0/1；V2第一版仅更新WorldState，不自动开口。

证据：[src/robot_brain/robot/bridge/room_protocol.py](/Users/dream/Documents/zhc/python/zh_Live-robot-terminal-brain/src/robot_brain/robot/bridge/room_protocol.py#L67)。

### gas

房间：`reb_main`；方向：device → Brain。

| 字段 | 类型 | 当前记录 |
|---|---|---|
| `type` | `str` | 已验证形状必有 |
| `level` | `int` | 已验证形状必有 |
| `id` | `int` | 可选/兼容 |
| `sensor_type` | `str` | 可选/兼容 |
| `text` | `str` | 可选/兼容 |
| `data` | `dict` | 可选/兼容 |
| `from` | `str` | 可选/兼容 |
| `room` | `str` | 可选/兼容 |
| `ts` | `int` | 可选/兼容 |

顶层与data兼容；pressure 0–5（0无压力），id=1额头/2鼻子/3脸颊/4肩膀拍打；gas/touch归一到0/1；V2第一版仅更新WorldState，不自动开口。

证据：[src/robot_brain/robot/bridge/room_protocol.py](/Users/dream/Documents/zhc/python/zh_Live-robot-terminal-brain/src/robot_brain/robot/bridge/room_protocol.py#L67)。

### tts

房间：`reb_tts`；方向：Brain → playback/viseme。

| 字段 | 类型 | 当前记录 |
|---|---|---|
| `type` | `str` | 已验证形状必有 |
| `ts` | `int` | 已验证形状必有 |
| `data.audio` | `str` | 已验证形状必有 |
| `data.format` | `str` | 已验证形状必有 |
| `data.sample_rate` | `int` | 已验证形状必有 |
| `data.channels` | `int` | 已验证形状必有 |
| `data.text` | `str` | 已验证形状必有 |
| `data.seq` | `int` | 已验证形状必有 |
| `data.final` | `bool` | 已验证形状必有 |
| `data.turn_id` | `str` | 可选/兼容 |
| `data.conversation_id` | `str` | 可选/兼容 |
| `data.source` | `str` | 可选/兼容 |
| `data.stop` | `bool` | 可选/兼容 |
| `data.stream_epoch` | `int` | 可选/兼容 |
| `data.playback_protocol` | `str` | 可选/兼容 |
| `data.protocol_version` | `int` | 可选/兼容 |
| `data.audio_total_samples` | `int` | 可选/兼容 |
| `data.audio_total_ms` | `number` | 可选/兼容 |

audio 为既有 JSON/Base64 PCM；非空当前运行PCM=24000Hz/mono/S16LE。conversation_id整轮，turn_id分段；final不代表扬声器drained。文档22050仅示例。

证据：[project/tts_viseme/PROTOCOL.md](/Users/dream/Documents/zhc/python/zh_Live-robot-terminal-brain/project/tts_viseme/PROTOCOL.md#L54)。

### expression

房间：`reb_tts`；方向：Brain → device。

| 字段 | 类型 | 当前记录 |
|---|---|---|
| `type` | `str` | 已验证形状必有 |
| `ts` | `int` | 已验证形状必有 |
| `data.expression` | `str` | 已验证形状必有 |
| `data.turn_id` | `str` | 已验证形状必有 |
| `data.source` | `str` | 已验证形状必有 |

喜/怒/哀/乐/中；独立JSON，排在对应PCM前，source兼容值不说明LLM实际来源。

证据：[src/robot_brain/robot/bridge/service.py](/Users/dream/Documents/zhc/python/zh_Live-robot-terminal-brain/src/robot_brain/robot/bridge/service.py#L1713)。

### action_trigger

房间：`reb_main`；方向：Brain → device。

| 字段 | 类型 | 当前记录 |
|---|---|---|
| `type` | `str` | 已验证形状必有 |
| `ts` | `int` | 已验证形状必有 |
| `data.script_id` | `str` | 已验证形状必有 |
| `data.offset_ms` | `int` | 已验证形状必有 |
| `data.conversation_id` | `str` | 已验证形状必有 |
| `data.anchor_source` | `str` | 已验证形状必有 |

只按conversation_id绑定整轮；offset锚定首个source=robot_main_brain PCM的真实播放sample，不使用网络接收时间；无action_cancel。

证据：[src/robot_brain/generation/text_utils.py](/Users/dream/Documents/zhc/python/zh_Live-robot-terminal-brain/src/robot_brain/generation/text_utils.py#L343)。

### robot_command

房间：`reb_main`；方向：Brain → device。

| 字段 | 类型 | 当前记录 |
|---|---|---|
| `type` | `str` | 已验证形状必有 |
| `ts` | `int` | 已验证形状必有 |
| `data.command_type` | `str` | 已验证形状必有 |
| `data.target` | `str` | 已验证形状必有 |
| `data.priority` | `int` | 已验证形状必有 |
| `data.payload` | `dict` | 已验证形状必有 |
| `data.metadata` | `dict` | 可选/兼容 |

motion.stop 示例来自中断出口；target=motion.primary是动作通道，不是机器人身份。

证据：[src/robot_brain/robot/bridge/service.py](/Users/dream/Documents/zhc/python/zh_Live-robot-terminal-brain/src/robot_brain/robot/bridge/service.py#L2301)。

### cancel

房间：`reb_main`；方向：small brain → Brain。

| 字段 | 类型 | 当前记录 |
|---|---|---|
| `type` | `str` | 已验证形状必有 |
| `reason` | `str` | 已验证形状必有 |
| `text` | `str` | 可选/兼容 |
| `source` | `str` | 可选/兼容 |
| `asr_from` | `str` | 可选/兼容 |
| `asr_ts` | `int` | 可选/兼容 |
| `asr_event_id` | `str` | 可选/兼容 |
| `asr_turn_token` | `str` | 可选/兼容 |
| `turn_token` | `str` | 可选/兼容 |
| `script_id` | `str` | 可选/兼容 |
| `latency_ms` | `number` | 可选/兼容 |

reason=action_prep；先event_id再turn_token，双方存在却冲突不退回弱匹配；否则from+ts一致且文本不矛盾；停Brain/TTS，不停小脑接管动作。发送端项目未提供。

证据：[dev/tests/companion/test_small_brain_cancel.py](/Users/dream/Documents/zhc/python/zh_Live-robot-terminal-brain/dev/tests/companion/test_small_brain_cancel.py#L137)。

### llm_delta

房间：`reb_sam`；方向：Brain → viseme。

| 字段 | 类型 | 当前记录 |
|---|---|---|
| `type` | `str` | 已验证形状必有 |
| `sender` | `str` | 已验证形状必有 |
| `initiator` | `str` | 已验证形状必有 |
| `conversation_id` | `str` | 已验证形状必有 |
| `turn_id` | `str` | 已验证形状必有 |
| `seq` | `int` | 已验证形状必有 |
| `text` | `str` | 已验证形状必有 |
| `final` | `bool` | 已验证形状必有 |
| `ts` | `int` | 已验证形状必有 |
| `llm_delta_created_ms` | `int` | 可选/兼容 |
| `llm_delta_enqueue_ms` | `int` | 可选/兼容 |

提前G2P；最终以tts.data.text为准；结束空text/final=true。

证据：[src/robot_brain/robot/bridge/viseme_sam.py](/Users/dream/Documents/zhc/python/zh_Live-robot-terminal-brain/src/robot_brain/robot/bridge/viseme_sam.py#L519)。

### viseme_control

房间：`reb_sam`；方向：Brain → sidecar。

| 字段 | 类型 | 当前记录 |
|---|---|---|
| `type` | `str` | 已验证形状必有 |
| `sender` | `str` | 已验证形状必有 |
| `initiator` | `str` | 已验证形状必有 |
| `enabled` | `bool` | 已验证形状必有 |
| `ts` | `int` | 已验证形状必有 |

现有enabled是全局开关，未带robot_id；多头实例级控制需确认外部契约。

证据：[src/robot_brain/robot/bridge/viseme_sam.py](/Users/dream/Documents/zhc/python/zh_Live-robot-terminal-brain/src/robot_brain/robot/bridge/viseme_sam.py#L516)。

### mouth_script

房间：`reb_sam`；方向：sidecar → device。

| 字段 | 类型 | 当前记录 |
|---|---|---|
| `type` | `str` | 已验证形状必有 |
| `sender` | `str` | 已验证形状必有 |
| `initiator` | `str` | 已验证形状必有 |
| `stream_id` | `str` | 已验证形状必有 |
| `seq` | `int` | 已验证形状必有 |
| `script_id` | `int` | 已验证形状必有 |
| `viseme` | `str` | 已验证形状必有 |
| `at_sample` | `int` | 已验证形状必有 |
| `start_sample` | `int` | 已验证形状必有 |
| `end_sample` | `int` | 已验证形状必有 |
| `duration_ms` | `number` | 已验证形状必有 |
| `timing_source` | `str` | 已验证形状必有 |
| `char` | `str` | 可选/兼容 |
| `pinyin` | `str` | 可选/兼容 |
| `final` | `str` | 可选/兼容 |
| `sample_rate` | `int` | 可选/兼容 |
| `duration_samples` | `int` | 可选/兼容 |
| `at_ms` | `number` | 可选/兼容 |
| `start_ms` | `number` | 可选/兼容 |
| `end_ms` | `number` | 可选/兼容 |
| `conversation_id` | `str` | 可选/兼容 |
| `turn_id` | `str` | 可选/兼容 |
| `ts` | `int` | 可选/兼容 |
| `from` | `str` | 可选/兼容 |
| `room` | `str` | 可选/兼容 |

a/o/e/i/u/ü→1/2/3/4/5/6；final是拼音韵母字符串，不是tts结束标记。由真实声学边界决定时刻；不平均分配，不拉长尾字；允许真实静音及轻微原始重叠。

证据：[project/tts_viseme/qwen_stream_room_g2pw.py](/Users/dream/Documents/zhc/python/zh_Live-robot-terminal-brain/project/tts_viseme/qwen_stream_room_g2pw.py#L1463)。

### mouth_sync_summary

房间：`reb_sam + observation HTTP`；方向：sidecar → observation。

| 字段 | 类型 | 当前记录 |
|---|---|---|
| `type` | `str` | 已验证形状必有 |
| `conversation_id` | `str` | 已验证形状必有 |
| `turn_id` | `str` | 已验证形状必有 |
| `synthetic_stretch_count` | `int` | 已验证形状必有 |
| `forced_fill_ms` | `number` | 已验证形状必有 |
| `sample_rate` | `int` | 可选/兼容 |
| `expected_syllable_count` | `int` | 可选/兼容 |
| `mouth_event_count` | `int` | 可选/兼容 |
| `dropped_chars` | `list` | 可选/兼容 |
| `unaligned_tail_ms` | `number` | 可选/兼容 |
| `coverage_ratio` | `number` | 可选/兼容 |
| `duration_matches_last_end` | `bool` | 可选/兼容 |

完整字段以现有文档示例为准。synthetic_stretch_count/forced_fill_ms必须0；coverage和duration_match仅观测。

证据：[project/tts_viseme/PROTOCOL.md](/Users/dream/Documents/zhc/python/zh_Live-robot-terminal-brain/project/tts_viseme/PROTOCOL.md#L136)。

### vision_perception

房间：`reb_visualdata`；方向：vision → Brain。

| 字段 | 类型 | 当前记录 |
|---|---|---|
| `type` | `str` | 已验证形状必有 |
| `kind` | `str` | 已验证形状必有 |
| `tracks` | `list` | 可选/兼容 |
| `gesture` | `dict` | 可选/兼容 |
| `from` | `str` | 可选/兼容 |
| `ts` | `int` | 可选/兼容 |
| `timestamp_ms` | `int` | 可选/兼容 |
| `sequence` | `int` | 可选/兼容 |
| `room` | `str` | 可选/兼容 |

face/object需要tracks；gesture需要gesture对象；tracks字段name/label/bbox/expression来自已支持形状，不猜设备额外字段。

证据：[src/robot_brain/perception/vision/visual_room_protocol.py](/Users/dream/Documents/zhc/python/zh_Live-robot-terminal-brain/src/robot_brain/perception/vision/visual_room_protocol.py#L318)。

### vision_legacy

房间：`reb_visualdata`；方向：vision → Brain。

| 字段 | 类型 | 当前记录 |
|---|---|---|
| `type` | `str` | 已验证形状必有 |
| `source` | `str` | 已验证形状必有 |
| `sequence` | `int` | 已验证形状必有 |
| `timestamp_ms` | `int` | 已验证形状必有 |
| `face` | `dict` | 可选/兼容 |
| `object` | `dict` | 可选/兼容 |
| `gesture` | `dict` | 可选/兼容 |
| `avoidance` | `dict` | 可选/兼容 |
| `runtime_status` | `dict` | 可选/兼容 |
| `face_management` | `dict` | 可选/兼容 |
| `object_command` | `dict` | 可选/兼容 |

type=face/object/gesture；source=face(仅face)或object(object/gesture)；至少一个business字段。

证据：[src/robot_brain/perception/vision/visual_room_protocol.py](/Users/dream/Documents/zhc/python/zh_Live-robot-terminal-brain/src/robot_brain/perception/vision/visual_room_protocol.py#L397)。

### vision_command

房间：`reb_visualdata`；方向：Brain → vision。

| 字段 | 类型 | 当前记录 |
|---|---|---|
| `type` | `str` | 已验证形状必有 |
| `action` | `str` | 已验证形状必有 |
| `request_id` | `str` | 已验证形状必有 |
| `name` | `str` | 可选/兼容 |
| `samples` | `int` | 可选/兼容 |
| `target` | `str` | 可选/兼容 |

face动作register/cancel_register/delete/train/audit_face；object动作set_object_target/set_object_auto/clear_object_target；参数按动作白名单，request_id关联结果，不是robot_id。

证据：[src/robot_brain/perception/vision/visual_command_router.py](/Users/dream/Documents/zhc/python/zh_Live-robot-terminal-brain/src/robot_brain/perception/vision/visual_command_router.py#L14)。

### mic_start

房间：`reb_mic`；方向：audio publisher → consumer。

| 字段 | 类型 | 当前记录 |
|---|---|---|
| `type` | `str` | 已验证形状必有 |
| `data.audio.codec` | `str` | 已验证形状必有 |
| `data.audio.sample_rate` | `int` | 已验证形状必有 |
| `data.audio.channels` | `int` | 已验证形状必有 |
| `data.audio.frame_ms` | `int` | 已验证形状必有 |
| `data.audio.bytes_per_frame` | `int` | 已验证形状必有 |

也接受data平铺形状；PCM S16LE/mono/16kHz/20ms/640B。V2首版不消费本地音频/全双工，作为外部保留参考。

证据：[src/robot_brain/robot/bridge/mic_channel.py](/Users/dream/Documents/zhc/python/zh_Live-robot-terminal-brain/src/robot_brain/robot/bridge/mic_channel.py#L259)。

### playback_control

房间：`reb_tts`；方向：Brain → playback。

| 字段 | 类型 | 当前记录 |
|---|---|---|
| `type` | `str` | 已验证形状必有 |
| `ts` | `int` | 已验证形状必有 |
| `data.control` | `str` | 已验证形状必有 |
| `data.playback_protocol` | `str` | 已验证形状必有 |
| `data.protocol_version` | `int` | 已验证形状必有 |
| `data.stream_epoch` | `int` | 已验证形状必有 |
| `data.conversation_id` | `str` | 已验证形状必有 |
| `data.reason` | `str` | 已验证形状必有 |

hold/resume/clear为现有duplex条件分支；V2首版不自动引入全双工；clear与stop至少要兼容接收端。

证据：[src/robot_brain/robot/bridge/playback_runtime.py](/Users/dream/Documents/zhc/python/zh_Live-robot-terminal-brain/src/robot_brain/robot/bridge/playback_runtime.py#L176)。

### playback_receipt

房间：`reb_main`；方向：playback → Brain。

| 字段 | 类型 | 当前记录 |
|---|---|---|
| `type` | `str` | 已验证形状必有 |
| `data.conversation_id` | `str` | 已验证形状必有 |
| `data.stream_epoch` | `int` | 已验证形状必有 |

held/resumed/cleared/drained/underflow；旧epoch/cid不应更新当前轮。

证据：[src/robot_brain/robot/bridge/service.py](/Users/dream/Documents/zhc/python/zh_Live-robot-terminal-brain/src/robot_brain/robot/bridge/service.py#L2639)。

### playback_status

房间：`reb_tts`；方向：playback → Brain。

| 字段 | 类型 | 当前记录 |
|---|---|---|
| `type` | `str` | 已验证形状必有 |
| `data.queue_ahead_ms` | `int` | 已验证形状必有 |
| `data.state` | `str` | 可选/兼容 |
| `data.conversation_id` | `str` | 可选/兼容 |

client.playback.status或tts.playback.status；队列毫秒反压，无状态回执时兼容旧客户端。

证据：[src/robot_brain/robot/bridge/tts_channel.py](/Users/dream/Documents/zhc/python/zh_Live-robot-terminal-brain/src/robot_brain/robot/bridge/tts_channel.py#L226)。

## 3. Binary 数据面

- Room BinaryFrame：`[2-byte big-endian typeLen][UTF-8 type][payload]`，typeLen不含自身2字节；decode失败返回None。MIC_AUDIO payload是PCM，不是JSON/Base64。
- MIC_AUDIO：16kHz、mono、S16LE、20ms、640B；消费者须先收到合法mic.start。房间兼容mic.audio JSON/Base64不等于可回到reb_main.robot_mic（该类型已停用）。
- 图像：reb_vision支持裸JPEG/PNG及Room BinaryFrame包裹图像。摄像头完整格式/发送端type码/设备目标字段待确认，不能用frame JSON/Base64旧接口代替现行数据面。
- 正常TTS仍按既有reb_tts JSON/Base64；“麦克风要Binary”不能误扩展成“把所有TTS协议也改Binary”。

## 4. 时序与取消

1. 连接房间 → SUBSCRIBE/data.types=["*"] → 消费消息。WELCOME/ACK等只走控制面。仅发送SUBSCRIBE不证明服务端已支持定向目标。
2. ASR/Web → Turn Gate → 当前主脑。V1另有模型Gate及投机生成；V2只保留PASS/HOLD/DROP业务语义，不锁定旧模型/投机结构。
3. LLM正文delta可提前发sam；TTS权威data.text及相应PCM发tts；expression在对应PCM前；最终空audio/final帧只表示合成流结束，非设备真实播放完成。
4. action_trigger可早于或晚于PCM到达。设备锚定同conversation且source=anchor_source的首PCM真实排队播放sample，再加offset；晚到时当前轮仍播放可补触发一次，旧轮drained则丢弃。
5. 常规中断清TTS队列、发stop；动作开启时发motion.stop。小脑action_prep只在身份匹配时停主脑/TTS，**保留小脑接管动作**；过期/冲突cancel必须不影响新回合。
6. 嘴型基于真实PCM声学边界逐条发；stop/clear/cleared/epoch变化清未发尾巴。过去事件不可回改，未来replacement按同stream_id+seq；真实静音不生成伪造嘴型。
7. transactional_v1的hold/resume/clear与回执是原版duplex条件分支。Phase 0盘点不等于要求V2第一版开发全双工。
8. TTS迟到旧conversation、已clear或过期帧在出口丢弃；现有状态回执提供队列毫秒反压。必须区分网页与设备反压，多个头也不能共用该可变状态。

## 5. 广播/定向与输出目标：已知和缺口

**已知：**消费者SUBSCRIBE=["*"]；代码/注释和sidecar具备自身广播回流过滤；同房间可出现多类消息。输入from是来源，room是房间元数据；request_id和conversation_id仅关联一次命令/回合。

**未证明：**Java服务端是否按目标字段定向、鉴权/连接身份如何绑定、是否原样透传、是否带稳定事件ID、ACK含义、是否重放。设备接收端缺失，不能凭独立输出WS连接假设服务端不广播给所有设备。

**V2已确认需求：**多个头可以订阅相同房间；显式广播各头各投递一次；定向事件只到指定头；ASR cancel/音频/动作/嘴型全部使用可验证目标与实例状态。`robot_id`是内部设计标识，目前未作为已存在外部字段填进fixture。

**联调前置项：**提供房间服务端协议及目标路由实现、小脑发送端、设备音频/动作/嘴型接收端。若没有同房间寻址，需单独确认协议或输出通道方案；内部fan-out无法替接收端区分设备。

## 6. 真实消息覆盖说明

- `fixtures/external_contract.jsonl`包含原协议文档逐字JSON示例、原测试cancel消息、源码literal及显式synthetic边界样例。没有把文档样例冒充在线抓包。
- `fixtures/observed_runtime.jsonl`保存9/17真实LLM request和观测投影的llm_delta/mouth_script；投影不补type或缺失sample字段，不能当完整wire packet重发。
- `evidence/traces.jsonl`保存现有trace完整记录；有prompt/context和响应。`evidence/viseme_log.jsonl`保存真实TTS_RX/嘴型/对齐观测日志。
- 原始ASR/cancel/完整TTS PCM JSON/mouth wire/视觉/算法线上完整收发抓包不足，标为待采样。当前fixture验证只证明离线契约与源码指定纯分支，不证明设备端真实行为。

## 7. 观测HTTP与文档/实现差异

`viseme_health`经HTTP POST到`/api/dev/viseme/events`，字段type/status/pid/ts及sam_connected、tts_connected、sam_subscribed、tts_subscribed、aligner_backend、reason；ready需两个房间订阅成立，不能以进程PID代表ready。

| 字段 | 类型 | 当前记录 |
|---|---|---|
| type / status | str | 生成端初始形状必有 |
| pid / ts | int | 生成端初始形状必有 |
| sam_connected / tts_connected | bool | 房间健康provider补充 |
| sam_subscribed / tts_subscribed | bool | 房间健康provider补充 |
| aligner_backend | str | 房间健康provider补充 |
| reason | str 或 null | ready生成null；未订阅生成room_not_subscribed |

reason的null由当前源码:3256直接证明，不是省略字段的同义词。契约类型`str|null`由离线校验器支持。

**summary差异：**协议文档定义mouth_sync_summary，Engine在final尝试送入send_mouth。但当前main的_deliver_mouth对所有事件都校验timing_source，summary只有alignment_source而没有timing_source，因而被丢弃；9/17日志有`[MOUTH_DROP] reject non-authoritative timing seq=None source=-`，trace中sync_summary为空。本阶段只记录差距，不修改旧版。

# 小安妮 V2｜异步语音感知与方言情绪 TTS 开发计划

> 编制：2026-10-09；已纳入用户本轮逐项确认的交互决策。**状态：最终实施方案（待开发与验收），未实施，未通过实测。**
> 本文件为用户明确提出的新功能专项设计，不表示解除项目总计划中 Phase 10/11 暂停与“普通聊天不增加第二套 ASR”的历史冻结规则。实施前须把本专项视为**受控例外**由用户确认启用；不得借此恢复全量新阶段或改造主架构。

## 1. 目标、范围与不变量

用户音频被独立旁路识别出**语种和声学情绪**；对中文方言尽力保存原始词汇并产生**有来源、证据等级的方言候选**。Realtime LLM 在可及时得到结论的情况下决定是否跟随用户语种／方言，输出相应的自然口语文本；独立 TTS 采用同样语种／方言和合适的情绪、语速、音调、音量合成小安妮原有复刻音色。用户可显式要求“这句用河南话”“这个 Chat 说四川话”“以后这台机器人说普通话”；临时、Chat、机器人全局分别按已确认范围处理。

**硬约束：**
- 原有 Room→AEC→AudioInputGate→唤醒→Qwen-Audio-3.0-Realtime-Flash(ASR/LLM/smart_turn/FC)→文本→独立 Qwen-Audio-3.0-TTS-Flash→Room PCM/ForcedAligner **不替换、不串联旁路 ASR、不二次回答**。
- 普通聊天仍不经过 LangGraph；旁路失败、慢、缺少信息时**立即走原链路**，不能等旁路结果。
- 保留麦克风单音源、一机器人独占一 Room、A/B 独立会话、AEC、KWS 一语即答、插话 HOLD/RESUME/STOP、字锚点动作、嘴型、Ledger、上下文、人格、主动追问及真实播放回执。
- 不把“方言 ASR 支持转写”误报为“能准确返回地域类别”；**允许 LLM 作标注为推断的方言猜测**，但不伪装官方标签或写入持久偏好。不把“说话人分离 speaker_id”误报为“知道用户是谁”；不把模型情绪当成用户真实心理状态。
- 正式 React 工作台只加小型状态／调试项；不增加第二个 HTML、长驻新 LLM 或专用数据库。无用户同意不保存原始声纹样本。

## 2. 已核实的模型能力与选择

| 输入需求 | 首选／候选 | 官方能够直接返回 | 限制与阶段 |
| --- | --- | --- | --- |
| 实时语种 + 输入语音情绪 | **`qwen3-asr-flash-realtime`** 独立 WebSocket | `language`（zh/yue/en/ja/…）、`emotion`（surprised/neutral/happy/sad/disgusted/angry/fearful）、`text`、`stash`；final 也返回 language/emotion | **M1 首选**；`zh` 包括普通话、四川话、闽南语、吴语，不能据此分河南／陕西／四川 |
| 原汁原味方言词汇转写 | `qwen-audio-3.1-asr-flash-streaming`，`keep_dialect=true` | 允许保留方言表达的转写 | **M2 A/B 候选**；不能输出可靠的地域方言标签；无情绪和实时说话人分离；**不默认双模型常驻** |
| 云端主 ASR/LLM/smart_turn | **现有** `qwen-audio-3.0-realtime-flash` | 对话、转写、FC，支持多语种与多方言理解 | **不更换**，不能假定其转写事件携带可用 `dialect` 标签 |
| 已登记目标声音的双工增强 | 现有 Realtime 首次 `session.update` 的 `turn_detection.voiceprint_audio_urls` | 接收最多 5 个 16kHz PCM/WAV URL，异步注册事件，可增强目标说话人关注 | **M3 可选**，仅首次设置生效；不是 speaker_id、人名或身份认证 |
| 多人说话段分离 | `qwen-audio-3.1-asr-flash` 文件/非实时或 filetrans | 分句 `speaker_id`（匿名编号） | **不进入热路径**；实时旁路 `qwen3-asr-flash-realtime` 官方表明不支持说话人分离 |
| 注册用户身份比对 | 独立经验证的 speaker verification/embedding 模型（待选型与实际测评） | 经注册的相似度／阈值判断（**项目实现**，非上述 ASR 自带） | **M4 延后**：需要用户授权、声纹生命周期和拒识机制 |
| TTS 方言、语气 | **现有** `qwen-audio-3.0-tts-flash` + 复刻音色 | `instruction`；`volume`／语速／音调参数 | **M1 首批实施**，保持 `24kHz` 原始 PCM 和单任务双向流 |
| 文本内情绪标签、拟声 | 同一个 TTS | `[sarcastic]`、`[mischievously]`、`[excited]`、`[sad]`、`[giggles]` 等 | **只支持单向流式**，现有 `streaming=duplex` 不可直接照搬；M2 独立可选试验 |

**优先级判断**：上线时最多选择**一个**并行语音识别旁路：默认 `qwen3-asr-flash-realtime` 获取真实语言和情绪；若需要保留地域词汇，再在离线 A/B 实验中评估 3.1-ASR，不能声称两套各有所长所以都常驻。河南／陕西／四川细分地域口音若仅有 `zh`，声学标签仍是 `dialect=unknown`；允许 LLM 根据稳定方言词及语境推断 `dialect_candidate`，但必须显示“推断”，不能伪装分类成功。

阿里官方资料（2026-10-09 查询）：
1. Qwen-ASR Realtime 服务端 `language/emotion` 事件：https://help.aliyun.com/en/model-studio/qwen-asr-realtime-server-events
2. ASR 模型选型／实时情绪与说话人支持：https://help.aliyun.com/zh/model-studio/asr-model
3. 3.1-ASR `keep_dialect`：https://help.aliyun.com/zh/model-studio/qwen-audio-asr-streaming-client-events
4. Qwen-Audio Realtime 声纹增强与首次配置限制：https://help.aliyun.com/zh/model-studio/qwen-audio-realtime-user-guides
5. TTS 指令、方言、标签和单向限制：https://help.aliyun.com/zh/model-studio/realtime-tts-user-guide
6. TTS 参数：https://help.aliyun.com/zh/model-studio/qwen-audio-tts-python-sdk

## 3. 真实代码基线／最小改动点

| 已有代码与事实 | 拟变更（仅本专项） |
| --- | --- |
| `apps/runtime/src/annie_runtime/__init__.py`：每机器人 `AudioInputGate` 有界队列、`stream_epoch`；可选云 AEC；Runtime 负责房间与机器人归属 | 只新增旁路生命周期初始化、开关和状态接线；不得在 Room 收帧回调增加网络等待 |
| `apps/runtime/src/annie_runtime/realtime.py`：`_audio_pump` 经唤醒后 `audio_append` 发送 16kHz 20ms PCM；Realtime 处理 `transcript.delta/done`、`response.created`、`response.text.delta`；已有单一路径 `_combined_instructions` 和视觉/记忆/对话更新合并器 | 在**确实通过准入、发往主模型的 PCM**处复制不可变 `bytes` 引用给旁路队列（可考虑有限的预缓存且不重复送音）；合并器增加小型带版本语种提示；各事件仅标记里程碑 |
| `packages/annie-adapters/src/annie_adapters/realtime.py`：原 `session_update` 只配置 `modalities=["text"]`、`smart_turn`、工具／人格 | 不改变默认协议；可选首次会话设置已有说话人增强 `voiceprint_audio_urls`，须实际核验字段与注册事件 |
| `packages/annie-adapters/src/annie_adapters/tts.py`：`command("run-task", …)` 当前只传 `voice`、PCM、24kHz、`enable_ssml=false` | `run-task` 增加白名单验证后的 `instruction`、`volume`、`rate`、`pitch`；Wire 键名须用实际 WebSocket 协议与 SDK 对照验证（SDK 名是 `speech_rate/pitch_rate`），严禁盲猜 |
| `apps/runtime/src/annie_runtime/speech.py`：每轮只有 1 个 TTS 任务；`run-task` 后 `continue-task` 逐片送；有界合成队列、Room 24kHz 帧、播停证据／viseme | **每轮首个 run-task 前冻结一次 `SpeechStyle`**；不对已开始的流动态改参，不打乱 `text.delta`→TTS 的时序；不要让原始标签进入显示文本／动作字锚点 |
| `packages/annie-brain/src/annie_brain/persona.py`：仅输出可说的文字、不写旁白，已有 Persona/SocialState 与对话节奏策略 | 复用当前人格规则，追加短小“语言跟随偏好”指令，而非每轮添加数百字动态大 Prompt |
| `web/src/workbench.tsx`、现有音频／性能诊断 | 一处展示旁路连接、输入 `language/emotion/dialect` 及来源／时效，LLM 采用与否、TTS 风格，状态与首响影响；不建新工作台 |

当前 `speech.py` 的流式、异步、取消/播放游标、ForcedAligner 字锚点均为必须回归的契约。已有代码与工作区可能有并行修改；落地时须再读最新文件并局部合并。

## 4. 建议处理时序（保证不阻塞主链）

```text
真实 Room 麦克风 PCM → 云端 AEC（启用时）
       │
       └→ AudioInputGate → Wake / Admission → [主路允许的 16kHz PCM]
                                         ├── 主路：Qwen Realtime → smart_turn → LLM text.delta ──┐
                                         └── 无等待 put_nowait → 有界旁路队列 → Qwen3-ASR WS      │
                                                                  │ language / emotion / text    │
                                                                  ▼                             │
                                                        AcousticObservation                 │
                                                                  ▼                             │
                                                          ReplyLanguageState ──instructions ┤
                                                                 (异步、可迟到)                 │
                                                                                                ▼
                                                                              回合开始时锁定 SpeechStyle
                                                                                                ▼
                                                                            Qwen TTS run-task + continue-task
                                                                                                ▼
                                                                           既有 Room / 扬声器 / ForcedAligner
```

### 4.1 绝不能误解的时序

- 旁路与主路**共享同一份授权音频**，旁路转写仅做观察，**不作为第二条 user 消息发送给 Realtime**，否则重复问答／打断／历史污染。
- `smart_turn` 自动发起回复；**异步旁路不能严格保证结果先于本轮首个 LLM token**。实际策略是“能赶上本轮就生效，否则延迟到下一轮”。已发生的首个回复片段永不回滚改写。
- `session.update` 发送成功不等于模型已采用；应等待 `session.updated` 确认并对比 `response.created`／首 `text.delta` 的时间先后。仅在**确认该轮使用的指令版本**时记 `applied_current_turn=true`；无法证实时写 `unknown/next_turn`。单次会话动态 `instructions` 变更可能晚于生成开始，不能宣称严格同步。
- 确实要“首次新方言在第一字回复前生效”时，必须另立严格模式任务，将自动回复改为可控触发并测试对应 VAD/打断；**本计划不实施**，不能用“等待 200ms 分类”偷偷阻塞主路。
- 对 TTS，`run-task` 前若没有可靠本轮状态，则使用**上次已稳定偏好或普通话默认**；参数锁定后迟到数据只影响后续回合。LLM 改变文本语言而音频仍使用旧方言时必须记录 `language_mismatch`，不得假称闭环成功。

### 4.2 有界队列与身份防污染

- 1 个启用的机器人至多 1 条旁路 ASR WebSocket；每机器人独立接收／发送任务与有界队列。网络连接预热或短断线重连走独立任务，绝不在 20ms PCM 回调内 `await` 云端服务。
- 本地 PCM 帧为不可变对象引用；发送方在自己的任务序列化；无同步大规模复制、音频重采样或逐帧数据库写入。约束队列上限（候选：不超过 50 帧/1 秒）与积压时效（候选：<=250ms，需压测调整）。
- 旁路落后时：**整段判断失效**而不是随意漏帧拼接出伪连续音频；重启旁路会话或等待下一段音频；主链继续运行。连接超时／熔断，恢复后仅处理新 epoch。
- 观察结果必须携带 `robot_id, room_id, source_id, audio_stream_epoch, robot_generation, chat_id, persona_id, turn_id/utterance_id, observed_monotonic_ms, provider_item_id`（部分尚未出现时可 null）。**主旁路的 provider item_id 不共享**：靠本地样本游标／语音起止窗口进行有界关联，匹配模糊就丢弃关联，绝不能凭文字相似绑定。
- 换 Room、Chat、Persona、重连、用户打断、唤醒重置、启停旁路、Runtime 停止一律递增代次/撤销过期结果；不得把 A 的语言结果交给 B。
- 休眠态不建立云端全天候旁路监听；只传已允许给主 Realtime 的 PCM。要识别唤醒词开始的区域，需重用**现有**唤醒预缓存、仍遵循既有授权和时效，不另开一条绕过唤醒的 ASR。

## 5. 数据合同与决策归属（示例为内部协议，不是阿里返回值）

```json
{
  "robot_id": "annie-a",
  "utterance_id": "local-utterance-42",
  "audio_stream_epoch": 12,
  "generation": 4,
  "source": "qwen3_asr_sidecar",
  "language": "zh",
  "emotion": "happy",
  "dialect": "unknown",
  "dialect_evidence": "none",
  "speaker": {"status": "unknown", "profile_id": null},
  "is_final": false,
  "observed_monotonic_ms": 0,
  "valid_until_monotonic_ms": 0
}
```

- **语种 `language`**：原样映射官方 `zh/yue/en/ja/...`。若仅 `zh` 不细分省份；`unknown`、无声、误触发、混合语种可以不跟随。`text` 是稳定前缀、`stash` 是不稳定草稿；`stash` 不用于持久偏好或推断方言。
- **方言 `dialect`**：官方原生未细分时为 `unknown`；独立字段 `dialect_candidate` 可由 LLM 根据稳定方言词汇／语境推断并明确标为 `inferred`，但不能因用户谈论某个地名便推断其口音。用户明确要求的 `dialect_requested` 单列。分四川／河南／陕西的专属声学分类留后续评测，不虚构 `dialect_confidence`；无原生概率时写 null，记录证据文字与 `evidence_level=explicit_user|asr_language|lexical_inference|none`。
- **输入情绪 `emotion`**：消费 Qwen3-ASR 原生 7 类，保留时间和来源。只是当时声学表现，不得直接修改用户记忆、人设、唤醒许可、行动权限或做心理诊断。短暂暴怒／难过先在回复语气上温和处理，不自动嘲讽。
- **说话人 `speaker`**：将三件事分离：
  1. **Realtime 声纹增强**：`voiceprint_audio_urls` 使模型更关注预登记目标声音，服务端有 `in_progress/completed/failed` 事件，但不会给出稳定人名或每句 `speaker_id`。
  2. **说话人分离**：一段音频内 `speaker_id=0/1`，只是匿名分段编号，离线/非实时能力不适合本次主链。
  3. **身份验证**：用户同意注册音频后由独立 verification 服务判断 `known_profile_id / unknown`；需真阳/假阳阈值与未知人拒识，当前明确**待设计待验**，不能用“声纹增强成功”伪装实现。
- **语言选择优先级**：见第 7 节用户已确认的**首次跟随覆盖 Chat／机器人默认、后续智能延续**状态机。当前轮显式指令优先；Chat 和机器人全局偏好作为**可被一次性跟随临时覆盖的默认值**，而不是始终硬锁；仅 `language` 改变不会自动改写用户事实或历史。
- **低成本 LLM 控制**：先通过已有 `_combined_instructions` 小型提示传入“当前可用语言偏好、来源、是否首次跟随”；LLM 直接用对应口语产出。**不要求每轮先调用 Function Calling 决定语种**，否则可能增加工具往返；用户显式设置 Chat／长期方言偏好使用经校验、可持久化的工具，晚到的工具结果只影响下一轮。原有 perform_action/get_weather/reminder 不改合同。
- **方言文本与方言发音一起设计**：`OutputSpeechPolicy` 在 TTS 启动前使用已冻结风格；如果 Realtime 输出普通话文本但 TTS 方言语音，不能声称完整“方言回答”；反过来只有地道方言词汇而没有适配 TTS 发音也不能声称闭环。例：用户说河南话→LLM 可自然选择河南话词汇→TTS `instruction="请用自然河南话说..."`。

## 6. TTS 情绪由谁输出：推荐双层结构

**已确认方案 B：LLM 负责“说什么、表达什么意图”，Runtime 负责“状态优先级、合法性、参数映射”，TTS 负责“怎么发声”。** 普通回答仍只产出**干净的可说文本**；不要让 LLM 在正文中输出 JSON、`[angry]` 等控制符再依赖正则猜正文。该职责分工**不意味着 LLM 已有保证早于首个 text.delta 的结构化情绪输出**；具体时序与降级见第 8 节。

建议内部 `SpeechStyle`（在首个 `run-task` 前决定、冻结，不要求 LLM 另调用一次模型）：

```json
{
  "language": "zh",
  "dialect": "henan",
  "emotion": "playful",
  "delivery": "mischievous",
  "voice_ref": "configured_annie_voice",
  "instruction": "使用自然河南话，语气俏皮，不夸张，保持原有音色。",
  "speech_rate": 1.05,
  "pitch_rate": 1.0,
  "volume": 50,
  "source": "persona_and_reply_policy",
  "locked_for_turn": true
}
```

**真实 TTS 控制边界：**
- 当前项目复刻 `voice` 固定，`instruction` 控制方言／情绪／角色，`volume` 允许 0–100、`speech_rate/pitch_rate`（SDK 名）允许 0.5–2.0；raw WebSocket 现有示例使用 `volume/rate/pitch`，**实施时用官方原始协议和实际连接回执验证精确 wire 键**，不得原封不动传内部字段。
- 首次建议安全范围为 `speech_rate=0.90–1.12`、`pitch_rate=0.95–1.05`、`volume=40–60`（**工程候选，不是模型官方限值**）；异常回退 1.0/1.0/50。设备实际音量另由扬声器端控制；TTS 请求 `volume` 不是硬件功放绝对音量，不能绕过设备安全限幅。
- **风格词不应该逐 token 切换**：以每轮一个 `SpeechStyle` 对应一个现有 duplex TTS task。正常情绪受当前 Persona、实际语义、用户情绪和安全边界影响；严肃求助抑制“机灵互怼”。不要自动把每个 `happy` 映射成高音、快语速。
- 如需要“同一段话先严肃、后大笑”，官方 `[serious]`、`[laughing]` 等文本标签目前**只支持单向流式模式**；现有 duplex 路径不得直接混入。将“段内多情绪／笑声效果”列为**M2 独立 A/B 分支**，仅确认单向流任务首包、切分、口型文本和打断延迟可接受后逐步启用。优先验证每轮 instruction 是否已足够自然。
- 原语义文本、展示文本、动作 `anchor_text`、ForcedAligner 输入保持同源；如果后续启用声学标签，必须在独立的 TTS 输入映射层维护 `spoken_text` 与 `provider_text` 的关系，不能让标签成为朗读字符或污染嘴型时间轴。
- 每次音色、语速、情绪控制变化的成本必须测量 `tts_task_start→first_pcm`。不要为了实时变化在同一轮 `finish-task` 后启动多份 TTS，除非专项压测通过且有完整对齐与播放取消证据。

## 7. 语言／方言状态机与存储规则（用户已确认）

### 7.1 不得被算法覆盖的需求选择

- **首次主动跟随一句，后续智能延续**：第一次可靠观察到新语种，或 LLM 从稳定原词与语境提出新的**方言推断**时，优先尝试本轮跟随；同一种方言连续发言不能每轮都重新强制“首次跟随”。之后 LLM 根据语境选择继续或恢复默认。
- **实时第一**：旁路返回晚于 LLM 首文本创建点时，本轮不等待、不取消重做，结果仅用于下一轮。用户已选择此取舍，**首次跟随不保证每次都能赶上首句**。
- **机器人长期范围**：明确“以后都用四川话”按 `robot_id` 存 SQLite，跨 Chat／Persona／重启；任何说话者均可明确覆盖，最后一次成功提交生效。
- **当前 Chat 范围**：明确“这个会话用四川话”按 `robot_id,chat_id` 存 SQLite；重开相同 Chat 仍生效，不影响别的 Chat，切 Persona 不删除该 Chat 的偏好。
- **首次跟随可以临时覆盖两种显式偏好**：例如 Chat 固定四川话，用户突然用河南话，首次仍尝试河南话一句；Chat 保存的四川话**绝不被自动改写**，后续 LLM 决定是否继续河南话。
- **允许智能推断，不允许伪造声学识别**：旁路返回 `zh` 只证明中文；从“中不中”等词判断河南话，结果另标 `dialect_candidate=henan, source=llm_inferred`；没有稳定证据则 unknown。自动推断只能影响临时回复，**不得写入长期偏好或个人身份**。
- **发音与表达同步**：LLM 负责方言词汇与自然用语，TTS 负责声学读法；只将标准文本套一个方言音色不算完成。

### 7.2 状态对象与失效

| 状态 | Scope | 由谁更新 | 何时失效 |
| --- | --- | --- | --- |
| `RobotLanguagePreference` | 机器人唯一键／SQLite | 用户明确“以后所有 Chat……” + 服务端校验后的 FC | 明确更改／清除才失效；**不因自动跟随改变** |
| `ChatLanguagePreference` | `robot_id,chat_id`／SQLite | 用户明确“本 Chat……” + FC | 明确更改／清除或该 Chat 被真正删除；不因关闭页面／休眠／重启失效 |
| `ReplyLanguageState` | 机器人＋当前 Chat＋generation／内存 | ASR 观察、LLM 推断、首次跟随和情境选择 | 切 Chat/Room、会话代次改变、休眠或交谈结束；连续静默 180 秒候选阈值，需实测 |
| `TurnSpeechStyle` | 单个有效 `turn_id + generation`／内存 | Runtime 合并表达意图与当前语言 | 当轮 TTS 第一个 `run-task` 冻结；打断/取消/结束后无效 |
| `SpeakerProfile` | 机器人＋已注册 `speaker_profile_id`／后续专用数据 | 后续独立声纹模块 | **不属于本期 M1**；识别个人仅供未来按 profile 查询专属记忆，不自动切 Chat |

**本轮语言决定顺序**：当前用户明确口头要求 > **新方言第一次临时跟随（甚至覆盖 Chat/机器人默认）** > LLM 后续语境选择 > Chat 持久偏好 > 机器人持久偏好 > Persona 默认语言。第一次机会只对“新的、来自当前有效语音的候选”消耗一次；连续同方言短答不重复触发。遇到引用外语、翻译任务、唱歌词、对方说话人不明的情况，LLM 可拒绝自动跟随并记录原因。

**本轮生效时序**：`acoustic.observed → language_candidate → session.update → session.updated → response.created → text.delta → tts.run-task`。只有旁路结果及时、指令确实被当前回复采用，才能称“本轮跟随”；若模型先开始说，则状态写 `next_turn`。对于 LLM 自行由文本推断的方言，实际首句已经开始时也不可倒回重播。Chat/robot 持久偏好在新轮启动前预加载；不每轮读 SQLite。

### 7.3 最小写库与工具

推荐新增前向兼容 `robot_language_preferences(robot_id PK,language,dialect,revision,source_turn_id,updated_ms)` 与 `chat_language_preferences(robot_id,chat_id PK,language,dialect,revision,source_turn_id,updated_ms)`，复用现有 SQLite 和队列；不把自动推断写成长记忆。Chat 删除同时处理该 Chat 偏好；机器人偏好原样保留。

仅在**明确设置／清除**时允许 `set_language_preference(scope="chat"|"robot",language,dialect)` 或 `clear_language_preference(scope)`。FC 提议经 Runtime 根据**当前真实用户转写／文本、robot、chat、turn、generation**验证；事务写成功才向用户报告成功，迟到工具拒绝旧代次，`call_id` 幂等。任何人可覆盖全局属于用户已选规则，必须留下修订历史以便追踪。**普通聊天的自动跟随不调 FC，更不经 LangGraph。**

### 7.4 必测状态示例

| 场景 | 本轮效果 | 后续与存储 |
| --- | --- | --- |
| 全局普通话，首次新四川话 | 若赶上首 token 则尽量用四川话回复一句，否则正常低延迟回复 | 后续 LLM 决策；机器人偏好仍普通话 |
| Chat 四川话，突然第一次河南话 | 尝试河南话一句 | 随后智能继续/恢复；Chat 记录仍四川话 |
| 用户连续五句河南话，包括“嗯” | 不每句重置首次许可 | 根据语境延续，不误识别“嗯” |
| 用户说“本 Chat 以后都粤语” | 显式指令本轮优先 | 写 Chat 偏好；换 Chat 不沿用 |
| 用户说“以后你都用英语” | 显式指令本轮优先 | 写本机器人偏好，别的机器人无变化 |
| 混说/引用/翻译/模型只返回 zh | 不强行省份方言 | 未确认仍为 unknown；可记录低信度词汇推断 |
| 旁路卡顿／切 Chat／打断 | 主链不停，丢弃旧观察 | 只保留该 Chat/机器人已存偏好 |

## 8. 方言、节奏、情绪：简洁的 TTS 主干

### 8.1 控制权划分

- **现有 Realtime LLM**：继续实时输出**纯可朗读文本**，直接选择方言词汇、措辞、标点、自然停顿和语义情绪。不要在用户可见文本里输出 `[sad]`、`{emotion:...}` 或“（生气地）”舞台旁白，也不要为每轮专门增加一次 FC 往返。
- **Runtime 小型 `SpeechStylePolicy`（无第二个模型）**：按用户显式指令、ReplyLanguageState、Persona、输入声学情绪和本轮可及时获得的表达意图，选择**有限的** `voice/instruction/rate/pitch/volume`。情绪标签白名单映射在代码里，防止 LLM 输出任意云端参数。
- **独立 TTS Flash**：只合成一份音频，保持当前自定义复刻音色不变。**每轮一个 duplex 任务**，首 `run-task` 前冻结风格，后续 `continue-task` 只追加干净文本；中途风格变化延迟到下一轮生效。
- **ForcedAligner/动作**：继续使用真实已提交 TTS 的 `spoken_text`、同一份 PCM 和稳定样本游标；不插入会改变锚点字数的标记；Room 停止/起播回执仍是物理证据。

**LLM 情绪意图时序的真实限制**：当前主链有 `response.text.delta`，但没有“保证先于首个文本到达”的标准 `ReplyStyleIntent` 事件。如果可用 FC 提前返回，可作为可选证据；如果没赶上首个 `run-task`，就用 Runtime 已知的用户声学情绪／Persona／当前任务语境构造保守风格，**绝不等待 FC、反复开 TTS 或加串行 LLM**。UI 必须区分 `llm_early`／`runtime_default`／`explicit_user`，不能伪称所有 TTS 情绪都是 LLM 实时规划的。

### 8.2 三层风格，不把“情绪”误当机械变速

| 层级 | 输入与输出 | 执行原则 |
| --- | --- | --- |
| 语言文本层 | `target_language/dialect` → LLM 自然词汇＋标点 | 文本方言和 TTS 发音应一致，避免普通话词汇硬套方言 |
| 表达意图层 | `neutral,warm,playful,sarcastic,curious,serious,sad,excited,calm` | Runtime 白名单；上下文/Persona 决定，输入 angry/sad 不等于输出同样 angry/sad |
| 声学控制层 | `instruction` + `rate/pitch/volume` | 适度偏移而非每次大幅改值；真实语速由 TTS 参数/声学韵律决定，标点停顿不可用任意硬插静音补丁 |

默认 `rate=1.0,pitch=1.0,volume=50`（实现前验证当前原始 WS 字段及范围）；候选微调 `rate=0.90–1.12,pitch=0.95–1.05,volume=40–60`，**只是工程测试范围**。例如温柔不等于强行降音量，调皮也不等于每句加速，严肃求助要压制嘲讽。无可靠意图则 neutral，不为了显得智能强制使用表演腔。

**真实双向流限制**：官方表情/笑声标签如 `[sarcastic]`、`[giggles]` 需要确认支持的合成模式；现有 `streaming="duplex"` 首期以 `instruction` 和整轮参数为准，**不直接把单向标签塞入文本**。同一句中多段情绪／非语言笑声单独做 M2 A/B，不能破坏首音延迟、字锚点和嘴型。句号、逗号如何影响声学停顿，首先真人试听，不预设固定毫秒节奏。

**关键一致性**：`input_observed_language`、`llm_target_language`、`llm_actual_text_language`、`tts_requested_language` 和 `tts_audibly_verified_language` 分开记录。后者必须真人或受验证的评测才写已确认，不能因为发出 `instruction` 就说确实播出了某方言。若已开始生成 LLM 四川话文本、而冻结 TTS 仍是普通话参数，记录 `language_mismatch`，让下一轮纠正而非本轮复写。

### 8.3 工作台已有页面的最小展示

在现有 React 工作台机器人/Chat 状态下方增加一行“语音表达状态”，诊断详情可折叠，**不另建新页面**：

| 显示项 | 示例／含义 |
| --- | --- |
| **用户输入** | `zh / 河南话(LLM 推断) / 情绪: happy (ASR)`，带时间和来源；unknown 原样显示 |
| **持久偏好** | `本 Chat 四川话 / 本机器人普通话`，明确 scope、来源、revision，允许手动设置/清除 |
| **LLM 文本** | `本轮目标河南话（首次临时）/ 实际文本语言待确认`；`current / next_turn / unknown` |
| **TTS 实发设置** | `目标河南话 / playful / rate 1.04 / pitch 1.0 / volume 50`，来自本轮冻结参数，不能从用户输入复制 |
| **旁路和延迟** | `running/late/failed`；首次识别、首文本、首 PCM、Room 首帧、speaker 回执分列 |

网页开关只控制服务端旁路功能配置，关旁路应保留显式 Chat/机器人偏好和原生 TTS 默认行为。不要显示隐私声纹样本、密钥、音色完整敏感标识或未经确认的“已识别说话人姓名”。

### 8.4 LangGraph 及声纹后置边界

**不要用 LangGraph 管每轮的语言优先级、首次跟随、TTS 情绪路由、写 SQLite**：这些是低延迟、可确定的 Runtime 状态任务；LangGraph 只保留已有按需复杂工作流／长期整理。

**声纹后置为独立专项**：未来已注册 `speaker_profile_id` 若可靠识别，只需先查询是否有该 profile 的**专属记忆**，有则按权限引用，没有则正常对话；始终不自动切换 Chat。当前 `memory_namespace` 仅 robot/Persona，没有自然人隔离，后续需要单独 owner 字段和隐私访问门槛。**不把声纹、注册、speaker verification 或私人记忆检索引入本期 M1／TTS 热路径**；未知、识别不可靠不使用个人记忆，声纹不能作为安全认证。

## 9. 分步实施与严格验收门槛

### M0｜基线／兼容验证（不变更当前行为）

- 固定当前 `main` commit、运行配置、音频源、ASR、人格和音色版本，记录旁路 OFF 的 **100 次正常问答 + 30 次插话**；记录首文本、TTS 首 PCM、Room 首发布、真实扬声器起播的 P50/P95、丢帧、CPU/RSS/网络、动作与音素偏差。若缺真实 speaker，明确“未取得端到端口径”，不得假装通过。
- 对比官方 `qwen3-asr-flash-realtime` 流式事件：记录 `language/emotion/text/stash` 是否存在、何时收到、短语种/噪声/混说的差异；对 3.1-ASR 仅做离线可切换 A/B，确认 `keep_dialect` 保留方言词是否对地域判断有信息增益。
- 配置默认 `ANNIE_ACOUSTIC_SIDECAR_ENABLED=0`；旁路失败不影响主服务 ready，不提交密钥、注册音频和用户原始 PCM。

### M1｜独立流式语种／情绪旁路 + 每轮 TTS 风格（最小可用）

1. `annie_adapters` 实现单旁路 WebSocket 事件解析器；测试 stable `text`、`stash`、`language`、`emotion`、session 重连、错误映射。识别事件只读，不写主对话。
2. `apps/runtime` 接入有界 PCM 广播与每头旁路独立任务，联结 utterance 身份与超时撤销；主 Realtime 的 20ms 送帧不等待旁路，断网时原有主 ASR 可正常回答。
3. 复用`realtime.py`现有指令合并器，以**小型提示**传入第7节的持久偏好、首次临时跟随候选与语境；区分本轮已应用／下轮候选／未知，不额外调用文本 LLM 或 LangGraph。
4. 在`tts.py`增加经官方 wire 实测的 `instruction/rate/pitch/volume` 白名单；在`speech.py`首个`run-task`前一次性冻结 `SpeechStyle`，不等情绪 FC。逐轮试听方言文本、自然节奏和情绪音色。继续保留 PCM/epoch/回执、取消、ForcedAligner 和动作字锚点。
5. 增加两张最小 SQLite 偏好表、经核验的显式设置／清除 FC、Chat 删除联动；**自动推断不得写表**，自动跟随不调用写库工具。
6. 正式 React 工作台新增第8.3节输入／LLM文本／TTS实发状态、偏好范围、真实生效时序和旁路开关；不重建页面、不显示密钥或声纹地址。

**M1 验收用例：**
- 普通话、英文、粤语各≥30轮；河南/陕西/四川话各≥30轮真人录音；英语↔中文混说、只说“嗯”、噪声、打断、短句。记录**语种准确率、LLM 方言推断的误判率、首次可用时间、是否赶在首个 token 之前**；地方话的官方 `dialect=unknown` 是真实模型边界，LLM 推断不能冒充 ASR 分类。
- 覆盖“长期普通话+首次四川话”“Chat 四川话+首次河南话”“连续同方言五轮”“沉默恢复”“明确设置 Chat/机器人偏好”“切 Persona/重启加载”“另一机器人不受影响”“任何说话者显式覆盖”“Chat 删除”。显式偏好写库成功／失败须能从 SQLite 与网页一致核实。
- **TTS 专项听测**：固定同一段内容与专属音色，对普通话／粤语／四川话／河南话分别做自然词汇、发音、顿挫／断句、语速、音调、响度、调皮／嘲讽／严肃／安抚的 A/B；打分与首 PCM P50/P95 同列，确认指令真实生效而非仅已发送。
- 重点验证 `smart_turn` 自动回复先发生而旁路后到的情况：仍正常播报、不等不重发、不修改已说文本，下一轮才能跟随；LLM 的语言选择与 TTS 设置一致。
- 两头 A/B 并行、切 Chat/Persona/Room、取消/重连、旁路 1–3s 卡顿、原始 PCM 队列满、HTTP/WS 故障、同气息唤醒、过期音频、失效 voice ID：不串机器人、不丢 KWS、不失忆、不重复答、不恶化 AEC。
- 保留已有 `pytest`、`npm test/typecheck/build`、`ruff`、`uv lock --check`，另测真实音频、浏览器及实体扬声器，没做的按“待验”。

### M2｜方言文本一致性与段内情绪（可选，需单独验收）

- 先测 `qwen-audio-3.1-asr-flash-streaming keep_dialect` 对广东话／四川话／河南话／陕西话在长短句的保留词汇；和 M1 `qwen3-asr` 在成本、首结果、语种/情绪能力上对照。
- 若“地域口音细分”无可信声学输出，依用户确认允许 LLM **弱证据推断**，但必须带来源且不得写长期记忆。另立声学分类器评估：标注集、窗口、误报与耗时；未达标不把推断伪称模型识别。用户显式选择方言仍能走输出链路。
- 仅对长句多情绪需求测试单向标签 TTS，与原 duplex 一一比较首 PCM P95、整句连贯、标签被念出概率、音频时钟和 ForcedAligner；达标后才讨论使用独立 `SpeechSegmentStyle` 与多任务策略。

### M3/M4｜声纹及专属记忆（后续独立专项，本轮不实施）

- 使用现有 Realtime 首次 `session.update` 配置 **`turn_detection.voiceprint_audio_urls`**，最大 5 个 16kHz PCM/WAV URL，记录 `in_progress/completed/failed`；注册失败不阻塞普通对话。
- 声纹素材需明确授权、加密访问与删除流程，不能将私人原始声纹随意放在公开 URL；官方要求服务端可访问 URL，采用短期签名地址并明确服务商可访问范围。
- 只声称“提升目标声音的关注”，不声称识别出用户姓名、可靠识别多个说话人或具备门禁安全等级。与 KWS/AEC/smart_turn 对照，核验误拒绝率。
- 后续仅需“识别到已登记的声纹 → 查询此声纹是否存在专属记忆 → 仅按授权使用”，**不自动切 Chat**。实际识别已登记用户需另外的 speaker verification/embedding，陌生人 unknown；现有记忆 namespace 尚无自然人 owner，不能把共享记忆冒充个人专属，缺少隔离时禁止个人召回。
- M3/M4 不纳入 M1 的实现、压测或交付承诺；不因此在本期增加新模型常驻、声纹数据库或 LangGraph 热路径。

## 10. 性能预算、证据与回滚

| 指标 | 拟定验收门槛（工程目标，非已有实测事实） |
| --- | --- |
| 热路径音频 fan-out | `put_nowait` 无网络等待；单帧额外开销 P95 **< 2ms**，队列积压可度量 |
| 主链首文本／TTS首 PCM／首发布 | 旁路 ON 相比 OFF 的 P95 **各不超过 +30ms**（相同音频、网络、温态分组；不以均值代替） |
| 旁路自身 | 测语音起点→首 `language/emotion` 的 P50/P95，不能用 ASR 模型停声回调代替物理起声时间；所有迟到均有计数 |
| 失败隔离 | 超时、鉴权失败、背压、云端异常、静音、无音频等情况下主链正常；100轮主问答不能因旁路增加语义重复响应 |
| 资源 | A/B 双机器人压测 CPU/RSS、网络带宽、请求数与费用；不能再出现多套侧车抢占导致主机卡顿；资源超标自动关闭旁路 |
| 风格有效性 | 语种正确率、方言误跟随率、音色保真、主观情绪自然度及反讽场景禁用率，人工标注，不把 LLM 猜测当标签真值 |
| 动作/嘴型/设备 | 原字锚点与输出文本一致，24kHz 帧序与游标不倒退，ForcedAligner 使用**实发**音频；设备起停播与浏览器试听证据独立 |

必需 trace（单调时钟，缺失为 null）：`mic_frame_received`、`main_audio_forwarded`、`sidecar_audio_enqueued`、`sidecar_first_language`、`sidecar_emotion_final`、`style_observation_accepted`、`session_update_sent`、`session_updated`、`response.created`、`llm_first_text`、`speech_style_locked`、`tts_run_task`、`tts_first_pcm`、`room_first_publish`、`speaker_started`。比对每个时点所属 robot/epoch/turn；不要跨设备直接相减不同基准时钟。保留阶段 9 低频汇总，不逐 PCM 写数据库。

**停止／回滚**：本专项开关 OFF 时使用原语音/TTS 路径；新增 Chat／机器人语言偏好采用**向前兼容的 SQLite 新表**，关闭旁路不删除偏好或改写现有 Ledger。异常需诊断但不阻塞；无旁路→已存显式偏好／原 Persona→默认 SpeechStyle→现有 TTS。旁路健康不计作核心服务健康失败。

## 11. 不确定点、遗漏盲点、失败风险与验收

1. **已明确实时优先**：首次方言判断赶不上首个 LLM 文本时不等待不补播；要保证第一次就跟随，需要改动 `smart_turn` 自动回复机制，**本计划明确不做**。
2. **已明确声纹后置**：M3/M4 只承诺未来已注册声纹查有无专属记忆，不自动切 Chat；身份注册、隐私权、历史可见性、拒识率需独立设计，不能误用 Realtime 声纹增强。
3. 地域口音真值标签和真人数据集尚无测评；`zh` 不能直接细分河南／陕西／四川。
4. 官方 `instruction`／原始 WebSocket `rate/pitch/volume` 兼容性、实际效果以及段内标签在本机复刻音色上的表现需真实音频 A/B 核验。
5. **遗漏盲点**：语种和省级方言不同、方言字词与发音可能不一致、LLM 选择的情绪意图可能晚于 TTS 首任务、单向情绪标签与 duplex 不兼容、标点停顿与分段会破坏自然韵律、多说话人及回声会污染观察值。
6. **失败风险**：旁路延迟→下轮而非堵主链；识别误判→仅试探性本轮口语不永久写偏好；提示状态迟到→不得声称本轮已生效；TTS 参数不兼容→回退默认；风格与音频文本错位→停用实验分支；个人记忆误归属→完全禁用个人召回。
7. **明确验收**：第7节的状态迁移、SQLite 重启与跨机器人隔离全部有证据；第8节的实际 TTS 方言音质/自然节奏/情绪控制作盲听 A/B；第9节 100轮/30次打断与真人方言集；第10节双头 P50/P95 指标、Room 真机、嘴型/动作不回归。未测项保持待验，不能用功能存在代替效果合格。
8. **更好的实施提问**：“仅实施 brainv2 方言/情绪 TTS 计划的 M0+M1：沿用 Realtime smart_turn 和复刻音色，单独异步 ASR 旁路读取 language/emotion；首次可及时跟随、后续智能延续；机器人/Chat 显式偏好写 SQLite；每轮 TTS 风格冻结，首 PCM 低延迟；React 明确展示输入、LLM 和 TTS 三层状态。以真人方言/音质和双头性能报告验收。声纹/个人记忆暂不实施。”

**本专项结束的定义**：M1 达到“语言/情绪旁路 → LLM 方言文本／后续智能延续 → TTS 方言+节奏+情绪控制 → 实发 24kHz PCM → 真实回执”闭环；Chat/机器人显式偏好持久、网页显示正确，延迟与动作、嘴型、唤醒回归通过。声纹与个人记忆独立后置，不参与本专项结项。

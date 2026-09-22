# Phase 7｜真实动作、表情、简单工具与小脑协同

## 本轮开工与边界

- 用户于2026-09-22安排第七阶段代码交付并准备统一测试。**软件执行链已接通，真实动作仍默认关闭，尚未核验真机接收、动作完成或停止回执；不能签发物理闭环PASS。** 不把模型选中工具、Room发送成功或浏览器动画当作物理执行成功。
- 最终目标：机器人A执行已确认的动作，B不动；表情与语音对应；重复、无效、过期、取消、设备无回执均有如实状态与工作台记录。现有Realtime/TTS/嘴型/六通道不变，简单动作不进LangGraph。
- 旧合同：config/action_scripts.json有34个enabled脚本。action_trigger发送至reb_main，含script_id、offset_ms、conversation_id、anchor_source；offset以真实首PCM播放sample锚定。expression发送至reb_tts。协议没有action_cancel；小脑action_prep取消Brain/TTS时须保留小脑接管动作。既有preview_action仅模拟，不是物理执行工具。
- 安全开工成果：packages/annie-brain/src/annie_brain/action_catalog.py只读加载并校验现有目录，拒绝重复ID、禁用动作、非法时长、附加target和模型自造脚本；tests/test_phase7.py为15项纯目录和白名单测试，不连接Room或触发真实电机。已有Python全量168项，增加15项后183项通过，30 subtests通过；Ruff通过。

## 必须确认后才能开启真实物理执行

1. 首批脚本：建议从已列出的agree_micro_nod、look_left、look_right、fast_double_blink中选少量核对接收端；计划示例“挥挥手”不在当前34个ID中，不可自行创造wave。表情的喜/怒/哀/乐/中要确认接收端具体值、分段和持续时间。
2. 协议与设备回执：需要接收端证明reb_main/action_trigger、reb_tts/expression实际消费的消息，以及动作开始、完成、失败、停止的回执来源与字段。没有回执时只能报告已提交或不确定，不能回写成功。
3. 物理安全与小脑：确认首批脚本幅度、部位、冷却、动作冲突、执行超时、取消后的收尾和小脑即时动作优先级。确认本期具体简单工具（例如已有天气配置是否真的应接入），未选定前不建技能平台。

## 后续待验（P7-W1/P7-W2）

- Realtime Function Call → ID与参数白名单及robot/generation/turn/call_id核对 → 所属Room与小脑 → 真实状态回写 → 工作台与模型后续表达。旧generation和重复call不得执行两次。
- P7-W1：在React工作台观察A的请求、校验、执行和回执；B正常聊天；真机分别确认动作/表情/音频/嘴型。
- P7-W2：重复、非法参数、超时、部分执行、取消、跨机器人与小脑抢占；无设备证据始终显示不确定。无新增独立HTML验收页。

## 2026-09-22 软件实现与现场验收更新

- **执行链已落地**：Qwen-Audio Realtime 的 perform_action、set_expression 与 get_weather 经严格参数/当前用户请求/turn与generation校验，调用本地 SQLite 事实记录。动作只接已有ID，默认允许 agree_micro_nod、look_left、look_right、fast_double_blink，其他不发。每机器人独立 Room 发布与状态；同回合相同动作、重复call_id和网络重试不二次执行；目录时长内冷却、冲突、取消及15秒内没有首帧 PCM 时过期。
- **PCM与外部合同**：动作等到本轮第一帧 PCM 确实发布后才通过所属 reb_main 发 action_trigger，data.script_id/offset_ms=0/conversation_id=本轮turn_id/anchor_source=robot_main_brain。表情仅采用喜/怒/哀/乐/中，随本轮 TTS 通过同一 reb_tts 出口在本轮首PCM前发送。main 出口检验 WELCOME 房间/通道身份并尝试预热。现有协议没有可信动作完成/停止回执，也没有action_cancel；故仅能区分等待PCM、已发未知、发送未知、取消未发、过期/失败。已发动作不能声称撤销，不能让LLM声称身体已动。
- **小脑与天气**：小脑 action_prep 只在当前 ASR 身份匹配时中止Brain/TTS与尚未发布的语义动作，保留小脑接管动作，不影响B。简单天气工具只用用户当前明确说出的城市请求真实高德实时接口，返回来源、报告时间、气温等；密钥缺失/失败不编造。
- **工作台**：顶部「动作」显示当前 robot_id 的SQLite真实执行记录、等待/失败/未知状态、动作目录和本机白名单；示例动作按钮仅填入对话输入框，不直接发电机命令。不新增独立Phase7 HTML页。
- **启用**：默认 ANNIE_ACTIONS_ENABLED 未设置时仍为关闭。只有确认接收端、安全幅度及测试环境后，停止麦克风并安全重启 Runtime 时配置 ANNIE_ACTIONS_ENABLED=1，同时需 ANNIE_TTS_ENABLED=1、DASHSCOPE_API_KEY、正确Room；可用 ANNIE_ACTION_ALLOWLIST 进一步收紧允许的目录ID。建议首先仅放行 agree_micro_nod。旧 preview_action 在动作关闭时仍是模拟；动作启用时从模型工具列表移除，不在真实执行中混淆预览。天气沿用 AMAP_WEATHER_API_KEY。
- **验收证据**：tests/test_phase7_execution.py、tests/test_phase7_runtime.py、tests/test_phase7_wire.py 覆盖真实SQLite与Fake Room、双机器人隔离、动作/表情旧wire、去重/冷却/取消/过期、ASR身份匹配、ASGI工作台接口和天气Mock；web/tests/action-panel.test.mjs 验证仅填入且未出现直接动作POST。隔离的真实Qwen-Audio Realtime三例：“点头”触发 agree_micro_nod、“眨两下眼”触发 fast_double_blink、“北京天气”触发 get_weather；真实高德北京市实时天气返回成功。这些都**没有**执行真实物理动作。
- **审查返工（2026-09-22）**：修复“问你有没有点头”等叙述/疑问误命令、用户要求看右边却被模型选看左边、表情喜哀不匹配；只能执行当前输入明确对应的四项命令或五种表情。新的目录ID需专门补经审查的语义规则，修改白名单本身不代表可安全执行。修复重启残留awaiting_audio_anchor阻断后续动作、冷却时间按前后两动作较长者计算、首PCM已发送仍注册动作而空等15秒、上一轮TTS进行中发送下一轮表情、Room发送故障未写入SQLite。若Function Calling先于当前最终ASR，明确回写unconfirmed_current_turn且绝不执行，不能像此前静默丢工具；需由真人连续语音验收确认这种保守降级出现频率。
- **自动化结果**：Python 全量 212 passed、30 subtests passed；前端24 passed；Ruff、TypeScript、Vite生产构建、uv锁和diff检查通过。两条警告来自第三方弃用接口，不是设备验证证据。阶段7源码从共用文件中只选择本阶段差异入Git；先前未提交的Phase6、旧Web/sidecar修改不擅自提交或清理。
- **现场P7-W1**：未启用时先核验模拟/未知状态；安全启用后A说“请你点下头”，工作台与接收端逐条核对工具→PCM→reb_main→设备动作，B独立聊天；表情及对应TTS和嘴型分别核验。
- **现场P7-W2**：重复相同call、无效ID、取消、过期、旧generation、网络失败、小脑action_prep及B不串动作；没有实际设备回执时只给unknown，真人物理完成/停止和Windows双头仍待用户人工签收。计划中的“挥挥手”不在34个已有脚本，不能擅创ID。

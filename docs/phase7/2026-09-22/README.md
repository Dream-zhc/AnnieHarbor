# Phase 7｜真实动作、表情、简单工具与小脑协同

## 本轮开工与边界

- 用户于2026-09-22明确安排开始第七阶段。实际状态：**动作目录校验已落地，真实设备执行未启用，Phase 7 尚未完成**。不把Function Call模拟结果、Room发送成功或浏览器动画当作物理设备成功。
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

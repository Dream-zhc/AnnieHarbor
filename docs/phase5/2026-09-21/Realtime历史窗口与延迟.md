# Phase 5｜Realtime 历史窗口和延迟：最小修改计划与复核

## 当前真源

- 正常对话只有 Qwen-Audio 3.0 Realtime Flash 负责 ASR、smart_turn、LLM、Function Calling；没有外置聊天 LLM，也没有每轮重传普通 Chat Completions messages。
- 阿里云 Realtime 在同一 WebSocket Session 中自行维护历史；官方 max_history_turns 默认 20、范围 1–50，并建议短问答尝试 5–10 轮来降低延迟。
- SQLite / 本地 Context 仍按 20 轮缓存且全量持久化；云端推理窗口与本地缓存是不同配置。重连、切 Chat、切 Persona 时新建 Provider Session，并从当前身份的已确认历史中只注入一次有界内容。

官方资料：
- https://help.aliyun.com/zh/model-studio/qwen-audio-realtime-user-guides
- https://help.aliyun.com/zh/model-studio/qwen-audio-3-0-realtime-flash

## 本次最小改动

1. 保留 conversation.history_turns=20，不动数据库；新增 conversation.realtime_history_turns=8，传入 Provider 原生 session.update.max_history_turns。8 是可调试起点，不是已测得最优数。
2. 重连/切换的首次注入限制为最多 8 轮、3000 字符；基于已播证据和完整用户更正选整轮，不把未播助手文本装成已说。正常每轮**不注入历史**。
3. 两个上限可通过 config/runtime_defaults.toml 配置，或机器本地 ANNIE_REALTIME_HISTORY_TURNS（1–50）、ANNIE_REALTIME_RESTORE_CHAR_BUDGET（1–12000）覆盖，保留按现有 Session 切换语义重建；非法值启动前拒绝。
4. 工作台的开发者诊断展示云端推理轮数与首次注入字符预算，与 SQLite 全量历史明确分离。Provider 如果回报的轮数不一致，不静默放行。

## 不做与边界

- 不接旧外置 LLM，不新加 tokenization、摘要/意图模型、redis 或额外请求；Persona 只在 Session 建立时发。
- 3000 是**字符**，不是 Token 精确限额。此型号 Realtime 文档有原生轮数参数，但没有在 session.update 给出可承诺的精确输入 Token 截断参数；音频 Token 和工具回执也不会被字符计数覆盖。
- 模型信息页注明 qwen-audio-3.0-realtime-flash **不支持上下文缓存**。不宣称使用其他 Qwen 的 Context Cache 能在本型号生效，也不声称仅减少历史必然缩短网络、ASR、TTS 总延迟。
- 云端会话可能已经有新音频历史；重连注入一次并非再从此每轮上传 8 轮。窗口和重连注入各自有上限，不能把它们相加为精准 Token 数。

## 审查与现场 A/B

回归验证：Session 初始配置传 8、错误范围拒绝；假历史保留 20、重连实际发送最多 8 且不超 3000 字符；本轮输入不会再触发历史注入；两机器人独立；实际已播/未播和新旧轮回归继续通过。测试仅能验证客户端代码，不代表 Provider 真正压缩了多少 Token。

现场用同一 Realtime 模型/设备/网络/Persona/音量，在 ANNIE_REALTIME_HISTORY_TURNS=8 和 20 下各重复三次以上“茶→咖啡→继续刚才”与约 20 轮长聊天，记录云端起声→ASR final、ASR final→首 text.delta、首 TTS PCM、设备首播的 P50/P95 及错答。若 8 轮导致明显指代遗失，可改回 10/20；判断是否省钱应以百炼账单或实际服务端用量为准，不能把字数当 Token 消耗报告。

**验收状态**：代码和 mock 证明配置/裁剪正确；成本和首响改善须真实 Provider A/B 后才可写 PASS。

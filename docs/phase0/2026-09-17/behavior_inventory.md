# 行为清单：保留 / 已知问题 / 明确删除 / 待确认

## A. 必须保留（兼容业务结果，不继承V1架构）

| ID | 行为 | 证据 / V2验收 |
|---|---|---|
| KEEP-01 | WS ASR和Web文本进入对话；没有回复义务的输入不调用正常主脑输出 | room_protocol.extract_asr、Web text_input、Gate运行trace；PASS/HOLD/DROP语义由V2计划锁定 |
| KEEP-02 | Chat/Persona上下文正确；默认增加robot_id隔离 | V2计划9/27；旧版不证明多头隔离已存在 |
| KEEP-03 | 当前MainLLM与TTS先做同模型/音色对照 | effective_config.json中的request、settings、switches与旧日志 |
| KEEP-04 | 音频、动作、嘴型和表情保持现有wire字段及含义 | external_contract.json；conversation_id整轮、turn_id分段、PCM sample clock权威 |
| KEEP-05 | 过期/身份冲突小脑cancel不取消当前新回合；action_prep取消主脑/TTS但保留小脑动作 | fixtures/cancel_identity.jsonl + test_small_brain_cancel.py |
| KEEP-06 | 实际播出的文本优先于未播完planned文本进入下轮Ledger | V2计划11；新Ledger按robot_id建边界，不继承共享history |
| KEEP-07 | 真实PCM决定嘴型声学边界；不伪造拉伸/静音填充 | 当前PROTOCOL、_deliver_mouth拒绝非ForcedAligner时间源；summary为观测不是生成依据 |
| KEEP-08 | 输出回流/控制握手不触发新Turn；旧回合音频不重播；可见超时/断线状态 | _on_main_message、TtsRoomChannel.message_should_drop、VisemeEngine.on_sam_message |
| KEEP-09 | Global/Persona长期记忆检索边界与少而准召回 | V2计划9/10；长期记忆默认按robot/user/persona边界隔离 |
| KEEP-10 | 单进程、本地SQLite、独立迁移、macOS/Windows启动目标 | V2计划和PROJECT_RULES；不等于Phase 0创建启动器 |

## B. 已知问题（证据分级，不称为本次复现）

| ID | 现状 | 证据 | 改造方向 |
|---|---|---|---|
| ISSUE-01 | 固定TTS等待1800ms与“正式默认0”注释冲突；四条trace出现约1801–1877ms等待 | settings.py:50、运行trace的tts_session_sleep_ms | V2不继承固定等待；把SDK建链/Provider首包与项目人为等待分开测 |
| ISSUE-02 | TTS全局锁、单热池和单应急槽限制并发 | aliyun.py:_POOL_USE_LOCK、max_size=1、Semaphore(1) | per-robot session/timeout/取消；共享资源独立限流，不串行所有回合 |
| ISSUE-03 | 嘴型sidecar只有一个_active_cid，切换cid重置上一轮；全局pending/control及singleton | qwen_stream_room_g2pw.py:on_tts_message/reset_conversation/on_viseme_control/main | 明确实例化状态及输出目标；不能直接复制现行调度 |
| ISSUE-04 | POC README/requirements与现行listener对齐路线不同 | 启动脚本、requirements-v3、ForcedAlignerRuntime | 当前路线优先，POC作为历史参考，不复制Allosaurus依赖集 |
| ISSUE-05 | 观测尚无扬声器实际起播时间 | 9/17 runtime_observability.tts.playback_started=null | PCM publish不是用户听见；接收端回执需补采样 |
| ISSUE-06 | 现有输入出口有单BRIDGE与全局配置假设 | service.py末尾BRIDGE、Web接口调用、共享TTS全局 | 按V2契约重写入口/生命周期，不作为新结构模板 |
| ISSUE-07 | 当前trace仍有旧动/中前缀，配置注释仍称结构化JSON预算 | 9/17 llm.response.raw；settings.LLM_MAIN_BRAIN_MAX_TOKENS注释 | 原始LLM内部输出不是外部契约；V2不照搬前缀解析历史格式 |
| ISSUE-09 | summary经过口型时间源校验而被拒，文档预期与发送路径不符 | final→send_mouth(summary)→_deliver_mouth要求timing_source；9/17 MOUTH_DROP source=-、sync_summary=null | 观测summary与可执行口型分开出口和校验；V2保留观测契约不继承错误分支 |
| ISSUE-08 | 9/10历史全链路报告14PASS/4FAIL | evidence/regression_report.json | 把失败作为回归候选，不认定9/17仍然复现 |

历史4FAIL分别为：action_and_wink（报告认为否定语境动作不符）、vision_face_emotion（人脸姓名未进入runtime_context）、sensors（回复泄漏“传感器”内部字段）、vision_object_gesture（物体label未进入runtime_context）。报告版本/场景/模式可能不同，保留原summary，先核对预期再设计V2回归。

## C. 明确删除 / 首版明确不迁入

以下由V2计划决定，不是在Phase 0修改或删除旧代码。

- V1 RobotBridge历史耦合、运行时父目录import和旧配置bootstrap副作用。
- 旧双LLM竞速快路径、普通陪伴回合额外串行intent/emotion/planner/memory judge模型；保留Gate回复义务语义，但不保留其模型实现。
- 首版本地ASR/KWS/VAD/Pipecat/SmartTurn、完整全双工；不把project/qwen_duplex/AEC音频链搬进Brain。
- 视觉/传感器自动创建普通Turn、自主主动开口；改为World/Control/Action输入。
- 天气/提醒/复杂技能/Proactive/Scheduler先后置；已有服务信息保留盘点，不实现。
- 首版额外Redis/Kafka/常驻Docker/独立数据库、提前创建未用框架与空壳Adapter。
- 固定TTS睡眠与口型文本均分/伪造拉伸不是兼容义务。

## D. 待确认（信息不足不猜字段）

| ID | 问题 | 所需证据 | 对下一阶段影响 |
|---|---|---|---|
| OPEN-01 | 同房间如何定向多设备？ | Java Room Server协议、目标字段/连接绑定及接收端过滤 | 阻塞真实同房间多头发布；不阻塞FakeRuntime隔离验证 |
| OPEN-02 | 小脑在哪个项目启动、用什么模型、是否另有action消息？ | 发送端仓库/启动配置/真实ASR→cancel/action记录 | 当前只能锁定action_prep cancel消费契约 |
| OPEN-03 | 算法/雷达/嵌入式完整字段？ | 发送端schema与真实业务packet | robot_event envelope支持不意味着payload定义完整 |
| OPEN-04 | 定制Annie音色当前账号/地域可用吗？ | 同账号有效请求/运行健康记录 | 静态音色ID不是在线可用性验证 |
| OPEN-05 | 设备是否实现played_sample_count、队列锚点、clear/drained及动作收尾？ | 接收端源码与真实播放回执 | 防止只测publish就声称声画同步 |
| OPEN-06 | 同一用户的记忆是否跨机器人共享？ | 显式产品决定和scope配置 | 默认隔离；人格关系/短时上下文不共享 |
| OPEN-07 | 房间重放/稳定事件ID/去重窗口怎样定义？ | server协议及重连抓包 | 未确定前不能保证网络exactly-once |
| OPEN-08 | Phase 0有完整真实wire样本吗？ | ASR/cancel/TTS/嘴型/视觉/算法原始packet及对应输出 | 现有doc/test/trace投影已区分，完整抓包仍缺 |
| OPEN-09 | 正式延迟/质量基线是否足够？ | 固定Companion场景、更大样本、真实设备起播 | 当前4条历史成功用户样本不是性能验收 |

## E. 多机器人与陪伴回归设计

`fixtures/multi_robot_acceptance.jsonl`只有未来验收场景和预期，不运行伪实现；`fixtures/companion_eval.jsonl`覆盖11类陪伴场景、固定控制条件和评分维度。两者都是设计资料，未调用模型或设备。

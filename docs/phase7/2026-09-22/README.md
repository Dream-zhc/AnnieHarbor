# Phase 7｜动作、表情与简单工具摘要

> 2026-10-10精简旧开工和重复返工记录。保留该阶段最终软件边界及未闭合风险；后续字级动作能力见[同步协议](../../规范/字级动作与嘴型同步协议.md)与最新开发记录，不用历史脚本数/白名单推定当前配置。**设备物理完成/停止尚待统一验收。**

## 软件链路与硬边界

- Qwen Realtime Function Calling提出动作、表情、天气；简单工具不进LangGraph。目录只读加载并校验真实ID、enabled/时长，拒绝重复、非法参数、附加target或模型自造脚本。实际目录与允许集查 `config/action_scripts.json`及本机配置。
- 每robot独立turn/generation/call_id、Room、幂等、冷却与冲突；重复call/同轮动作不二次执行。旧generation、未确认当前ASR、取消、过期/无PCM明确拒绝。FC早于最终ASR时回写unconfirmed_current_turn，不暗中执行。
- 后端已移除手写中文动作句式白名单，Qwen按目录description选择；仅保留明确否定/取消veto及硬约束。**模型语义仍可能误判**，目录允许不等于用户意图可靠确认。
- 该阶段action_trigger经所属reb_main发布，含script_id/offset_ms/conversation_id/anchor_source；动作等真实首PCM锚点，表情经同一reb_tts出口在首PCM前发送。在途PCM用inflight_end及共用WS锁二次检查，避免仅检查已完成frames导致错过音频边界。后续字锚点扩展按同步协议，不在本文重新定义wire。
- action_requests迁移/查询/CAS/崩溃恢复由annie-adapters RuntimeStore承担，调度器不自行建SQLite表；保留历史及记忆数据。发送前原子记录send_unknown并释放robot锁，网络等待不挡同头取消或B并发；已发送/可能已收不盲目重试，也不伪报cancelled_before_send。
- 小脑action_prep在当前ASR身份匹配时取消Brain/TTS及未发布语义动作，保留小脑接管动作，不影响另一头。协议无可信action_cancel、完成或停止回执；Room成功发送不证明电机动作或严格物理起声同步。
- 工作台动作面板展示真实SQLite记录、目录、允许集与等待/失败/未知状态；示例只填对话输入框，不直接POST电机命令。preview_action仍是模拟，不能混为真实执行成功。

## 开关与未解决风险

- 真实动作需显式 `ANNIE_ACTIONS_ENABLED=1`、TTS/Key/所属Room及接收端测试前提；`ANNIE_ACTION_ALLOWLIST`收紧允许集。幅度、部位、冷却、冲突、超时、小脑优先级和真实回执未确认前不扩大执行范围，计划示例不授权发明wave等ID。
- 自主轻动作另需 `ANNIE_SOCIAL_ACTIONS_ENABLED=1`，仅向perform_social_action授予micro类及agree_micro_nod，不包含头部大动作。
- **已知未闭合：关闭社交工具不能保证普通perform_action绝不误触发。** 当时隔离复现“我完全赞成你”被模型普通工具选为agree_micro_nod仍返回awaiting_audio_anchor。若要求硬保证，需单独明确意图授权或用户确认，不能恢复中文正则并宣称已解决。
- send_unknown后取消不能保证电机停止；无可信动作回执持续unknown，不让模型宣称身体已动。双头物理验收、真实自主轻动作选择频率/误触发率待测。

## 天气与历史实证

- 仅对当前用户明确城市调用已有高德接口，不凭关键词编造结果；缺Key/超时/错误可解释。历史城市歧义：西安被解析为黑龙江西安区231005，西安市/610100才是陕西西安。保留城市名称归一/返回校验，显式区县/adcode不误改。
- 该阶段复用1200ms超时、180秒缓存，异步单飞合并同城请求，成功结果带来源/报告时间/cache_hit/fetch_ms；无新增LLM/Graph或无限重试。当前值以配置为准，未测真实语音端到端改善。
- 隔离真实Qwen三例分别选择点头、双眨眼和北京天气工具；高德北京市实时天气返回成功。**这些没有执行真实物理动作。**
- 阶段最后一轮自动化Python221项、30子测试、2项第三方弃用警告，前端24项及Ruff/类型/构建/锁/diff通过；不是今日计数或真机通过证明。

## 回归与人工验收

长期 `tests/test_phase7*.py`覆盖目录、SQLite迁移/恢复、Fake Room旧wire、去重/冷却/取消/过期、ASR身份、在途PCM、慢网络不持锁、社交授权、天气与机器人隔离；前端动作面板测试保留。

- P7-W1：工作台选择A，核对工具→PCM/字锚点→reb_main→接收端动作与真实回执，B独立聊天；表情、TTS与嘴型分别验。
- P7-W2：重复call、非法ID、旧generation、超时、取消、部分执行、Room失败和小脑抢占；确认设备实际开始/完成/停止，不把已提交未知当完成。

入口仅现有React工作台，不新建HTML。验收前按真实接收端合同确认样本，自动化/Mock和物理结果分列。

# Phase 5｜上下文、人格与语音交互摘要

> 整理：2026-10-09。合并旧返工计划；只保留有效实现、可复跑案例、历史证据和未闭合问题。近期修复以当前代码与 `tests/` 回归为准。
> **软件回归不等于Phase 5真实体验PASS**。真人多轮、实体播放、AEC和嘴型待统一实测。

## 现行链路与上下文

- 普通对话由Qwen-Audio Realtime负责ASR、smart_turn、原生文本与Function Calling；旧外置Chat LLM/Cascade试验已移除，不作为待恢复能力。独立TTS→所属Room PCM→ForcedAligner保留。
- SQLite保存完整历史，本地近期缓存20轮，云端原生推理窗口和首次恢复字符预算分别配置。历史基线为8轮/3000字符，可调；当前值以 `config/runtime_defaults.toml`、本机覆盖和Session快照为准，字符不是精确Token。
- 新Session/重连/切Chat或Persona才注入一次有界完整历史，不每轮重发。用户最新更正保留；有TTS时助手历史以真实speaker drained及实际spoken_text为依据，生成/Room发布/browser_test不冒充已听到。无确认的完成输入可按有界user-only规则恢复，旧Ledger不删除。
- 较旧发言可按可靠created_ms加历史时间说明，仅改变Provider注入文本，不改数据库原话；没有可信时间不猜。长时间无业务且没有听/答/播时安全回收Session，一个空闲周期不反复重连，Robot/Chat/Persona归属不变。
- SocialState的追问偏好沿用 `normal/low/none`和 `source=user_setting`；Persona共性身份/事实/工具规则一致，四种表达素材只影响风格。Persona按Session/revision加载，不增加情绪/摘要模型或通用问号裁剪。

## 轮次、打断与播放

| 问题 | 保留的处理与边界 |
| --- | --- |
| 旧ASR抢答新问题 | 带item_id的新起声使前一未确认item失效；迟到stopped/delta/ambient/final不抢新轮。旧response也不能结束新轮。没有ID不靠时钟猜精确归属。 |
| 未播助手污染上下文 | 取消旧response、TTS和嘴型，并请求删除有ID的未播助手项；请求不等于删除确认，无ID显示unverified。已真正drained的回复不删除。部分播出无可靠字词边界时不捏造半句。 |
| 候选插话与环境声 | 起声候选HOLD；无效turn/ambient恢复原采样位置，有效输入清旧队列。电平只观测，不代替语义判定。 |
| TTS背压/尾包 | 有界队列与pending保留文本，finish不阻塞Realtime读事件，晚到finish仍唤醒消费者发送尾包；显式取消保持STOP合同。每轮一个Provider task与声学segment，不把文本提交当音素边界。 |
| 短句首响 | 达到既有字符/等待上限后提交首段，保留连续PCM节奏、缓冲和单任务；无实测不将等待预算简单相加成固定延迟。 |
| 热链负载 | Room仍按20ms原帧发布，只限频UI进度；ForcedAligner未解析长PCM采用有界预算/递增重试，final仍尝试真实对齐；逐PCM日志默认关闭。不用停止能力冒充优化。 |
| 网页渲染/试听 | 麦克风电平和播放缓冲独立更新，诊断抽屉关闭不额外驱动主界面；试听默认手动开启，只消费所属reb_tts，不合成第二份TTS，切对象/离页释放资源。 |

播放字段及物理游标见[播放端协议](../../phase4b/2026-09-20/播放端WebSocket协议.md)；新增 `reb_voice`、sender/source和Hold/Resume去重见[语音信令](../2026-09-21/语音信令与全双工.md)。原六通道和已确认配置不删除，网页试听不替代speaker证据。

## 保留的历史证据与缺口

- 2026-09-21本机只读连接roomv2/reb_tts曾收到WELCOME和订阅ACK；未发布现场测试音频。模拟Room与Chromium/原生AudioContext验证PCM调度、重复seq、stop及切对象释放，不代表耳听、AEC或实体扬声器通过。
- 同日排障发现TTS `tts_queue_full/tts_queue_timeout`触发STOP，约65帧后停播；据此补背压/finish回归。累计Room/Provider送帧不能证明同一Session里有效人声或ASR成功。
- 同日一轮ForcedAligner约13.68秒PCM的对齐耗时3859.5ms且无事件，逐帧TTS_RX日志超过16000条；按此限制UI更新与重复对齐。全机16GiB内存及约6.55GiB swap不能全归因V2，也没有两套侧车并发的现场证据。
- 历史自动化覆盖旧item迟到、旧response交错、取消助手项删除、完整播完不删、短句首段、有界历史、更正、长文本背压和finish晚到；2026-09-21一轮性能修改Python139项/25子测试、sidecar预算2项通过。这些是当时结果，不是当前全量计数。
- 2026-09-23真实Qwen文字复跑“简单喜好/简短回答/换题/练鼓困扰”：修改Prompt后“喜欢喝茶”“绿茶”仍有多余追问，`low`亦未可靠压住。**追问质量当时未通过**，保留为后续音频多轮复验案例；不能因四Persona或规则存在就宣称稳定自然。
- 无真实Qwen删除确认、无可靠设备样本游标或逐字对齐时，不能宣布服务端已播上下文完全净化；长句预算保护可能产生尾段嘴型延迟/缺失，不等于高性能嘴型算法已验收。

## 统一复测入口与用例

仅使用正式React工作台 `/?view=workbench`。旧 `/phase-tests/`与 `tests/html/`已移除，不恢复。操作方法与声学归因见[语音闭环统一测试](../2026-09-21/语音闭环统一测试.md)。

| 连续操作 | 预期及取证 |
| --- | --- |
| 喜欢茶→改成咖啡→刚才更正什么 | 最新事实咖啡，旧item不再触发回答；查item/turn/response与恢复快照。 |
| 讲故事→有效插话问别题→继续刚才 | 新轮不播旧队列，已取消的未播内容不当承诺；查TTS/设备游标和删除确认。 |
| 今天累不想讲原因→别再问→知道了 | 收住追问，区别用户偏好与模型实际行为，不按关键词判质量。 |
| 退下→空闲60分钟→我喜欢喝茶 | 新轮正常承接，旧一次性指令不延续；查Session回收与实际生成/播出，不清掉真实长期偏好。 |
| 耳机仅试听→耳机开麦→外放1× | 区分Provider/Room/播放器断粮、麦克风误触发、回灌/AEC；同Room始终一路麦克风，真实扬声器播放时关网页双声源。 |
| 长句/误Hold恢复/重连/切Chat/Persona/Robot | 连续PCM不丢，目标隔离，旧结果不串新身份，实际物理started/held/resumed/stopped/drained齐全。 |

失败样本记匿名身份、模型/Persona revision、ASR final、item/response/turn、Session恢复快照、生成文字、Ledger与设备回执，以及首文本/TTS首PCM/Room首发布/设备首播P50/P95和样本量。文字正确但播旧音查播放队列；输入归属错误查轮次；输入事实正确却答旧题查模型语义。不要保存密钥或私人原始对话。

Python回归在 `tests/test_phase5.py`及相关Realtime/TTS用例；前端回归在 `web/tests/`。具体命令沿用[项目入口](../../../README.md)。自动化/Mock、真实后端、真实Room、物理设备、人工体验分列，缺项继续待验。

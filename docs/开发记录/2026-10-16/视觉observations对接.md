# 视觉 observations v1 与实时大脑对接

> 2026-10-16。视觉端在 `reb_visualllm` 消息中新增 `observations`（version 1）：每秒一条，`people[]`（`subject_id`、`label`、`is_gaze_target`、`position`、`action`、`holding`、`expression` 等）、`looking_at`、`environment`、`objects[]`、`changes[]`；同一条消息内同帧绑定；字段只增不改，不兼容时升级 version。用户要求拿视觉数据，结合实时大脑优化并对接。

## 真实数据（视觉恢复后，camera-a → roomv2）

`GET http://192.168.1.13:8766/api/vision/outbox`、`/state` 两次各取最近 20 条 visualllm（只读，分析后删除，不入库）：

| 项目 | 实测 |
| --- | --- |
| 字段 | `observations.version=1`、`people_count`、`looking_at`；人员含 `subject_id/label/name/known/identity_state/identity_confidence/position{side,distance,text,bbox_norm}/action/holding/expression/is_gaze_target`；`environment` 为字符串；`objects`、`changes` 为列表。原有 `description`、`facts`（v2）、`people`（原始框）仍在 |
| 频率 | 每秒 1 条（间隔 1002～1062 ms） |
| 画面→发出 | 中位数 1.0～1.3 s（895～1769 ms），模型单次约 0.9 s（`last_inference_ms`），不是说明里的 0.35/0.5 s |
| 描述变化 | 20 条里 20 次、16 次（措辞每帧不同） |
| 人数抖动 | 同一场景人数 2→1→2→3→1→3：画面边缘的人时有时无 |
| 一人两条 | 右侧同一人同时出现 `person_10`（只有身体框，`no_face`）和 `face_4`（只有脸框），脸框位于身体框上部，被说成“3 个人” |
| 认错名字 | `face_4` 有一帧被识别为主人姓名，同帧出现两个同名；主人 `identity_confidence` 0.524 |
| 动作同义词 | 同一人逐帧为“专注看屏幕/专注看电脑/学习专注/学习电脑” |
| visualdata | `vision.people` 约 17 条/秒（框、身份、机器人注视，0.5 s 有效）；`vision.perception` 人脸约 17 条/秒、手势约 10 条/秒，几乎全是“中性/idle” |

## 设计

```text
reb_visualllm 1 条/秒 ──observations v1──► RoomMonitor
   ├─ PeopleSmoother：在场滞回（3 条里见到 2 条算在、连续 5 条没见到算走），
   │   名字需连续 2 条一致，同名只给机器人正看着的/置信度最高的，未识别者按顺序叫“一位朋友/另一位朋友/又一位朋友”，
   │   出现/离开由平滑结果产生，一来一回互相抵消，开头 3 条不算出现
   ├─ 紧凑画面：【眼前画面，N人】你正看着X（位置）：动作，手里拿着…，表情…。其他人…。周围…。物品…。刚才…。
   └─ 更新节奏：在场的人/注视目标/出现离开 立即；措辞、距离、动作、表情 最多 5 秒一次
        ├─► Realtime session 指令（替换上一份画面，不当作用户发言）
        └─► “对谁说话”决策模型的“画面”字段
   └─ facts（v2）照旧交给 IdleService 判断现场有人
reb_visualdata vision.people 17 条/秒 ──► 机器人头部注视（设备侧），大脑不读
reb_visualdata vision.perception 人脸/手势 ~27 条/秒 ──► 大脑只在内容变化时转发，重复的每 5 秒一次
```

用两批真实消息重放：紧凑画面 20 条里只更新 6 次和 5 次（视觉描述 20 次和 16 次），“3 人↔2 人”的剩余跳动来自上面的“一人两条”。

人脸/手势去重前，每条都会写一行运行追踪并打印一行 INFO 日志（约 27 行/秒）；它们在事件策略里只是 `context_only`，不进模型上下文，去重不改变行为。

## 视觉端修复与复测

视觉端回复已处理 4 项（`observations` 仍为 version 1，新增 `action_detail`，`action` 改为固定词表：看电脑、看手机、打字、看书、写字、喝水、吃东西、挥手、说话、戴着耳机、看着我、走动、专注做事、站着、坐着、打电话；`identity_confidence` 为余弦相似度，0.45 计一票、12 帧内 6 票确认，本人通常 0.52～0.70）。复测 29 条实时消息：

| 项目 | 修复前 | 修复后 |
| --- | --- | --- |
| 同帧重名 | 出现 | 0 |
| 只有脸框的单独记录（一人两条） | 出现 | 0 |
| `action` 超出词表 | — | 0 |
| 同一人 `action` 变化（20 条） | 12、14 次 | 1 次 |
| `action_detail`（模型原话）变化（20 条） | — | 13 次 |

因此大脑只用稳定的 `action`，不用 `action_detail`。`objects[].near_label` 实测为称呼字符串，同时有 `near_subject_id`。

复测中又发现并在大脑侧处理：

- 环境措辞每帧不同（“办公室场景 / 办公室场景，有电脑和耳机 / 办公室环境”）→ 连续 2 条一致才换。
- 表情系统性误判：一个人一直在看电脑，被连续判为“厌恶”7 秒、随后“愤怒”3 秒 → 表情需连续 3 条一致，且**只把微笑、开心、高兴、大笑、惊讶告诉模型**，中性和负面表情一律不写，避免机器人因误判说“你怎么一脸嫌弃”。负面表情的准确度请视觉端校准后再议。
- 物品“谁身边”改为按 `near_subject_id` 对应大脑平滑后的称呼，避免出现“你正看着一位朋友……椅子（张某身边）”的前后不一。

最终用实时数据重放：20 条里视觉描述变化 14 次，给模型的画面只更新 2 次（首条与名字确认）；另一批 9 条更新 3 次（含一次真实的有人出现）。

视觉端说明：画面边缘的人时有时无是 YOLO 检测本身的问题，视觉端未处理，由大脑“3 条里见到 2 条算在、连续 5 条没见到算走”的规则覆盖。

## 熟人主动打招呼（用户决定：要；挥手不触发回应）

```text
observations ─► PeopleSmoother：开头 3 条之后才出现、且出现后 10 条内名字连续 2 条一致的人
   ─► vision.known_arrival {id, name}（每个人只报一次）
   ─► EventPolicy：need_response；同一名字 30 分钟内只打一次（config/runtime_defaults.toml
      [vision].greet_known_cooldown_minutes）；提示“按当前性格用一句很短的话打招呼，可以叫名字，
      不提摄像头/识别/画面，不执行动作”
   ─► EventService：机器人空闲、且最近 20 秒没有人在对话才说（否则等，10 秒有效期过了就不打）
   ─► Realtime 外部事件（不是用户发言）；唤醒模式下这一轮视同已准入的回复：播完进入追问窗口，
      对方回话不必喊“小安妮”（60 秒保护时限，回复失败也会关回休眠）
```

- 不打招呼：启动时就在场的人；没认出名字的人；在场途中才认出名字的人；30 分钟内打过的人；正在和别人对话时。
- 挥手（`vision.gesture`）保持 `context_only`，不触发回应。
- 测试 `tests/test_greeting.py`：策略（冷却、换人、无名、挥手）、平滑器（开机已在场、陌生人、只报一次、途中才认出）、端到端（事件路径提交外部事件含名字、唤醒进入 ACTIVE 并授权该轮、播完进入 FOLLOWUP、对话进行中等待）。
- 未实测：真实打招呼的时机与说法、对方回话的追问窗口，需要统一测试。

## 大脑原状

- `RoomMonitor.on_visual` 只认 `type=visualllm` + `description`，`facts` 可选；**顶层未知字段（含 observations）直接忽略**，现有对接不受影响（已在代码与测试中确认，回答视觉端的问题）。
- 画面文本 = 描述 +（可选）facts JSON，最多 2000 字，写进 session 指令；描述一变就整段重写指令，最快每秒一次。视觉端每秒一条、措辞随帧抖动，意味着指令几乎每秒被重写。
- `IdleService` 用 v2 `facts.subjects` 判断“现场是否有人”；“对谁说话”的决策模型取画面文本前 300 字。

## 改动

`apps/runtime/src/annie_runtime/room_monitor.py`：

- 有 `observations` 且 version 为 1（缺省视为 1；也认 `schema_version`）时，生成紧凑画面：

  ```text
  【眼前画面，2人】你正看着张三（在我面前）：专注看电脑，手里拿着黑色的手机。一位朋友（在右边，离我比较近）：看书。周围：办公室环境，有电脑和耳机。刚才：一位朋友出现。
  ```

  注视目标排第一，空字段不写，不写 bbox，最多 6 人（其余计数）；空房间写“画面里没有人”。不再把 facts JSON 塞进指令。设计细节见上节（平滑、更新节奏）。出现/离开记 30 秒、最多 3 条。
- version 不是 1 或没有 observations → 沿用原描述 + facts 逻辑；`facts` 仍交给 `IdleService`，主动陪伴逻辑不变。
- 诊断快照 `visual` 新增 `source`（`observations_v1`/`description`）、`context_text`、`observation_updates`。
- “对谁说话”的决策模型自动拿到以“你正看着……”开头的画面。

## 验证

`tests/test_visual_observations.py`（8 项，样例按真实 camera-a 消息结构，姓名替换）：注视目标在前且空字段不出现；名字需两条一致、一帧认错不改名；边缘闪烁既不算新人也不算离开，真正离开 5 秒后出现，一来一回抵消；措辞抖动 5 秒内不改写、注视切换立即改写；中性与负面表情不出现；version 2 回退；空房间；Realtime 与决策模型拿到紧凑画面；人脸/手势重复事件去重。原有视觉/Room 观察/陪伴测试全部通过；两批真实消息重放结果见上。

## 仍待确认

- 已确认：版本号键名为 `observations.version`（为兼容也接受 `schema_version`）；实测 `changes[].type` 只见到 `appeared`，大脑不依赖它（出现/离开由平滑结果产生）。
- `holding` 实测多为 null；负面表情的可靠性待视觉端校准。
- 2026-10-14 记录里提议的视觉 `interaction` 字段已被 observations 取代，不再单独实施。

# Phase 6｜长期记忆开工与验收记录

**状态：开工；仅完成命名空间硬边界和 macOS 依赖兼容性首轮试验。长期记忆尚未接入 Runtime/Realtime/React，不得标记可用。**

## 目标与边界

- 来源明确的用户偏好/经历跨 Chat 可召回；更正、遗忘先在读取路径生效，迟到旧任务不能复活事实。
- 普通聊天仍由 Qwen-Audio Realtime 完成；后台 LangMem 不阻塞主链，不追加外置聊天 LLM。最多 4 条相关证据进入上下文，零条属于正常结果。
- 保留 SQLite 全量 Ledger、20 轮本地上下文、Realtime 历史窗口、TTS/嘴型、独占 Room 和工作台现有功能；不创建 Phase 6 独立 HTML 页。

## 已确认与待决业务规则

- 总计划第 9.1 节明确当前身份只用服务器绑定的 `robot_id`；跨 Chat 同一机器人共用 semantic，Persona/episodes 按 Persona 分隔，跨机器人默认不共享。第 25 节 Phase 6 任务卡仍写「可信用户」scope，与前述已确认约束冲突；不据此创设 `user_id` 或自动共享。
- **待用户确认**：遗忘仅指长期记忆，还是还要删除对应 Ledger 原话、派生数据、导出和备份；若是后者需明确可追溯记录与备份保留策略。没有确认前不开放声称彻底遗忘的接口。
- **待用户确认**：embedding 使用哪个模型/部署（本地 vs 云端），以及后台 LangMem 抽取所用模型/费用与网络策略；没有确认前不改模型、不引真实记忆服务。
- Phase 5 真人语义、长空闲及实体播放仍待现场验收；本阶段的代码自动化不能代替它们。

## 检索预算（开工前固定的试验基线，尚未上线）

- 每次最多取 8 个候选，相关且未失效的证据最多 4 条，不相关时为 0 条。
- 记忆注入目标预算 256 Token；当前 Realtime 不报告可核实的逐段 Token 数，先用最多 256 UTF-8 字节作为保守代理，**不宣称已测得 Provider 的精确 Token 数**。
- 前台整个检索总 deadline 100ms（包含 embedding、SQLite 等待和筛选）；超时的普通聊天不等检索、按无记忆继续，明确问旧记忆时不得编造。此为待压测的初始门槛，不是已证明能达到的 P95。

## 验证记录（macOS arm64、Python 3.11.16）

- 开工前：`uv run --locked pytest -q`：149 passed、30 subtests、2 条第三方弃用警告；`uv lock --check` 通过。
- 使用 `uv run --no-project --python .venv/bin/python --with langmem --with 'langgraph-checkpoint-sqlite==3.1.1' --with 'sqlite-vec==0.1.9'` **临时隔离环境**，确认导入 LangMem 0.0.30、AsyncSqliteStore 3.1.1、sqlite-vec 0.1.9。未修改生产依赖或锁文件。
- 官方 Store 的内存数据库、2 维 FakeEmbedding：put/get、robot A/B 精确隔离、向量搜索、更新和删除后向量检索为空，首轮试验通过；不等于生产环境持久化、慢 embedding、锁争用、乱序更新/遗忘一致性验收。Windows 仍待验证。
- `memory_scope.py` 仅固定完整 namespace 构造和拒绝空标识，不连接数据库和模型，不构成长期记忆交付。

## 后续交付门槛

1. 明确遗忘与模型选择后锁定依赖及候选数、0–4 条、记忆 token 预算、总检索 deadline；先测慢 embedding 和 SQLite 写锁竞争，再用真实数据定稿。
2. 连接有来源的 Ledger → 后台有界提取/合并 → 持久化与索引 → 按需检索；验证重复任务、更正抢占、删除生效及索引清理。
3. 在现有 React 工作台实现查询来源、更正和遗忘状态，执行 P6-W1/W2 与 WEB-01～08；浏览器、真人模型质量、Room/物理体验分开记录，不替用户代验。

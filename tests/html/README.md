# 阶段可视化验收页

这里仅放Phase 1–5（含4B/4C）的临时HTML验收页，不属于正式Dashboard。Phase 4起正式能力统一进入`web/`内React工作台；本目录只保留用例说明、验收入口和必要独立诊断，不再维护第二套多会话/PCM界面。既有页面在迁移验收前保留；最终删除本目录不得影响正式工作台。

- Phase 1–5（含4B/4C）沿用对应页面安排，文件名：`phaseN-*.html`；Phase 6及以后不再新增独立HTML测试/验收页，直接在现有React工作台集成功能与诊断并执行网页验收，证据写阶段README，自动化和真实/人工验收要求不变。
- 只调用当前阶段已经存在的公开HTTP接口或已确认的Room WebSocket协议，不发明接口、不复制后端业务逻辑。
- 页面应能直观看到当前阶段的核心能力、失败状态和隔离边界；布局与验收遵守[项目规则第7条“阶段HTML最低可读性标准”](../../PROJECT_RULES.md)：先显示操作步骤、当前对象、中文结论和关键指标，再折叠展示JSON/日志。该标准不代表现有页面已完成改造。
- Runtime存在本目录时会把它挂到`/phase-tests/`。
- 当前保留三个阶段验收页：`phase1.html`验证单进程多机器人头并发回合；`phase2.html`验证真实Room PCM回环与多Room隔离；`phase3.html`验证Chat/Persona切换、Ledger、20轮上下文和会话隔离。
- `phase2.html`直接连接真实`reb_mic`发送BinaryFrame，并通过Runtime诊断接口确认收回结果；HTTP debug ingress只保留自动化/协议边界回归，不再作为真实联调证据。
- 既有页面不因Phase 6切换验收方式而提前删除；按Phase 10最终整合安排、用户确认后删除整个`tests/html/`目录及临时挂载，后续不重建。正式产品能力始终留在`web/`。

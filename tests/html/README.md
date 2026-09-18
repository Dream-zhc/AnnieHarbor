# 阶段可视化验收页

这里仅放开发阶段的临时HTML验收页，不属于正式Dashboard。

- 每个实现阶段必须至少有一个对应页面，文件名：`phaseN-*.html`。
- 只调用当前阶段已经存在的HTTP接口，不复制后端业务逻辑。
- 页面应能直观看到当前阶段的核心能力、失败状态和隔离边界。
- Runtime存在本目录时会把它挂到`/phase-tests/`。
- 当前只保留两个人工验收页：`phase1.html`验证单进程多机器人头并发回合；`phase2.html`验证浏览器真实麦克风/测试PCM → Java Room → Annie Harbor的完整输入回环，以及roomv2/roomv3等多Room隔离。两页都在页面内打印操作日志，同时浏览器Console保留同样日志。
- `phase2.html`直接连接真实`reb_mic`发送BinaryFrame，并通过Runtime诊断接口确认收回结果；HTTP debug ingress只保留自动化/协议边界回归，不再作为真实联调证据。
- 所有阶段完成并进入最终整合后，直接删除整个`tests/html/`目录；正式产品能力必须进入`web/`而不是留在这里。

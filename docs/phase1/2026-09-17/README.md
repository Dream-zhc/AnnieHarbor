# Annie Harbor Phase 1 验收记录

日期：2026-09-17。

## 范围

本阶段只实现最小V2 Skeleton：uv workspace、统一启动、基础SQLite、health、最小Dashboard，以及同一Python进程内两个Fake RobotRuntime。旧版继续冻结；未搬RobotBridge，未接Room/LLM/TTS/长期记忆/嘴型真实运行链，未引入Redis、Pipecat或复杂事件总线。

## 架构结果

- workspace：`apps/runtime` + 5个`packages/annie-*`，Python 3.11。
- Runtime：每个`robot_id`独立`asyncio.Queue`、worker、当前turn task和取消状态，无全局回合锁。
- Fake Brain：确定性异步回显，可显式注入异常。
- SQLite：schema v1，WAL；只含`runtime_snapshots`和`runtime_events`。
- Web：FastAPI提供`/health`和本地Fake控制API；原生Dashboard服务于同一进程。
- 启动：macOS/Linux `start.sh`、Windows `start.bat`均进入`uv run --locked python -m annie_runtime`。
- 配置/环境：`config/`只放非敏感TOML；根`.env`由Python bootstrap自动加载且不覆盖已导出的系统环境；运行数据/工具缓存统一进入`var/`。
- 启动预检：先验证数据库路径和现有SQLite文件可读/schema兼容，再处理端口替换；新数据库无效时不会先停掉旧实例。检测到同一个Annie Harbor占用默认端口时，才按`/health.pid`终止旧实例并等待listener释放；非本项目占用则明确报错并退出2，禁止按端口盲杀。
- 配置校验：RobotConfig严格校验bool/int类型与范围；数据库只允许落在项目`var/`目录。
- 延迟口径：Turn结果区分`queue_wait_ms`、`service_ms`，`elapsed_ms`表示从入队到完成的端到端耗时。

## 隔离验收

自动化测试明确覆盖：

1. A与B同时处于running，证明同进程并发而非串行全局锁。
2. 停止A时，A当前回合取消/排队回合丢弃，B当前回合与后续回合继续完成。
3. 取消A当前turn时，A worker保持可用，B不受影响。
4. A触发真实FakeBrain异常后仅A进入failed，B继续完成；A可独立restart。
5. A队列满/积压与客户端任务取消都不会破坏B或全局Runtime。

实际Uvicorn HTTP联调额外验证：A故障后B返回`completed`；A重启再停止后B仍返回`completed`。一次live curl取消因工具调用间隔超过配置的800ms Fake延迟，A已经完成，所以该次调用不作为取消成功证据；取消隔离由上述并发自动化测试证明。

## 最终命令结果

```text
uv run --locked pytest -q
33 passed, 2 warnings, 7 subtests passed

node --test web/tests/dashboard.test.mjs
6 passed, 0 failed

uv lock --check
PASS

.venv/bin/ruff check apps packages tests/test_phase1.py
All checks passed!

./start.sh --check
ready=true, status=healthy, annie-a=idle, annie-b=idle

真实替换测试：先启动Annie Harbor PID 55112占用8780，再执行sh start.sh
=> 旧PID 55112收到SIGTERM并完整shutdown
=> 新PID 55146绑定同一8780
=> GET /health => 200, ready=true, annie-a/annie-b均idle
```

阶段可视化验收页位于`tests/html/phase1.html`；Runtime启动后访问`/phase-tests/phase1.html`，页面会列出当前配置中的全部机器人头，并可一键并发向全部头发送Fake回合，验证“单进程、多robot_id、并发执行、状态隔离”。单头turn/cancel/stop/start/fail仍可分别测试。

测试允许读取本机真实`.env`做泄漏边界检查；但Fake Runtime基础回归不要求真实Provider密钥存在，因此新机器/CI没有本机`.env`时仍可执行基础测试。

两条warning来自FastAPI/Starlette测试栈对当前`httpx`/AnyIO别名的弃用提示，不影响Phase 1运行或隔离验收；本阶段不为消除第三方测试warning额外扩依赖。

## 已知边界

- Phase 1验收时只有本地Fake输入/输出，不定义或猜测真实Room wire字段。
- Phase 0确认的同房间真实设备输出寻址仍未解决，留到External Gateway阶段基于证据处理。
- sidecar仍未多头化；长期记忆尚未进入本阶段。
- Phase 1开发时当前目录尚不是Git仓库；进入Phase 2前核对忽略规则后建立独立基线`2cebdf6`。旧版Git始终未修改。

历史红绿日志、迁移快照和重复变更清单已在后续文档精简中删除；本README保留阶段结论，真实能力以当前代码和可重跑测试为准。

## 下一阶段

Phase 2已进入实施，当前结果见`docs/phase2/2026-09-18/README.md`；仍继续以Phase 0 fixtures作为协议依据，不把RobotBridge整体搬入V2。

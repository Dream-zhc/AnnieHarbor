# Phase 4C · 统一 React 工作台实施与验收

> **2026-09-20 返工决定：原版“测试功能汇总页”不作为正式产品交付。以下历史清单记录上轮状态；以文末“产品级工作台返工”作为当前实现与验收范围。**

## 阶段状态

- 用户已明确安排在 Phase 4B 尚未人工测试时先实施 4C，待统一工作台完成后集中测试。**4B 保留待验，不宣布 PASS**；Phase 4 的物理麦克风与真正音频联合验收同样未补证据。
- 本阶段只整合原有 web/、Phase 1–4B 已接线功能，不换模型、音色、Room 合同、4B 合成/嘴型/取消链，不新增后续阶段业务。
- 4C 自动化、构建、实际浏览器、真 Room、真扬声器、人工体验分别记录；真实回执不存在时继续 unknown。

## 功能迁移清单（入口 → 真实接口 → 状态）

| 来源 | 统一工作台入口 | 后端/Room 接线 | 本轮结果 |
|---|---|---|---|
| 1 | 顶部健康、机器人栏、开发者 Fake 控制/独立 Fake 回合及取消 | `/health`、`/api/robots/{id}/{start,stop,fail,cancel}`、`/turns` | 代码接线，人工待验 |
| 2 | PCM / Room、测试帧、浏览器麦克风和明确停止按钮 | `/api/phase2/rooms`、`/inputs`、原 MIC_AUDIO Room WS | 复用，人工待验 |
| 3 | 会话栏、新建/切换/删除、完整历史、20轮上下文、开发者 Fake 播放、Coordinator/SocialState | `/session`、`/conversations`、`/context`、`/turns/{turn_id}/playback`、`/response-requests`、`/coordinator`、`/social-state` | 历史列表补 `persona_revision`；页面动作/历史复用，人工待验 |
| 4 | 对话文字流、转写/函数事件、Session/generation、连接/错误 | `/realtime/events?after=N`、`/realtime/text`、`/health` | 复用并增加隔离单测，人工待验 |
| 4B | TTS/PCM/嘴型/回执、浏览器测试扬声器、同一取消链 | 已有 reb_tts、reb_sam、reb_main、`speech_output` | **仅整合展示；声学/真机未验** |

本轮改动只对现有会话列表 API 增加 `persona_revision`：取本 Chat/Persona **最后创建的已落盘回合**的 revision（即使后来手动从 4 改回 1 也显示 1），不改数据库 schema/历史数据；全新无历史 Chat 仍由当前 Session 表单创建。无法从列表推出无历史 Session 的版本时，不伪造数据库事实。

## 工作台组织与边界

- 顶栏唯一显示 robot / Room / 当前 Chat、健康及全局取消；侧栏只负责机器人与会话；中央保留完整历史与流式文字；音频/PCM 独立入口，开发者详情默认收起、JSON 默认折叠。
- 六通道显示的是地址配置状态，**仅 mic 展示其实际连接状态**；浏览器试听报告标 `browser_test`，不升格为真机扬声器已播；4B 默认不开启真实发布或下载模型。
- GSAP 3.15.0 + @gsap/react 2.1.2 精确锁版，首次静态卡片轻量入场，media query 减少动效；无流式文本逐 token 动画，不阻塞取消、PCM 或 Room 消息，卸载清理 GSAP scope。
- 原 React/TypeScript/Vite、npm 单锁、Python 静态发布保留；Fake 播放修改限定刚在本页创建的 Fake Turn，不改真实回复的播放事实。

## 网页验收

正式入口 `/`；独立步骤入口 `/phase-tests/phase4c.html`。按总计划 WEB-01～08 与 P4C-W1～W7 执行。桌面 1440×900、窄屏 390×844、减少动效开关、机器人/会话连续切换、Room 输入/浏览器播放器、失败提示、关闭页面释放、前后端回归及浏览器性能均须单独留下证据。

**结论：浏览器人工操作、真实设备/麦克风、AEC、实际嘴型与性能前后对照均待验；不能用构建通过替代。**

## 本轮自动化证据（2026-09-20）

- `uv run --locked pytest -q`：**101 passed / 2 条既有第三方弃用 warning / 25 subtests**。4C 新增 revision 回退与 A/B 独立历史、Python TestClient 正式页面/静态资源/验收页 HTTP 检查。
- `npm --prefix web ci`、`npm --prefix web run typecheck`、`npm --prefix web test`（**7 passed**）、`npm --prefix web run build` 均退出 0；`uv lock --check`、`uv run --locked ruff check apps packages tests`、`git diff --check` 均通过。
- 正式构建 JS：前次已提交产物 243,214 B；本轮构建约 317,700 B，新增 GSAP 等约 74.5 kB 未压缩。**未**取得浏览器动效开/关的长任务/掉帧和取消延迟对照；不评价实际性能。
- HTTP 检查使用内存 TestClient + 独立临时 SQLite，不是通过 CLI 新开独立服务，也不是实际浏览器。未调用 TTS 或声学模型、未向 Room 发布测试音频；设备联合实测留待用户集中进行。

## 产品级工作台返工（用户确认，优先于上轮迁移清单）

- **正式 React 首页不是阶段测试聚合器。** 导航固定为当前有真实数据的总览、实时对话、机器人控制中心和只读开发者观测；后续记忆、视觉、事件、动作、复杂任务按实际接线逐步增量进入各业务区，不预建空白模块或伪造状态。
- 对话页改为「机器人/会话侧栏 → 历史、实时文字与固定输入 → 当前机器人状态」三栏；Session/Persona/revision 使用新建会话弹窗、历史会话直接切换，技术 epoch/generation 留在 Developer。删除范围显式确认。用户查看旧消息时不强制滚动；转写/生成事件仍以 robot/chat/persona/epoch/generation 过滤。
- 总览和机器人页面使用后端的健康/Room/Realtime/TTS/嘴型及播放证据数据；六通道保留，非 mic 只展示已配置而非已连接；发布和真机已播保持分层，未知仍为 unknown。
- **前序调试功能保留单独简单 HTML，不作为正式 React 操作**：Phase 1 Fake/多头在 `/phase-tests/phase1.html`；Phase 2 单帧 PCM、浏览器测试麦克风在 `/phase-tests/phase2.html`；Phase 3 Fake Ledger/模拟播放/Coordinator 在 `/phase-tests/phase3.html`；Phase 4 Realtime 联调在 `/phase-tests/phase4.html`；Phase 4B reb_tts/reb_sam 浏览器试听与 `browser_test` 回执在 `/phase-tests/phase4b.html`。**保留了这些页的可测试能力与公开后端合同，不删除阶段回归。**
- 4B 测试 HTML 直接订阅原 Room WebSocket，消费一份真实 reb_tts PCM、显示 ForcedAligner 实际嘴型、发 browser_test 回执；无设备数据时不产生 speaker、spoken 事实。移除 React 中无人使用的 PCM/播放测试组件，不改变 Runtime/声学 sidecar/模型配置/Room wire。
- GSAP 只做展示入场，媒体减少动效时不创建该动画；输入/取消无需等待动画、无假波形或伪反馈；不增加新状态框架/第二前端/产品测试按钮。

### 返工验证记录

工作台正式入口仍为 `/`，新版阶段验收入口仍为 `/phase-tests/phase4c.html`。需重新执行 WEB-01～08 与 P4C-W1～W7 的实际布局、弹窗、历史滚动、隔离、错误、取消、减少动效、Room/设备/性能分层验收。**Phase 4/4B/4C 人工体验与真实设备不因为本轮重排而自动通过。**

- `npm --prefix web ci`、`npm --prefix web run typecheck`、`npm --prefix web test`（8 passed）、`npm --prefix web run build`：通过。Python 完整回归 101 passed / 2 条既有弃用 warning / 25 subtests，uv lock、ruff、diff check 通过。
- 实际以独立 SQLite、关闭外置 TTS/ForcedAligner 且不使用 Provider Key 启动 Python Runtime；`/`、`/health` 和全部 `/phase-tests/phase1.html`～`phase4c.html` 真实 HTTP 均为 200。自建测试进程已结束，测试数据库已清理；未发起真实音频发布。
- 测试 HTML 与正式工作台隔离由静态回归覆盖；Phase 4B HTML 的 JS 已语法解析并核对 Room URL、browser_test 与采样回执字段。**浏览器实际播放、桌面/窄屏截图、减少动效与性能前后对照未取得：本机 Playwright 未安装浏览器可执行文件，不能把 HTTP/构建验证记成 GUI PASS。**

## 工作台第二轮精简（2026-09-20）

- 总览仅保留 Runtime、机器人数量和可选机器人列表，去掉「当前 Realtime」「设备播放证据」和逐项能力统计；总览不依赖当前选中哪个机器人。
- 对话、机器人、开发者共用顶部唯一机器人选择框；切换后复用原 robot_id 隔离、重置旧会话状态与迟到结果保护，不再各页重复选择器。
- 机器人页面保留当期可用的 Realtime、TTS、嘴型、设备播放状态及最小 Room 信息；六通道细项与 PCM/事件 JSON 只放只读开发者诊断，Fake/注入/浏览器试听只在前序独立 HTML。
- TTS 代码已连接但默认关闭，需 `ANNIE_TTS_ENABLED=1` + Realtime Key/Room；嘴型另需 `ANNIE_VISEME_ENABLED=1` 和 Sidecar 环境，模型输出真实 `mouth_script` 前不宣告已对齐。未取得真机播放证据时保持 unknown。
- 本轮 `npm --prefix web run typecheck`、`npm --prefix web test`（8 passed）、`npm --prefix web run build`、`git diff --check` 通过。未进行人工浏览器布局、设备或声学验收。

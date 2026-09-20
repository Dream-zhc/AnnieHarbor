# Phase 4C · 统一 React 工作台实施与验收

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

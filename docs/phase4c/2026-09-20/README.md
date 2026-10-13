# Phase 4C｜React工作台整合摘要

> 2026-10-10精简。历史“测试汇总页”、独立阶段HTML及缺失环境说明不再作为当前操作指南。**软件与浏览器模拟证据不代替真实设备、声学或人工体验验收。**

## 软件职责与保留项

- 在既有 `web/`整合Phase 1–4B功能，不重建React/TypeScript/Vite工程、不换模型/音色/Room合同、不改变TTS/嘴型/取消链。当前工作台与大屏入口见[前端说明](../../../web/README.md)。
- 顶部robot/Room/Chat与取消清楚；会话侧栏、完整历史/流式正文/输入、当前机器人状态和按需开发者诊断分区。技术epoch/generation后置，JSON默认折叠，不预建空模块或假状态。
- 新建/切换/删除Chat及Persona/revision沿用Session接口，删除明确确认；查看旧消息不强制滚动。旧请求/事件按robot/chat/persona/epoch/generation过滤，不串目标。
- 会话列表的persona_revision取该Chat/Persona最后创建的已落盘回合，不取最大值；回退4→1仍显示1，无历史不伪造落盘事实。
- 既有接线包括健康/实例控制、PCM/Room、会话/历史/context、Ledger/Coordinator/SocialState、Realtime文本/SSE与TTS/嘴型/播放证据。Fake与真实操作分开，模拟播放不能修改真实回复事实。
- 六通道配置存在不等于全部已连接。speaker与browser_test分源，无真实播放证据仍unknown；网页不能持有Provider密钥或自行切换Provider开关。
- GSAP3.15.0 + @gsap/react2.1.2为该阶段精确依赖基线，当前以锁文件为准。入场动效遵守减少动效和卸载清理，不逐token动画、不延迟PCM/输入/取消，不制造假波形。

## 麦克风0帧缺陷与有效复现

根因是启动完成后把局部ws/context设为undefined，音频回调仍引用这些变量，造成UI“发送中”而不产帧。修复捕获不可变capture资源并显式移交所有权，启动失败与正常停止分别清理；getUserMedia和AudioContext.resume在用户点击时发起。

PCM跨回调按16kHz/mono/S16LE、20ms打包；16/44.1/48kHz连续样本有可执行回归，不再仅匹配源码字符串。首帧即时刷新计数，后续降频更新；3秒无首帧显示AudioContext/track状态并停止，不把WS连接成功当音频回调成功。切Robot/离页/停止释放MediaStream、AudioContext和WS。同Room一路音源，真实发送必须由用户显式开启。

**当时真实Chromium＋假麦克风＋Mock Room WS**捕获12个651B MIC_AUDIO包，UI非零帧，停止/切B关闭旧连接，无JS异常。该证据验证真实浏览器回调，不向现场Room发帧，不证明物理麦克风、ASR或扬声器通过。长期浏览器回归为 `web/tests/microphone.browser.mjs`。

## 历史验证与证据级别

- 初始/返工Python101项、25子测试、2项既有弃用警告，前端最终8项及类型/构建/Ruff/锁/diff通过；这些是历史版本结果，不是当前全量计数。
- TestClient与独立SQLite验证过页面/静态资源；独立关闭TTS/ForcedAligner的Runtime曾HTTP200且退出清理。**当时**无法取得浏览器可执行文件的那轮只记HTTP通过，不追认GUI通过；后来麦克风Chromium证据独立记录如上。
- GSAP接入时未压缩构建约增加74.5kB；没有动效开关的长任务/掉帧/取消延迟对照，不据构建体积判真实性能。
- 旧 `/phase-tests/`和 `tests/html/`已清理；正式麦克风与网页调试扬声器以现有工作台实现为准，不恢复阶段验收页面。

## 统一验收待办

按[总览WEB-01～08](../../规划/小安妮V2后端开发总计划.md)验证1440×900/390×844、键盘、弹窗、历史滚动、错误/断线、减少动效、快速切Robot/Chat/Persona、迟到结果和资源释放；音频/取消并发与性能留实际数据。真实Room/物理麦克风/扬声器/AEC/嘴型与真人体验继续待验，不能因重排布局或自动化通过而标Phase 4/4B/4C整体PASS。

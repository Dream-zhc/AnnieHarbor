# Annie Harbor · 正式React工作台

当前工程使用React + TypeScript + Vite，源码在`web/src/`，构建产物在`web/dist/`，统一使用`web/package-lock.json`。现有实现和待验项见[Phase 4实施记录](../docs/phase4/2026-09-20/README.md)，不以代码存在替代网页/设备验收。

## Phase 4C整合目标

**Phase 4B已在开发，新增Phase 4C完善网页架构与基础、接齐Phase 1–4B已有功能；后续阶段只在同一工作台追加。** 不前移第一阶段、不重建已有React工程。功能盘点、基础职责、布局和验收见[总计划第22.1/22.5/25节](../docs/规划/小安妮V2后端开发总计划.md)。

- 顶部明确当前机器人/Room/会话，侧栏管机器人与会话，对话区承载历史和输入；PCM详情与开发者诊断分区，停止入口始终可达。
- 保留健康/实例操作、PCM、Chat/Persona、完整历史、20轮上下文、Ledger/Coordinator/SocialState既有能力；Fake/真实操作明确区分，迁入逐项举证。
- 4C整合已有TTS/嘴型展示；后续记忆、动作、事件和工作流复用现有导航、样式、API与隔离规则，不另建前端或在临时HTML重复实现。

## Phase 4C动效要求（待实施）

采用GSAP + @gsap/react，版本基线/具体效果/性能与减少动效验收统一见[总计划第22.6节](../docs/规划/小安妮V2后端开发总计划.md)。主要用于面板转场、列表变化和操作反馈，简单样式过渡用CSS；音量/波形只展示真实数据，动画不延迟请求或取消。本次仅规划，尚未安装动画依赖。

## 当前构建与检查命令

从项目根目录执行（Node版本要求以`web/package.json`为准）：

```sh
npm --prefix web ci
npm --prefix web run typecheck
npm --prefix web test
npm --prefix web run build
```

Python正式静态入口提供`web/dist/`，不依赖生产常驻Node开发服务；统一启动后访问工作台。后端请求使用同源接口，PCM测试沿用已确认Room WebSocket协议。浏览器不持有Provider密钥，机器人正常运行不依赖网页常开。

## 验收与边界

总计划WEB-01～08和Phase 4C网页用例覆盖实际接线、桌面/窄屏布局、错误状态、目标隔离与资源释放。类型检查/构建/自动化通过不代替浏览器实际操作或人工确认；本README不宣称本次已运行上述命令。

同Room仅一路PCM；开启浏览器测试麦克风前确认没有机器人音源同时发送，切机器人或离页释放本页采集。完整历史不受20轮窗口截断，旧generation结果不得串入新会话；生成、发布、设备已播与unknown分开记录。Phase 1–5（含4B/4C）临时HTML仅作验收入口；Phase 6及以后不再新增独立HTML测试/验收页，新增功能、操作、状态和诊断直接集成到本工作台，按WEB-01～08及阶段用例进行浏览器验收，证据写阶段README。自动化和真实/人工验收不省略，既有临时页待最终整合确认后清理。

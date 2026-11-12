# Annie Harbor｜唯一前端工程

React + TypeScript + Vite工作台，一个 `web/`构建，由Python静态服务器提供并读同一套后端业务数据；不另建前端项目。

## 正式入口

- `/`：完整交互工作台与开发者诊断。

内网启动后直接访问 `http://<主机内网IP>:8780/`，无需登录凭据或逐次访客授权。数据大屏已交给前端团队另做，本工程不再包含大屏页面；大屏从 Java Room 的 `reb_display` 频道取数据，见[数据大屏数据同步说明](../docs/规范/数据大屏数据同步说明.md)和[数据大屏监听接口](../docs/规范/数据大屏监听接口.md)。

`src/`是源码，`dist/`是正式静态产物，`package-lock.json`是唯一npm锁文件。声学sidecar自带测试HTML不是正式前端入口。

## 构建与回归

从项目根执行，Node版本以 `package.json`为准：

```sh
npm --prefix web ci
npm --prefix web run typecheck
npm --prefix web test
npm --prefix web run build
```

Python提供 `web/dist/`，生产不依赖常驻Node开发服务；机器人不依赖网页常开。更新构建须保留工作区已有改动，不以构建旧代码覆盖并行成果。

## 交互与验收边界

- robot/Room/Chat和停止入口清楚，日常操作先于诊断JSON；完整历史、Persona、PCM、记忆、动作和事件沿用已有API，不在浏览器复制后端状态机。
- 同Room只允许一路PCM。浏览器测试麦克风显式开启，切对象/离页释放采集与连接，不与机器人音源同时发送。
- Provider密钥不进入浏览器；generated/published/spoken/unknown分开，网页试听回执只报 `browser_test`，不冒充实体speaker。
- GSAP + @gsap/react已列入现有依赖与锁文件；减少动效、组件卸载、真实并发与真实波形必须验证，动画不延迟PCM、文本或取消。
- [收束总览](../docs/规划/小安妮V2后端开发总计划.md)的WEB-01～08仍适用。类型/构建/自动化通过不替代浏览器或设备人工操作；历史证据见[Phase 4C](../docs/phase4c/2026-09-20/README.md)。

软件阶段规划已收束，仅按具体Bug修复。旧 `tests/html/`及 `/phase-tests/`已清理，不恢复独立验收页面。

# Annie Harbor · 安妮港 Dashboard

当前最小独立前端展示同一 Python 进程内 Fake RobotRuntime 的健康、回合和实例操作；Phase 2 PCM准入验收使用`tests/html/phase2.html`。没有前端依赖、CDN或真实Provider/Room连接。

## 运行与构建

Runtime 统一启动后访问 `http://127.0.0.1:8780`。Python Web 层直接提供本目录 `src/`，无需 Node 常驻开发服务。所有请求使用同源 URL。

本目录可独立复制；构建与测试需要 Node >=20，均无需安装依赖：

```sh
node --test tests/dashboard.test.mjs
node scripts/build.mjs
```

也可使用 `npm test` 与 `npm run build`。构建产物为本目录 `dist/`，构建按脚本自身位置定位，不依赖调用工作目录。

## 操作边界

- 卡片和请求明确指定 `robot_id`；文本、统计和取消不跨实例。
- 停止/故障后可以启动该机器人；另一机器人仍可提交 Fake 回合。
- `cancel` 只取消正在执行的回合，不是房间协议或真实小脑 cancel。
- 页面从同一 `/health` 快照读取机器人指标、PID 和能力状态；定时请求串行执行。
- Fake Brain 确定性回显；Room、LLM、TTS、长期记忆均未接入，嘴型 sidecar 未启动。
- 所有回复和错误按纯文本渲染，不解释用户输入中的 HTML。

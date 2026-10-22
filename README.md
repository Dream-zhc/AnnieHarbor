# Annie Harbor（安妮港）

小安妮机器人实时陪伴项目。一个 Python Runtime 管理多个独立机器人，React 工作台与数据大屏共用同一后端。

**当前状态：软件阶段规划已收束，等待统一实测；Phase 10/11及后续阶段暂停。** 软件接线、自动化通过、真实设备验收分开记录，后续只按具体 Bug 最小修复。

## 启动

在项目根目录执行：

```sh
sh start.sh
```

Windows 使用 `start.bat`。默认端口 `8780`：

- 数据大屏：`http://localhost:8780/?view=cockpit`（默认视图）
- 交互工作台：`http://localhost:8780/?view=workbench`
- 健康状态：`http://localhost:8780/health`

非敏感配置在 `config/`；机器差异与密钥在根 `.env`，模板为 `.env.example`。同项目旧实例由启动器验证后替换，其他程序占用端口时明确报错，不静默换端口。

## 目录一览

| 目录 | 职责 |
| --- | --- |
| `apps/runtime/` | 统一启动、机器人生命周期、音频准入与回合协调 |
| `packages/` | 核心类型、对话/记忆、外部适配、诊断和 Web API |
| `web/` | 唯一 React + TypeScript 前端，工作台与大屏共享构建 |
| `config/` | 可提交的配置资产，不存真实密钥 |
| `scripts/` | 中文唤醒模型下载、展厅访问初始化等运维工具 |
| `tests/`、`web/tests/` | 长期回归测试，不是临时探针 |
| `docs/` | 当前边界、规范、协议及实际验收证据 |
| `var/` | 本机数据库、日志、缓存和模型，不参与源码发布 |

## 先读什么

1. [文档导航](docs/README.md)：按问题找到当前真源。
2. [项目规则](PROJECT_RULES.md)：工程与业务硬约束。
3. [收束总览](docs/规划/小安妮V2后端开发总计划.md)：架构、阶段摘要和待验项。
4. [目录与配置规范](docs/规范/项目结构与配置规范.md)。

当前链路方向是 `Room PCM → Realtime 文本 → 独立 TTS → Room PCM`，同一 PCM/文本送 ForcedAligner。普通聊天不走 LangGraph；复杂任务才按需编排。A→roomv2、B→roomv3，每机器人独占一个 Room，原六通道全保留，每 Room 只有一路 PCM 输入。配置存在不等于能力已连接。

## 开发检查

```sh
uv run --locked pytest -q
npm --prefix web test
npm --prefix web run typecheck
uv lock --check
uv run --locked ruff check apps packages tests
```

发布前端修改时再执行 `npm --prefix web run build`，由 Python 提供 `web/dist/`。启动改动另做真实进程与 HTTP 验收；自动化通过不代替浏览器、真实麦克风或设备实播验收。临时 HTML 验收页已删除，不恢复。

## 后续任务提示词

```text
仅在当前 brainv2 根目录工作。先读 docs/README.md、PROJECT_RULES.md、收束总览和结构规范。
阶段规划已冻结；只复现并最小修复本次明确的 Bug，不自动推进后续阶段。
保留工作区已有代码/配置改动、长期回归、全部 Room 通道和单头嘴型能力。
说明目标、可见效果、输入输出、拟改文件、保留项和验收；业务或协议不明先询问。
记录真实命令与证据级别，不用模拟/自动化结果填写设备或人工验收通过；不自动提交。
```

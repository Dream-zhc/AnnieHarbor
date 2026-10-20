# 运行日志全量导出（2026-10-20）

用户要求把本机全部运行日志和对话记录上传，用于分析。本目录是 2026-10-20 17:46 的快照，原始文件仍在 `var/`（被 `.gitignore` 忽略）。

## 日志/

`var/` 下全部非空 `*.log`，共 152 个，保持原相对路径：

- `logs/`：运行期日志。`viseme-annie-a.log`（约 7MB，嘴型 sidecar，持续到 10-20）、`viseme-annie-b.log`、`phase4b-enabled.log`、`phase4-rework.log`。
- `tmp/`：各次 Bug 修复与验收留下的日志，如 `wake-enable-20261009/startup.log`（启动控制台输出）、pytest/web 测试输出。
- `审查/`、`整理回滚/`：审查与回滚整理时留下的日志。

## 对话与事件/

从 `var/runtime.db` 只读导出，每行一条 JSON，二进制列（向量 embedding）已去掉：

| 文件 | 内容 | 条数 |
| --- | --- | --- |
| `conversation_turns.jsonl` | 每轮对话：用户原文、计划/生成/发布/实际播出的回复、打断/取消、来源 | 465（2026-09-18 ～ 10-20） |
| `runtime_events.jsonl` | 运行事件：启动/停止、请求耗时、排队、错误 | 112404（2026-09-17 ～ 10-20） |
| `chat_memories.jsonl`、`long_term_memories.jsonl` | 会话记忆与长期记忆 | 111、25 |
| `action_requests.jsonl` | 动作/表情调用与执行结果 | 21 |
| `conversation_sessions.jsonl`、`conversation_chats.jsonl`、`conversation_social_state.jsonl` | 会话、聊天与社交状态 | 2、10、14 |
| 其余 | Persona 实例、快照、提醒、记忆进度、克隆音色元数据 | 少量 |

`var/backups/` 下的旧数据库备份和 `var/phase*-*/` 测试库没有导出。

## 控制台日志

此前 `start.sh` 不落盘，过去的控制台输出只剩上面 `tmp/` 里的几份。从本次起 `start.sh` 每次启动会把控制台输出同时写到 `var/logs/runtime-<时间>.log`，之后由`scripts/导出运行日志.py`按日期增量导出（同一天追加到同一目录），每次提交推送前执行。`start.bat` 未改。

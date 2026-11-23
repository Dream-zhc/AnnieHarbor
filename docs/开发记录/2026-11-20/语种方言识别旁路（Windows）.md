# 语种 / 方言识别旁路（Windows 外挂，2026-11-20）

用户：写一个 Windows 模块，拷过去执行 bat 自动下载启动，监听机器人麦克风，把语种信息发出来，工作台显示当前语种；要匹配机器人；两台机器人能不能听得过来。

## 选型

- 阿里的 ASR 都不给地域方言标签（qwen3-asr 只分 zh/yue 等大类加情绪；fun-asr 方言转写最广但不报方言）。
- 实测两个开源模型（合成方言样本，Mac M4 CPU）：Dolphin small 5 句对 3、base.cn 对 2，粤语都认错；FireRedLID（小红书 FireRedASR2S 的语种识别模块，Apache-2.0，100+ 语言、20+ 方言，官方方言集 88.5% 对 Dolphin 69%）5 句对 4（粤语、西南官话、普通话×2 对；合成“河南话”判成西南官话）。选 FireRedLID。

## Windows 模块 `sidecars/annie-dialect-lid/`

- `start_win.bat` → `service_win.ps1`：缺环境自动建本文件夹 `.venv`（Python 3.11，CUDA PyTorch，官方源失败换阿里云镜像）、下载 FireRedASR2S 代码（GitHub zip）和模型（Hugging Face，失败换 hf-mirror.com），然后启动 `lid_service.py`。单实例（本机端口 37661）。
- 每台机器人一条 `<room>/reb_mic` 连接（WELCOME 校验 room 与通道 → SUBSCRIBE → 二进制 PCM），能量切句（全零帧不拉低底噪），单线程依次用 FireRedLID 判断，结果 `language_id` 与 2 秒一次的 `language_id_health` 发到同一 Room 的 `reb_sam`，带 room 和 robot_id；一个 Room 只对应一台机器人（config.json：annie-a→roomv2、annie-b→roomv3，annie-small→roomv4 先关）。
- 并发：单线程排队，不抢显卡；Mac CPU 一句 0.4～0.6 秒，RTX 4060 预计 0.2～0.4 秒（官方 RTF≈0.07），两台机器人同时说话只多排队零点几秒；半精度显存约 1.8 GB。
- 协议与配置见 `sidecars/annie-dialect-lid/README.md`。

## 主脑与工作台

- `room_monitor.py`：`reb_sam` 的 `language_id_health`、`language_id` 按 Room 收（room 不符的丢弃），日志 `language_id room=… robot=… label=… conf=…`；`/api/robots/{id}/room-observations` 新增 `language`（state：online / waiting / offline（10 秒无心跳）/ disconnected / error / loading / no_mic，最新结果、设备、排队）。
- 工作台顶部新增“语种”胶囊：在线时显示“粤语 99%”，悬停看把握度、句长、出结果耗时、设备；`web/src/language-id.ts`，已重新打包 `web/dist`。
- 结果暂只显示和记日志，不驱动方言跟随；等真人方言录音测完准确率再定。

## 验证

- Mac 端到端：假 Room 按实时节奏推两路 PCM（40 ms 一包、不按帧对齐），服务分别判出 roomv2 粤语 0.999、roomv3 普通话 1.0 并发回各自 reb_sam；断线重连后继续判断。
- 隔离根真实 `sh start.sh` + 假 Room 的 reb_sam：接口返回 annie-a 粤语、annie-b 西南官话，state=online，日志有 `language_id` 行；工作台 200。
- 新增 tests/test_language_id.py（主脑接收与过期、切句不被全零帧唤醒、消息带机器人、配置一房一机）和 web/tests/language-id.test.mjs；`uv run --locked pytest -q` 1543 passed，ruff、`uv lock --check`、`npm --prefix web test`（144/0）、typecheck 通过。
- 待真机：Windows 上 bat 实际安装下载、4060 实测速度与显存、真实麦克风下的切句和方言准确率。

## Windows 首次运行报错修复（同日）

- 现象：`install_win.ps1` 第 31 行检测 torch 时中断，“环境准备失败”。原因：Windows PowerShell 5.1 在“出错就停”下，把原生程序写到 stderr 的内容当成致命错误；检测“没装 torch”本来就靠导入失败，于是整个脚本被中断（嘴型服务的脚本用 try/catch 绕开过）。
- 修复：所有“试一下”的调用走 `Invoke-Quiet`（临时改成继续执行，只看退出码）；会打印的安装命令走 `Invoke-Loud`，输出用 `Out-Host` 显示，不混进函数返回值；`-c` 里的 Python 代码不再含双引号（PowerShell 5.1 传参会把内嵌双引号弄坏）。
- 那台机器下载约 400 KB/s：PyTorch 和依赖先走阿里云镜像、模型先走 hf-mirror.com；FireRedLID 推理代码（11 个 .py，Apache-2.0）直接放进 `vendor/`，不再去 GitHub 下载。用放入的代码在 Mac 上复测粤语样本仍判为 `zh yue` 1.0。
- 脚本在 Mac 上无法执行 PowerShell，只做了静态检查，需要在 Windows 上再双击验证。

## 方言跟随：方言词 + 语种旁路综合（同日，用户按推荐：先做、先关着）

- 不加模型，用规则综合：FireRedLID 只听声音给大类，方言词由已有的 `language.py` 按主模型转写文字匹配。
- 开关 `config/runtime_defaults.toml` `[dialect_follow] enabled = false`（`.env` 的 `ANNIE_DIALECT_FOLLOW_ENABLED` 覆盖）。打开后：
  1. 多认方言词：东北（咋整、嘎哈、唠嗑、埋汰、嘚瑟、旮旯…）、山东（杠赛、拉呱、俺寻思…）、上海（侬、阿拉、好伐、结棍、白相…）、湖南（么子、伢子、妹陀、霸得蛮…）；原有粤语/四川/河南/陕西词表照旧常开。天津话语音合成不支持，不加。
  2. 语种旁路连续两句同一大类且把握度 ≥0.9：粤语→粤语、吴语→上海话、西南官话→四川话、湘语→湖南话，按“听口音判断”跟随（和方言词同一条路径，2 分钟内有效，每句同类结果续期）；连续两句普通话就改回。北方官话、闽语不跟（北方分不出哪种，语音合成没有闽南话）。
  3. 话里的方言词指向别的方言时以词为准；用户明确要求的语言/方言优先。
- 实现：`ReplyLanguageState.hear_lid()`、`EXTRA_DIALECT_MARKERS`、`RealtimeRobot.accept_language_id()`（只收本机器人 Room 的结果，跟随变化时发 `language.lid_followed` 事件、日志 `dialect_follow_lid`，并立即刷新提示词）；`_room_sam_event` 在开关打开时把 `language_id` 交给对应机器人。
- 验证：新增 tests/test_dialect_follow.py（词表只在开关打开时生效；一句不跟、两句才跟；改回普通话；北方官话 / 低把握 / 开关关 / 方言词冲突 / 明确要求都不被覆盖；别的 Room 的结果不进来；默认关时只显示）；`uv run --locked pytest -q` 1548 passed，ruff、web 144/0 通过；开关打开的隔离根 `sh start.sh` 工作台 200、`/health` 503（Room 故意连不上），已清理。

## 2026-11-23 修正

真实 Room 实测发现 Room 的麦克风帧带信封、机器人自己的声音会被当成用户，已修，见 [语种旁路真实Room验证与修正](../2026-11-23/语种旁路真实Room验证与修正.md)。上文第一次实测用的是自己编的假 Room（发裸 PCM），没有覆盖这两点。

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

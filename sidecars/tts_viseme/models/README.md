# 本地模型目录

- `forced_aligner/`：Qwen3-ForcedAligner-0.6B 完整 HF snapshot 的**真实文件**，必须有 `config.json`、权重文件和附带的 tokenizer/config 文件；不能只复制 HF cache 的断链 symlink。
- G2PW 现有约 606 MB 模型暂保留在**同一个可搬迁服务目录**的 `../G2PWModel/`；不能只用 Git 克隆代码，该目录在 `.gitignore` 内，需要连同服务目录的真实文件一起复制。
- `.venv/` 是每台 Windows 机器本地安装的 Python 3.11 环境；**不得从 macOS 复制虚拟环境到 Windows**。

若 Windows 曾经下载模型，可以运行 `.venv\Scripts\python.exe model_assets.py --import-cache`，它离线复制 HF 缓存里的完整文件到 `forced_aligner/`。缓存不存在或不完整时，使用 `download_model_win.bat` 单独下载，正式启动永不触发联网下载。

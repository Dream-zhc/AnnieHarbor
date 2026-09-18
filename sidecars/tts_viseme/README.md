# 当前嘴型listener迁移副本

源码来自原工作区project/tts_viseme，保留其已有修改；不是已重写的多头sidecar。

迁入：qwen_stream_room_g2pw.py、forced_aligner_runtime.py、requirements-v3.txt、PROTOCOL.md、install_v3_mac.sh、run_v3_mac.sh。原文件逐字保留与哈希见migration_manifest；本README和.env.example为新根说明。

链路：sam正文预缓存 → tts真实PCM + data.text → G2PW/pypinyin + Qwen3 ForcedAligner → sam mouth_script。listener不调用TTS/LLM API，无需把原POC的Provider密钥带过来。

模型Qwen/Qwen3-ForcedAligner-0.6B；Darwin arm64走MLX，其余平台走qwen-asr/Torch分支。未搬模型缓存/G2PWModel/虚拟环境/输出音频，依赖与模型首次安装/下载留给实际接入阶段。

Mac安装/启动脚本仅依赖本目录，本次没有执行它们。旧Windows启动器依赖旧setup_win与外部venv路径，所以未搬入；目标Windows同套Python Runtime的安装/监督入口待Phase 1。

配置注意：.env.example只列listener参数；parse_args和顶层环境常量先于load_dotenv，URL可用--sam-url/--tts-url，其他顶层参数需在启动前导出。

已知问题保留：单_active_cid、全局pending/control、singleton port；summary经过timing_source校验丢弃。详见Phase 0行为清单。本次不启动进程、不发布音频或嘴型、不声称多头就绪。

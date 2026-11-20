"""Download FireRedLID into models/FireRedLID: the hf-mirror.com mirror first, then Hugging Face."""
import sys
from pathlib import Path

from huggingface_hub import snapshot_download

TARGET = Path(__file__).resolve().parent / "models" / "FireRedLID"
for endpoint in ("https://hf-mirror.com", None):  # the domestic mirror first, then huggingface.co
    try:
        snapshot_download("FireRedTeam/FireRedLID", local_dir=str(TARGET), endpoint=endpoint)
        if (TARGET / "model.pth.tar").is_file():
            print(f"[LID] 模型就绪：{TARGET}")
            sys.exit(0)
    except Exception as exc:  # network: try the next endpoint
        print(f"[LID] 从 {endpoint or 'huggingface.co'} 下载失败：{type(exc).__name__}: {exc}")
sys.exit(1)

"""下载 sherpa 官方中文说话人嵌入模型。不训练，不保存用户录音。"""

import hashlib
import shutil
from pathlib import Path
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[1]
NAME = "3dspeaker_speech_campplus_sv_zh-cn_16k-common.onnx"
URL = ("https://github.com/k2-fsa/sherpa-onnx/releases/download/"
       f"speaker-recongition-models/{NAME}")
SHA256 = "f682b514c05d947ee3fa91cd6ec6c5c7543479a128373fa29b1faedccd21fd11"


def main():
    target = ROOT / "var/models" / NAME
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.is_file():
        digest = hashlib.file_digest(target.open("rb"), "sha256").hexdigest()
        if digest == SHA256:
            print(f"[OK] 声纹模型已就绪: {target}")
            return
        raise RuntimeError(f"已有声纹模型 SHA256 不一致: {digest}")
    temporary = target.with_suffix(".onnx.partial")
    print(f"[INFO] 下载官方中文声纹模型: {NAME}")
    with urlopen(URL, timeout=120) as response, temporary.open("wb") as output:
        shutil.copyfileobj(response, output)
    digest = hashlib.file_digest(temporary.open("rb"), "sha256").hexdigest()
    if digest != SHA256:
        temporary.unlink(missing_ok=True)
        raise RuntimeError(f"下载的声纹模型 SHA256 不一致: {digest}")
    temporary.replace(target)
    print(f"[OK] 模型 SHA256 校验通过: {target}")


if __name__ == "__main__":
    main()

"""在Win/Mac/Linux下载并核验sherpa官方中文KWS，无需训练或额外Python依赖。"""

import hashlib
import shutil
import tarfile
import tempfile
from pathlib import Path
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[1]
NAME = "sherpa-onnx-kws-zipformer-zh-en-3M-2025-12-20"
URL = f"https://github.com/k2-fsa/sherpa-onnx/releases/download/kws-models/{NAME}.tar.bz2"
SHA256 = "68447f4fbc67e70eee3a93961f36e81e98f47aef73ce7e7ca00885c6cd3616a6"
SUFFIX = "-epoch-13-avg-2-chunk-16-left-64.onnx"
FILES = ("encoder" + SUFFIX, "decoder" + SUFFIX, "joiner" + SUFFIX, "tokens.txt")


def main():
    target = ROOT / "var/models" / NAME
    target.mkdir(parents=True, exist_ok=True)
    if all((target / name).is_file() for name in FILES):
        print(f"[OK] 中文KWS模型已就绪: {target}")
        return
    temporary = ROOT / "var/tmp"
    temporary.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="sherpa-kws-", dir=temporary) as folder:
        archive = Path(folder) / "model.tar.bz2"
        print(f"[INFO] 下载官方中文唤醒模型: {NAME}")
        with urlopen(URL, timeout=90) as response, archive.open("wb") as output:
            shutil.copyfileobj(response, output)
        with archive.open("rb") as binary:
            digest = hashlib.file_digest(binary, "sha256").hexdigest()
        if digest != SHA256:
            raise RuntimeError(f"下载的模型SHA256不一致: {digest}")
        with tarfile.open(archive, "r:bz2") as pack:
            for name in FILES:
                source = pack.extractfile(f"{NAME}/{name}")
                if source is None:
                    raise RuntimeError(f"模型包缺少 {name}")
                with source, (target / name).open("wb") as output:
                    shutil.copyfileobj(source, output)
    print(f"[OK] 模型SHA256校验通过: {target}")


if __name__ == "__main__":
    main()

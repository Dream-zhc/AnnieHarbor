"""Manage the portable ForcedAligner weights without implicit network access.

Normal service startup only reads a local directory or imports an existing
Hugging Face cache. Downloads require an explicit --download invocation.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from pathlib import Path


MODEL_ID = "Qwen/Qwen3-ForcedAligner-0.6B"
SERVICE_ROOT = Path(__file__).resolve().parent
MODEL_DIR = SERVICE_ROOT / "models" / "forced_aligner"


def model_error(path: Path) -> str | None:
    """Reject absent/partial snapshots, including broken links or missing shards."""
    if not (path / "config.json").is_file():
        return f"missing config.json in {path}"
    indexes = sorted(path.glob("*.index.json"))
    if indexes:
        for index in indexes:
            try:
                shards = set(json.loads(index.read_text(encoding="utf-8"))["weight_map"].values())
            except (OSError, ValueError, KeyError, TypeError) as exc:
                return f"invalid weight index {index.name}: {exc}"
            for shard in shards:
                candidate = path / shard
                if not candidate.is_file() or candidate.stat().st_size == 0:
                    return f"missing or empty weight shard: {candidate}"
    elif not any(f.is_file() and f.stat().st_size > 0 for pattern in ("*.safetensors", "pytorch_model*.bin") for f in path.glob(pattern)):
        return f"missing model weights in {path}"
    return None


def import_cache() -> Path:
    """Materialize real bytes instead of HF cache symlinks; safe to move hosts."""
    if MODEL_DIR.exists():
        problem = model_error(MODEL_DIR)
        if problem:
            raise RuntimeError(f"local model incomplete: {problem}; repair it explicitly")
        return MODEL_DIR
    os.environ["HF_HUB_OFFLINE"] = "1"
    from huggingface_hub import snapshot_download

    source = Path(snapshot_download(MODEL_ID, local_files_only=True))
    problem = model_error(source)
    if problem:
        raise RuntimeError(f"cached snapshot incomplete: {problem}")
    MODEL_DIR.parent.mkdir(parents=True, exist_ok=True)
    temporary = MODEL_DIR.with_name("forced_aligner.importing")
    if temporary.exists():
        raise RuntimeError(f"unfinished import at {temporary}; inspect before retrying")
    try:
        shutil.copytree(source, temporary, symlinks=False)
        problem = model_error(temporary)
        if problem:
            raise RuntimeError(problem)
        temporary.rename(MODEL_DIR)
    except BaseException:
        if temporary.exists():
            shutil.rmtree(temporary)
        raise
    return MODEL_DIR


def download() -> Path:
    """Opt-in network setup; never called by the realtime process."""
    from huggingface_hub import snapshot_download

    os.environ.pop("HF_HUB_OFFLINE", None)
    os.environ.pop("TRANSFORMERS_OFFLINE", None)
    MODEL_DIR.mkdir(parents=True, exist_ok=True)
    snapshot_download(MODEL_ID, local_dir=str(MODEL_DIR), max_workers=4)
    problem = model_error(MODEL_DIR)
    if problem:
        raise RuntimeError(problem)
    return MODEL_DIR


def main() -> int:
    parser = argparse.ArgumentParser(description="Prepare self-contained, offline ForcedAligner weights")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--import-cache", action="store_true", help="copy complete existing HF cache, no network")
    group.add_argument("--download", action="store_true", help="explicitly download weights into this folder")
    args = parser.parse_args()
    try:
        if args.download:
            directory = download()
        elif args.import_cache:
            directory = import_cache()
        else:
            problem = model_error(MODEL_DIR)
            if problem:
                raise RuntimeError(problem)
            directory = MODEL_DIR
    except Exception as exc:
        print(f"[MODEL ERROR] {exc}", file=sys.stderr)
        print(f"[MODEL] Put complete {MODEL_ID} files in {MODEL_DIR}", file=sys.stderr)
        print("[MODEL] Or run: python model_assets.py --download (requires Hugging Face access)", file=sys.stderr)
        return 2
    print(f"[MODEL] offline weights ready: {directory}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

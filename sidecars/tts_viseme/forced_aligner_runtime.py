"""Qwen3-ForcedAligner runtime used by the TTS viseme sidecar.

Timing ownership lives here.  G2PW remains pronunciation/viseme mapping only.
On Apple Silicon we use the native MLX implementation; other platforms use
the official qwen-asr wrapper (CUDA when available, CPU as a last resort).
"""

from __future__ import annotations

import os
import platform
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, List

import numpy as np


DEFAULT_MODEL = "Qwen/Qwen3-ForcedAligner-0.6B"
LOCAL_MODEL = Path(__file__).resolve().parent / "models" / "forced_aligner"


@dataclass(frozen=True)
class AlignedChar:
    text: str
    start_s: float
    end_s: float


def pcm16_to_mono_f32(pcm: bytes, channels: int) -> np.ndarray:
    if not pcm:
        return np.asarray([], dtype=np.float32)
    samples = np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32768.0
    ch = max(1, int(channels or 1))
    if ch > 1:
        usable = (len(samples) // ch) * ch
        samples = samples[:usable].reshape(-1, ch).mean(axis=1)
    return np.ascontiguousarray(samples, dtype=np.float32)


def resample_linear(audio: np.ndarray, source_rate: int, target_rate: int = 16000) -> np.ndarray:
    """Small dependency-free resampler for the aligner's 16 kHz input."""
    src = max(1, int(source_rate or target_rate))
    dst = max(1, int(target_rate))
    audio = np.asarray(audio, dtype=np.float32).reshape(-1)
    if not len(audio) or src == dst:
        return np.ascontiguousarray(audio, dtype=np.float32)
    count = max(1, int(round(len(audio) * dst / float(src))))
    source_pos = np.arange(len(audio), dtype=np.float64)
    target_pos = np.arange(count, dtype=np.float64) * (src / float(dst))
    target_pos = np.minimum(target_pos, max(0, len(audio) - 1))
    return np.interp(target_pos, source_pos, audio).astype(np.float32, copy=False)


class QwenForcedAlignerRuntime:
    def __init__(self, model: str | None = None, backend: str | None = None) -> None:
        self.model_id = str(
            model or os.getenv("TTS_VISEME_ALIGNER_MODEL")
            or (LOCAL_MODEL if LOCAL_MODEL.is_dir() else DEFAULT_MODEL)
        ).strip()
        requested = str(backend or os.getenv("TTS_VISEME_ALIGNER_BACKEND") or "auto").strip().lower()
        if requested == "auto":
            if platform.system() == "Darwin" and platform.machine().lower() in {"arm64", "aarch64"}:
                requested = "mlx"
            else:
                requested = "qwen_asr"
        if requested not in {"mlx", "qwen_asr"}:
            raise ValueError(f"unsupported forced-aligner backend: {requested}")
        self.backend = requested
        self._aligner: Any = None
        self._torch: Any = None
        self.load_ms: float | None = None
        self.online_warm_ms: float | None = None
        self.last_align_ms: float | None = None

    def load(self) -> None:
        if self._aligner is not None:
            return
        started = time.perf_counter()
        if self.backend == "mlx":
            from mlx_qwen3_asr import ForcedAligner

            self._aligner = ForcedAligner(self.model_id, backend="mlx")
            # ForcedAligner is lazy.  Compile/load before room listeners start so
            # the first user turn does not pay the model cold-start cost.
            warm = np.zeros(8000, dtype=np.float32)
            # The online path starts with roughly 500 ms PCM and four Han
            # characters. Warm that production shape, not a one-character toy,
            # so the first real mouth packet does not pay MLX graph compilation.
            self._aligner.align(warm, "测试一下", "Chinese")
        else:
            import torch
            from qwen_asr import Qwen3ForcedAligner

            self._torch = torch
            if torch.cuda.is_available():
                device = "cuda:0"
                dtype = torch.bfloat16
            else:
                device = "cpu"
                dtype = torch.float32
            self._aligner = Qwen3ForcedAligner.from_pretrained(
                self.model_id,
                device_map=device,
                dtype=dtype,
            )
            warm = np.zeros(8000, dtype=np.float32)
            self._aligner.align((warm, 16000), "测试一下", "Chinese")
        self.load_ms = (time.perf_counter() - started) * 1000.0

    def warm_online(self, sample_rate: int = 24000) -> None:
        """Compile the production ~500 ms / 4-char online alignment shape."""
        sr = max(1, int(sample_rate or 24000))
        pcm = b"\x00\x00" * max(1, int(round(sr * 0.5)))
        started = time.perf_counter()
        self.align_pcm(
            pcm,
            "测试一下",
            sample_rate=sr,
            channels=1,
            language="Chinese",
        )
        self.online_warm_ms = (time.perf_counter() - started) * 1000.0

    def align_pcm(
        self,
        pcm: bytes,
        text: str,
        *,
        sample_rate: int,
        channels: int = 1,
        language: str = "Chinese",
    ) -> List[AlignedChar]:
        if self._aligner is None:
            self.load()
        mono = pcm16_to_mono_f32(pcm, channels)
        if not len(mono) or not str(text or "").strip():
            return []
        started = time.perf_counter()
        if self.backend == "mlx":
            audio = resample_linear(mono, sample_rate, 16000)
            raw = self._aligner.align(audio, text, language)
        else:
            raw_result = self._aligner.align((mono, int(sample_rate)), text, language)
            raw = raw_result[0] if raw_result else []
        self.last_align_ms = (time.perf_counter() - started) * 1000.0
        out: List[AlignedChar] = []
        for item in raw:
            token = str(getattr(item, "text", "") or "").strip()
            try:
                start_s = float(getattr(item, "start_time"))
                end_s = float(getattr(item, "end_time"))
            except (TypeError, ValueError):
                continue
            if not token or end_s < start_s or start_s < 0:
                continue
            out.append(AlignedChar(token, start_s, end_s))
        return out

    def describe(self) -> str:
        load = "-" if self.load_ms is None else f"{self.load_ms:.1f}ms"
        online = "-" if self.online_warm_ms is None else f"{self.online_warm_ms:.1f}ms"
        return f"backend={self.backend} model={self.model_id} warmup={load} online_warm={online}"

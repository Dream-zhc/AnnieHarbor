"""sherpa-onnx streaming Chinese keyword detector, model shared, stream per robot."""

import asyncio
import math
import time
from pathlib import Path


class SherpaWakeModel:
    """One native model, one serialized decoder, independent streaming state."""

    def __init__(self, model_dir: Path, *, keywords: Path, threshold: float = 0.35,
                 num_threads: int = 1):
        if not 0 < threshold <= 1 or not isinstance(num_threads, int) or not 1 <= num_threads <= 4:
            raise ValueError("invalid sherpa keyword threshold or num_threads")
        model_dir = Path(model_dir)
        keywords = Path(keywords)
        suffix = "-epoch-13-avg-2-chunk-16-left-64.onnx"
        encoder, decoder, joiner = (model_dir / f"{name}{suffix}"
                                    for name in ("encoder", "decoder", "joiner"))
        tokens = model_dir / "tokens.txt"
        for file in (encoder, decoder, joiner, tokens, keywords):
            if not file.is_file():
                raise ValueError(f"sherpa KWS asset missing: {file}")
        if not keywords.read_text(encoding="utf-8").strip():
            raise ValueError("sherpa KWS keywords file is empty")
        try:
            import numpy as np
            import sherpa_onnx
        except ImportError as exc:
            raise RuntimeError("sherpa wakeword requires installed sherpa-onnx") from exc
        self.numpy = np
        self.threshold = threshold
        self.keywords_name = keywords.name
        self.engine = sherpa_onnx.KeywordSpotter(
            tokens=str(tokens), encoder=str(encoder), decoder=str(decoder),
            joiner=str(joiner), keywords_file=str(keywords),
            keywords_threshold=threshold, num_threads=num_threads,
            provider="cpu",
        )
        self.lock = asyncio.Lock()
        self.model_name = model_dir.name

    def new_stream(self):
        return SherpaWakeStream(self)


class SherpaWakeStream:
    """Per-robot streaming fbank/Zipformer caches; 16k mono s16le input."""

    def __init__(self, shared: SherpaWakeModel):
        self.shared = shared
        self.stream = shared.engine.create_stream()
        self.decoded_batches = 0
        self.input_dbfs = None
        self.quiet_batches = 0
        self.last_keyword = None
        self.last_decode_ms = None

    def _decode(self, pcm):
        began = time.perf_counter()
        samples = (self.shared.numpy.frombuffer(pcm, dtype="<i2")
                   .astype(self.shared.numpy.float32) / 32768.0)
        # Measure exactly the PCM consumed by KWS, not the browser's meter.
        # The samples array already exists; once per 100ms is inexpensive.
        if samples.size:
            rms = math.sqrt(float(self.shared.numpy.mean(samples * samples)))
            self.input_dbfs = round(20 * math.log10(max(rms, 1e-5)), 1)
            self.quiet_batches = self.quiet_batches + 1 if self.input_dbfs < -55 else 0
        self.decoded_batches += 1
        self.stream.accept_waveform(16000, samples)
        engine = self.shared.engine
        try:
            while engine.is_ready(self.stream):
                engine.decode_stream(self.stream)
                keyword = engine.get_result(self.stream)
                if keyword:
                    self.last_keyword = str(keyword)
                    engine.reset_stream(self.stream)
                    return True
            return False
        finally:
            self.last_decode_ms = round((time.perf_counter() - began) * 1000, 2)

    async def detected(self, pcm):
        async with self.shared.lock:
            return await asyncio.to_thread(self._decode, pcm)

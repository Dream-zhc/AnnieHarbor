"""Short, memory-only listening after a conversation while the wake gate sleeps.

Why bounded: the useful context is what people say right after talking to the
robot ("我们刚才在聊……"); always-on cloud transcription of a home would cost
money all day and expose private talk. So the window opens only when a reply
finishes, lasts three minutes and sends audio only while it is loud enough. The
owner keeps the heard lines in a small in-memory buffer, cleared on a sleep
command, a session change or shutdown; nothing is persisted or memorised.
"""

import math
import time
from array import array

WINDOW_SECONDS = 180.0
ENERGY_DBFS = -45.0  # quieter frames are not uploaded at all (no cloud cost)
# Keep 1.2 s after the last loud frame: the ASR server VAD needs 0.8 s of
# silence before it emits the finished sentence.
HANGOVER_FRAMES = 60
# Context, not a reply: frames may wait while the lazy ASR connection opens.
MAX_LAG_MS = 3000
QUEUE_FRAMES = 150


def frame_dbfs(pcm: bytes) -> float:
    samples = array("h", pcm)
    if not samples:
        return -120.0
    power = sum(sample * sample for sample in samples) / len(samples)
    return 10 * math.log10(power / (32768 * 32768)) if power else -120.0


class AmbientListener:
    def __init__(self, sidecar, on_line, *, clock=time.monotonic,
                 window_seconds=WINDOW_SECONDS, energy_dbfs=ENERGY_DBFS):
        self.sidecar = sidecar
        self.on_line = on_line  # owner keeps the bounded, memory-only lines
        self.clock = clock
        self.window_seconds = window_seconds
        self.energy_dbfs = energy_dbfs
        self.until = None
        self._hangover = 0
        self.uploaded_frames = 0
        self.heard = 0

    def open_window(self):
        self.until = self.clock() + self.window_seconds

    def clear(self, reason):
        self.until = None
        self._hangover = 0
        self.sidecar.invalidate(reason)

    def listening(self):
        return self.until is not None and self.clock() < self.until

    def offer(self, pcm, *, identity, stream_epoch, source_id, received_at):
        """Hot path from the 20 ms audio pump while the wake gate sleeps."""
        if not self.listening():
            if self.until is not None:
                self.clear("window_closed")
            return False
        if frame_dbfs(pcm) >= self.energy_dbfs:
            self._hangover = HANGOVER_FRAMES
        elif self._hangover:
            self._hangover -= 1
        else:
            return False
        sent = self.sidecar.offer(pcm, identity=identity, stream_epoch=stream_epoch,
                                  source_id=source_id, received_at=received_at,
                                  sample_start=None)
        self.uploaded_frames += bool(sent)
        return sent

    def observe(self, row):
        """AcousticSidecar callback: keep only finished, non-empty sentences."""
        text = str(row.get("text") or "").strip()
        if row.get("kind") == "completed" and text and self.listening():
            self.heard += 1
            self.on_line(text)

    def snapshot(self):
        return {"listening": self.listening(),
                "heard": self.heard, "uploaded_frames": self.uploaded_frames,
                "remaining_s": (round(self.until - self.clock())
                                if self.listening() else 0),
                "sidecar": self.sidecar.snapshot()}

"""When the user's voice really ended, against when the provider said so (11-19).

The latency logs start at the provider's speech_stopped, which only comes after
smart_turn has decided the turn is over: the 0.8-1.4 s the user waits for that
decision was in no log. Every PCM chunk forwarded to the provider is noted here
with the moment it left and whether the local ear heard voice in it (the same
floor-following level as the listening nods in motions.py). The provider's
audio_start_ms / audio_end_ms count from the session's first sample, so they map
back to the moment that audio was sent.
"""

import time
from collections import deque

from annie_runtime.ambient import frame_dbfs

SAMPLES_PER_MS = 16
HISTORY_CHUNKS = 3000
VOICE_ABOVE_FLOOR_DB = 10.0
FLOOR_RISE_DB = 0.02
FLOOR_MIN_DBFS = -90.0


class VoiceTiming:
    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self.chunks = deque(maxlen=HISTORY_CHUNKS)
        self.floor = None

    def reset(self):
        """A new provider session counts its audio from zero again."""
        self.chunks.clear()

    def sent(self, pcm, first_sample):
        level = frame_dbfs(pcm)
        floor = level if self.floor is None or level < self.floor else self.floor + FLOOR_RISE_DB
        self.floor = max(FLOOR_MIN_DBFS, floor)
        self.chunks.append((first_sample, first_sample + len(pcm) // 2, self.clock(),
                            level >= self.floor + VOICE_ABOVE_FLOOR_DB))

    def sent_at(self, provider_ms):
        """When the chunk holding this provider offset left for the provider, or None."""
        if type(provider_ms) is not int or provider_ms < 0:
            return None
        sample = provider_ms * SAMPLES_PER_MS
        for first, end, at, _voiced in reversed(self.chunks):
            if first <= sample:
                return at if sample <= end else None
        return None

    def voice_end(self, since):
        """When the last chunk with voice in it left (not before `since`), or None."""
        for _first, _end, at, voiced in reversed(self.chunks):
            if at < since:
                return None
            if voiced:
                return at
        return None

    def floor_dbfs(self):
        return None if self.floor is None else round(self.floor, 1)


def ms_between(start, end):
    """Milliseconds from one monotonic moment to a later one; None when unknown."""
    if start is None or end is None or end < start:
        return None
    return round((end - start) * 1000)

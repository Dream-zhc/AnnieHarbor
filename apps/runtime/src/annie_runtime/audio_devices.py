"""Which microphones and speakers are working in one robot Room right now.

The product rule is one microphone and one speaker per Room. Nothing here
blocks a second device; it only reports what was observed so the workbench
and the big screen can warn. Evidence, in order of strength:

1. Device reports on reb_voice: mic.capture.* from the board program or the
   web microphone, speaker.presence from the board speaker or the web audition.
2. Two playback_ids reporting the same TTS stream (also catches old players).
3. MIC_AUDIO arriving faster than one 20 ms stream for several seconds
   (also catches old microphones that send no reports).

Room's own PRESENCE/size messages are not used: their meaning is unverified.
"""

import logging
import time
from collections import deque

DEVICE_TTL_SECONDS = 6.0  # reports come every 1-2 s
FRAME_RATE_LIMIT = 62  # one 20 ms stream is 50 frames/s
FRAME_RATE_SECONDS = 3  # consecutive seconds above the limit
PLAYBACK_WINDOW_SECONDS = 5.0
ROLES = ("microphone", "speaker")
logger = logging.getLogger(__name__)


def _text(value, limit):
    return value[:limit] if isinstance(value, str) and value else None


class RoomAudioDevices:
    def __init__(self, *, clock=time.monotonic, on_change=None):
        self.clock = clock
        self.on_change = on_change  # (status, snapshot) on every conflict transition
        self.devices = {role: {} for role in ROLES}
        self._second = None
        self._frames_this_second = 0
        self.frames_per_second = deque(maxlen=FRAME_RATE_SECONDS)
        self._playbacks = {}  # (conversation_id, epoch) -> {playback_id: (source, last_seen)}
        self._status = "ok"

    # ---- inputs -----------------------------------------------------------------
    def mic_frame(self):
        """Hot path, once per received MIC_AUDIO frame: counters only."""
        second = int(self.clock())
        if second != self._second:
            if self._second is not None:
                self.frames_per_second.append(self._frames_this_second)
                if second - self._second > 1:
                    self.frames_per_second.clear()  # a gap is not a rate
            self._second = second
            self._frames_this_second = 0
            self._frames_this_second += 1
            self.evaluate()
            return
        self._frames_this_second += 1

    def report(self, message):
        """mic.capture.* or speaker.presence from reb_voice; False if not a device report."""
        kind = message.get("type") if isinstance(message, dict) else None
        data = message.get("data") if isinstance(message, dict) else None
        if not isinstance(data, dict):
            return False
        if isinstance(kind, str) and kind.startswith("mic.capture.") and \
                data.get("sender") == "microphone":
            role = "microphone"
        elif kind == "speaker.presence" and data.get("sender") in {"speaker", "browser_test"}:
            role = "speaker"
        else:
            return False
        # Old web pages send only sender: one anonymous browser microphone.
        instance = _text(data.get("instance_id"), 64) or "browser-microphone"
        stopped = kind == "mic.capture.stopped" or data.get("state") == "stopped"
        if stopped:
            self.devices[role].pop(instance, None)
        else:
            self.devices[role][instance] = {
                "instance_id": instance,
                "host": _text(data.get("host"), 64),
                "device_name": _text(data.get("device_name"), 80),
                "kind": "browser" if instance.startswith("browser") else "device",
                "state": _text(data.get("state"), 24),
                "room_connected": data.get("room_connected")
                if isinstance(data.get("room_connected"), bool) else None,
                "peak_dbfs": data.get("peak_dbfs")
                if type(data.get("peak_dbfs")) in (int, float) else None,
                "dropped_frames": data.get("dropped_frames")
                if type(data.get("dropped_frames")) is int else None,
                "last_seen": self.clock(),
            }
        self.evaluate()
        return True

    def playback_receipt(self, message):
        """A verified tts.playback.* receipt from a speaker or the web audition."""
        data = message.get("data") if isinstance(message, dict) else None
        if not isinstance(data, dict):
            return
        key = (data.get("conversation_id"), data.get("stream_epoch"))
        playback_id = _text(data.get("playback_id"), 128)
        if playback_id is None or not isinstance(key[0], str):
            return
        now = self.clock()
        self._playbacks.setdefault(key, {})[playback_id] = (data.get("source"), now)
        self.evaluate()

    # ---- state ------------------------------------------------------------------
    def _expire(self, now):
        for role in ROLES:
            for instance, row in list(self.devices[role].items()):
                if now - row["last_seen"] > DEVICE_TTL_SECONDS:
                    del self.devices[role][instance]
        for key, rows in list(self._playbacks.items()):
            for playback_id, (_, seen) in list(rows.items()):
                if now - seen > PLAYBACK_WINDOW_SECONDS:
                    del rows[playback_id]
            if not rows:
                del self._playbacks[key]

    def conflicts(self, now=None):
        now = self.clock() if now is None else now
        self._expire(now)
        found = []
        for role in ROLES:
            if len(self.devices[role]) > 1:
                found.append({"role": role, "reason": "multiple_devices",
                              "count": len(self.devices[role])})
        rates = list(self.frames_per_second)
        if self._second is None or int(now) - self._second > 1:
            rates = []  # no audio right now: an old burst is not a second microphone
        if (len(rates) == FRAME_RATE_SECONDS and min(rates) > FRAME_RATE_LIMIT
                and not any(item["role"] == "microphone" for item in found)):
            found.append({"role": "microphone", "reason": "frame_rate",
                          "frames_per_second": max(rates)})
        players = max((rows for rows in self._playbacks.values()), key=len, default={})
        if len(players) > 1 and not any(item["role"] == "speaker" for item in found):
            found.append({"role": "speaker", "reason": "multiple_playbacks",
                          "count": len(players),
                          "sources": sorted({source for source, _ in players.values()
                                             if isinstance(source, str)})})
        return found

    def snapshot(self):
        now = self.clock()
        conflicts = self.conflicts(now)
        return {
            "status": "conflict" if conflicts else "ok",
            "conflicts": conflicts,
            "microphones": [self._public(row, now) for row in self.devices["microphone"].values()],
            "speakers": [self._public(row, now) for row in self.devices["speaker"].values()],
            "mic_frames_per_second": (self.frames_per_second[-1] if self.frames_per_second
                                      and self._second is not None
                                      and int(now) - self._second <= 1 else None),
        }

    @staticmethod
    def _public(row, now):
        public = {key: value for key, value in row.items() if key != "last_seen"}
        public["age_ms"] = int((now - row["last_seen"]) * 1000)
        return public

    def evaluate(self):
        """Publish only transitions; callers read snapshot() for details."""
        status = "conflict" if self.conflicts() else "ok"
        if status != self._status:
            self._status = status
            if self.on_change is not None:
                try:
                    self.on_change(status, self.snapshot())
                except Exception:
                    # Called from the MIC_AUDIO hot path: a warning must never
                    # cost audio.
                    logger.exception("audio device warning failed")

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

Robot heads (room-speaker/head.py): two boards can serve one Room, e.g. the
big head and the obj doll. The board started later takes over and the older
one stops itself. robot.head.active / robot.head.released on reb_voice say
which head is in use; for a few seconds around a handover the older head's
devices are not counted as a second device.
"""

import logging
import time
from collections import deque

DEVICE_TTL_SECONDS = 6.0  # reports come every 1-2 s
FRAME_RATE_LIMIT = 62  # one 20 ms stream is 50 frames/s
FRAME_RATE_SECONDS = 3  # consecutive seconds above the limit
PLAYBACK_WINDOW_SECONDS = 5.0
HEAD_TTL_SECONDS = 6.0  # robot.head.active comes every 2 s
# The older board stops within ~1 s; one that died without its stop reports
# lingers until DEVICE_TTL_SECONDS, so the grace outlasts that.
HANDOVER_GRACE_SECONDS = DEVICE_TTL_SECONDS + 1.0
HEAD_KINDS = ("robot.head.active", "robot.head.released")
HEAD_FIELDS = ("head_id", "head_name", "host", "address")
ROLES = ("microphone", "speaker")
logger = logging.getLogger(__name__)


def _text(value, limit):
    return value[:limit] if isinstance(value, str) and value else None


def _volume(value):
    return value if type(value) is int and 0 <= value <= 100 else None


class RoomAudioDevices:
    def __init__(self, *, clock=time.monotonic, on_change=None, on_head=None):
        self.clock = clock
        self.on_change = on_change  # (status, snapshot) on every conflict transition
        self.on_head = on_head  # (kind, details): online / handover / offline
        self.heads = {}  # instance_id -> row, every head program still announcing
        self.current_head = None
        self.handover = None
        self._handover_since = None
        self.volume_acks = {}  # request_id -> volume the speaker confirmed
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
        if kind in HEAD_KINDS and data.get("sender") == "room_audio":
            return self._head(kind, data)
        if kind == "speaker.volume" and data.get("sender") == "speaker":
            return self._volume_ack(data)
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
                "head_instance": _text(data.get("head_instance"), 64),
                "head_name": _text(data.get("head_name"), 40),
                # Board speakers that can be turned up or down by voice report it.
                "volume": _volume(data.get("volume")) if role == "speaker" else None,
                "last_seen": self.clock(),
            }
        self.evaluate()
        return True

    def _volume_ack(self, data):
        volume, request_id = _volume(data.get("volume")), _text(data.get("request_id"), 64)
        if volume is None or request_id is None:
            return False
        row = self.devices["speaker"].get(_text(data.get("instance_id"), 64) or "")
        if row is not None:
            row["volume"] = volume
        self.volume_acks[request_id] = volume
        while len(self.volume_acks) > 32:
            self.volume_acks.pop(next(iter(self.volume_acks)))
        return True

    def speaker_volume(self):
        """Volume of the board speaker in use; None if none can be set by voice."""
        now = self.clock()
        self._expire(now)
        rows = [row for row in self.devices["speaker"].values()
                if row["kind"] == "device" and row.get("volume") is not None]
        if self._handing_over(now) and self.current_head is not None:
            rows = [row for row in rows if row.get("head_instance") in (None, self.current_head)]
        return rows[0]["volume"] if rows else None

    def _head(self, kind, data):
        instance = _text(data.get("instance_id"), 64)
        if instance is None:
            return False
        now = self.clock()
        ref = {"instance_id": instance, **{key: _text(data.get(key), 64) for key in HEAD_FIELDS}}
        if kind == "robot.head.released":
            self.heads.pop(instance, None)
            for role in ROLES:  # in case its own stop reports were lost
                for device, row in list(self.devices[role].items()):
                    if row.get("head_instance") == instance:
                        del self.devices[role][device]
            newer = data.get("taken_over_by")
            if data.get("reason") == "taken_over" and isinstance(newer, dict):
                self.handover = {
                    "from": ref,
                    "to": {key: _text(newer.get(key), 64) for key in ("instance_id", *HEAD_FIELDS)},
                    "at_ms": time.time_ns() // 1_000_000, "seen": now,
                }
                self._notify("handover", self._handover_public(now))
            self._select_head(now, released=ref)
            self.evaluate()
            return True
        active_for = data.get("active_for_ms")
        row = self.heads.get(instance)
        if row is None:
            seconds = active_for / 1000 if type(active_for) is int and active_for >= 0 else 0.0
            row = self.heads[instance] = {"since": now - seconds}
        row.update(ref)
        row.update(started_at_ms=data.get("started_at_ms")
                   if type(data.get("started_at_ms")) is int else None,
                   microphone=_text(data.get("microphone"), 80),
                   speaker=_text(data.get("speaker"), 80), last_seen=now)
        self._select_head(now)
        self.evaluate()
        return True

    def _select_head(self, now, released=None):
        """The head that started last is the one in use; tell the screens when it changes."""
        gone = released
        for instance, row in list(self.heads.items()):
            if now - row["last_seen"] > HEAD_TTL_SECONDS:  # powered off or unplugged
                if instance == self.current_head:
                    gone = {key: row.get(key) for key in ("instance_id", *HEAD_FIELDS)}
                del self.heads[instance]
        newest = max(self.heads.values(), key=lambda row: row["since"], default=None)
        current = newest["instance_id"] if newest else None
        if current == self.current_head:
            return
        previous, self.current_head = self.current_head, current
        if previous is not None and previous in self.heads:
            self._handover_since = now  # the older one is about to stop
        if newest is not None:
            self._notify("online", self._head_public(newest, now))
        elif previous is not None:
            self._notify("offline", gone or {"instance_id": previous})

    def _notify(self, kind, details):
        if self.on_head is None:
            return
        try:
            self.on_head(kind, details)
        except Exception:
            logger.exception("robot head notice failed")

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

    def _handing_over(self, now):
        return self._handover_since is not None and \
            now - self._handover_since < HANDOVER_GRACE_SECONDS

    def conflicts(self, now=None):
        now = self.clock() if now is None else now
        self._expire(now)
        self._select_head(now)
        handing_over = self._handing_over(now)
        found = []
        for role in ROLES:
            rows = list(self.devices[role].values())
            if handing_over:  # the older head's devices are on their way out
                rows = [row for row in rows if row.get("head_instance") in (None, self.current_head)]
            if len(rows) > 1:
                found.append({"role": role, "reason": "multiple_devices", "count": len(rows)})
        rates = list(self.frames_per_second)
        if self._second is None or int(now) - self._second > 1:
            rates = []  # no audio right now: an old burst is not a second microphone
        if (len(rates) == FRAME_RATE_SECONDS and min(rates) > FRAME_RATE_LIMIT
                and not any(item["role"] == "microphone" for item in found)):
            found.append({"role": "microphone", "reason": "frame_rate",
                          "frames_per_second": max(rates)})
        players = max((rows for rows in self._playbacks.values()), key=len, default={})
        if len(players) > 1 and not handing_over and \
                not any(item["role"] == "speaker" for item in found):
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
            "head": {
                "current": (self._head_public(self.heads[self.current_head], now)
                            if self.current_head in self.heads else None),
                "heads": [self._head_public(row, now) for row in
                          sorted(self.heads.values(), key=lambda row: -row["since"])],
                "handover": self._handover_public(now),
            },
        }

    @staticmethod
    def _public(row, now):
        public = {key: value for key, value in row.items() if key != "last_seen"}
        public["age_ms"] = int((now - row["last_seen"]) * 1000)
        return public

    @staticmethod
    def _head_public(row, now):
        public = {key: value for key, value in row.items() if key not in {"since", "last_seen"}}
        public["active_for_ms"] = int((now - row["since"]) * 1000)
        public["age_ms"] = int((now - row["last_seen"]) * 1000)
        return public

    def _handover_public(self, now):
        if self.handover is None:
            return None
        public = {key: value for key, value in self.handover.items() if key != "seen"}
        public["age_ms"] = int((now - self.handover["seen"]) * 1000)
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

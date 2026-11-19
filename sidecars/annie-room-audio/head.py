"""Which robot head owns this Room's microphone and speaker.

Two boards may serve the same Room, for example the big head and the obj doll,
and they are not meant to run at the same time. If both do, the one started
later keeps the Room and the earlier one stops itself.

Every running program announces itself on reb_voice (robot.head.active) when
it connects and every 2 s after, with how long it has been running. A program
that hears another one with a shorter running time knows it is the older one:
it stops playing and capturing, says robot.head.released and exits. Running
times come from each board's own monotonic clock, so boards whose wall clocks
disagree (no RTC before NTP) still agree on who started later.

Other Room clients can show which head is in use from the same messages.
"""

from __future__ import annotations

import asyncio
import json
import logging
import socket
import time
import uuid

HEAD_INTERVAL = 2.0
EXIT_TAKEN_OVER = 10  # systemd RestartPreventExitStatus: a yielded board stays stopped
logger = logging.getLogger("speaker.head")


def local_address(host, port):
    """This board's LAN address toward the Room server, for display only."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.connect((host, int(port)))  # a UDP connect sends nothing
            return probe.getsockname()[0]
    except (OSError, ValueError):
        return None


def _text(value, limit):
    return value[:limit] if isinstance(value, str) and value else None


class Head:
    def __init__(self, head_id="", head_name="", *, address=None, microphone=None,
                 speaker=None, clock=time.monotonic):
        self.clock = clock
        self.started = clock()
        self.instance_id = f"head-{uuid.uuid4().hex[:12]}"
        self.host = socket.gethostname()[:64]
        self.head_id = _text(head_id, 40)
        self.head_name = _text(head_name, 40) or self.head_id or self.host
        self.address = address
        self.started_at_ms = time.time_ns() // 1_000_000  # display only
        self.microphone = _text(microphone, 80)
        self.speaker = _text(speaker, 80)
        self.taken_over_by = None
        self.announced_on = None  # the reb_voice socket this head was last announced on

    def identity(self):
        """Merged into the device reports so each device names its head."""
        return {"head_instance": self.instance_id, "head_id": self.head_id,
                "head_name": self.head_name}

    def ref(self):
        return {"instance_id": self.instance_id, "head_id": self.head_id,
                "head_name": self.head_name, "host": self.host, "address": self.address}

    def active_for_ms(self):
        return int((self.clock() - self.started) * 1000)

    def active_message(self):
        data = {"protocol_version": 1, "sender": "room_audio", **self.ref(),
                "started_at_ms": self.started_at_ms, "active_for_ms": self.active_for_ms(),
                "microphone": self.microphone, "speaker": self.speaker}
        return {"type": "robot.head.active", "description": f"{self.head_name}在用本房间的麦克风和扬声器",
                "ts": time.time_ns() // 1_000_000,
                "data": {key: value for key, value in data.items() if value is not None}}

    def released_message(self, reason):
        data = {"protocol_version": 1, "sender": "room_audio", **self.ref(), "reason": reason}
        description = f"{self.head_name}已断开"
        if self.taken_over_by:
            data["taken_over_by"] = self.taken_over_by
            description += f"：{self.taken_over_by.get('head_name') or '另一个头'}接管"
        return {"type": "robot.head.released", "description": description,
                "ts": time.time_ns() // 1_000_000,
                "data": {key: value for key, value in data.items() if value is not None}}

    def hear(self, message):
        """True once another program that started later is announced: yield to it."""
        if self.taken_over_by is not None or not isinstance(message, dict) \
                or message.get("type") != "robot.head.active":
            return False
        data = message.get("data")
        if not isinstance(data, dict) or data.get("sender") != "room_audio":
            return False
        other = data.get("instance_id")
        active_for = data.get("active_for_ms")
        if not isinstance(other, str) or other == self.instance_id \
                or type(active_for) is not int or active_for < 0:
            return False
        if active_for >= self.active_for_ms():
            return False  # it is the older one; it will hear us and stop
        newer = {key: _text(data.get(key), 64) for key in
                 ("instance_id", "head_id", "head_name", "host", "address")}
        self.taken_over_by = {key: value for key, value in newer.items() if value is not None}
        return True


class AnnouncedLink:
    """reb_voice for device reports, open only once this head was announced on it.

    Listeners then always learn a new head before its microphone and speaker,
    so a handover never looks like a second device in the Room.
    """

    def __init__(self, voice, head):
        self.voice, self.head = voice, head

    @property
    def ready(self):
        socket = self.voice.socket
        return self.voice.ready and socket is not None and self.head.announced_on is socket

    async def send(self, payload):
        return self.ready and await self.voice.send(payload)


async def report_head(voice, head, *, interval=HEAD_INTERVAL, ready=None):
    """At once on every (re)connect, then every interval while connected.

    ready() gates the first announcement: a board whose card is there but
    delivers nothing restarts again and again, and must not take the Room
    from a board that works each time it starts.
    """
    last = 0.0
    while True:
        if voice.ready and (ready is None or head.announced_on is not None or ready()):
            now = time.monotonic()
            socket = voice.socket
            if head.announced_on is not socket or now - last >= interval:
                if await voice.send(json.dumps(head.active_message(), ensure_ascii=False)):
                    head.announced_on, last = socket, now
        await asyncio.sleep(0.1)

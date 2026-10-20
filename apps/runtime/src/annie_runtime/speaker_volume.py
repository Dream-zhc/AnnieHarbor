"""Turn the robot's speaker up or down by voice ("大声点", "音量调到一半").

The board speaker program (room-speaker) applies a software volume 0-100 and
confirms it on reb_voice; 100 is the loudness the device mixer already gives.
Without such a speaker (a browser audition only) the caller falls back to the
TTS volume preference, so the old spoken setting keeps working.
"""

import asyncio
import uuid

VOLUME_STEP = 20  # one "大声点"
VOLUME_MIN = 10  # by voice: still loud enough to hear the answer
ACK_SECONDS = 2.0


def volume_target(current, *, change=None, level=None):
    """The new volume, kept within VOLUME_MIN..100."""
    if (change is None) == (level is None):
        raise ValueError("expected change or level")
    if change is not None:
        if change not in {"up", "down"}:
            raise ValueError("invalid volume change")
        target = current + (VOLUME_STEP if change == "up" else -VOLUME_STEP)
    else:
        if type(level) is not int or not 0 <= level <= 100:
            raise ValueError("invalid volume level")
        target = level
    return max(VOLUME_MIN, min(100, target))


class SpeakerVolume:
    def __init__(self, devices, publisher, *, ack_seconds=ACK_SECONDS):
        self.devices = devices  # RoomAudioDevices of this Room
        self.publisher = publisher  # reb_voice
        self.ack_seconds = ack_seconds

    def current(self):
        return self.devices.speaker_volume()

    async def set(self, volume):
        request_id = uuid.uuid4().hex[:16]
        await self.publisher.publish({
            "type": "speaker.volume.set", "description": "调整扬声器音量",
            "data": {"protocol_version": 1, "sender": "brain", "volume": volume,
                     "request_id": request_id},
        })
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.ack_seconds
        while loop.time() < deadline:
            confirmed = self.devices.volume_acks.get(request_id)
            if confirmed is not None:
                return {"status": "set", "volume": confirmed}
            await asyncio.sleep(0.05)
        return {"status": "unconfirmed", "volume": volume}

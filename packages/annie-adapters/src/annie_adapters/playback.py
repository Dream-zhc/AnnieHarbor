"""Room playback receipt and mouth event contracts; no inference from PCM sends."""

import asyncio
import json
import logging

from websockets.asyncio.client import connect

from annie_adapters import parse_room_welcome, parse_subscribe_ack

logger = logging.getLogger(__name__)
PLAYBACK_EVENTS = frozenset({
    "tts.playback.started", "tts.playback.progress",
    "tts.playback.stopped", "tts.playback.drained",
    "tts.playback.held", "tts.playback.resumed",
})


def playback_receipt(message):
    """Validate a speaker/browser-reported sample cursor, never infer it locally."""
    if not isinstance(message, dict) or message.get("type") not in PLAYBACK_EVENTS:
        raise ValueError("unsupported playback event")
    data = message.get("data")
    if not isinstance(data, dict):
        raise ValueError("playback event requires data")
    if set(data) - {
        "protocol_version", "conversation_id", "stream_epoch", "played_sample_count",
        "sample_rate", "seq", "source", "playback_id", "reason",
        "command_seq",
    }:
        raise ValueError("unknown playback event fields")
    if type(data.get("protocol_version")) is not int or data["protocol_version"] != 1:
        raise ValueError("unsupported playback protocol_version")
    if not isinstance(data.get("conversation_id"), str) or not data["conversation_id"]:
        raise ValueError("conversation_id required")
    if type(data.get("stream_epoch")) is not int or data["stream_epoch"] < 1:
        raise ValueError("stream_epoch must be positive integer")
    if type(data.get("played_sample_count")) is not int or data["played_sample_count"] < 0:
        raise ValueError("played_sample_count must be nonnegative integer")
    if type(data.get("sample_rate")) is not int or data["sample_rate"] != 24000:
        raise ValueError("playback sample_rate must be 24000")
    if type(data.get("seq")) is not int or data["seq"] < 1:
        raise ValueError("playback seq must be positive integer")
    if data.get("source") not in {"browser_test", "speaker"}:
        raise ValueError("source must be speaker or browser_test")
    if not isinstance(data.get("playback_id"), str) or not 1 <= len(data["playback_id"]) <= 128:
        raise ValueError("playback_id required")
    if message["type"] in {"tts.playback.held", "tts.playback.resumed"}:
        if data["source"] != "speaker":
            raise ValueError("only the physical speaker may acknowledge hold/resume")
        if type(data.get("command_seq")) is not int or data["command_seq"] < 1:
            raise ValueError("hold/resume requires command_seq")
    return message


class RoomEventListener:
    """Subscribe to one existing Room channel; a disconnect is not a stop receipt."""

    def __init__(self, config, room_id, channel, on_message, socket_factory=connect):
        self.config = config
        self.room_id = room_id
        self.channel = channel
        self.url = config.url_for(room_id, channel)
        self.on_message = on_message
        self.socket_factory = socket_factory
        self.task = None
        self.socket = None
        self.connected = False
        self.last_error = None
        self._stopped = False

    def start(self):
        if self.task is None or self.task.done():
            self._stopped = False
            self.task = asyncio.create_task(self._run(), name=f"room-events:{self.room_id}:{self.channel}")

    async def close(self):
        self._stopped = True
        if self.task is not None:
            self.task.cancel()
        if self.socket is not None:
            self.socket.transport.abort()
        if self.task is not None:
            await asyncio.gather(self.task, return_exceptions=True)
        self.socket = self.task = None
        self.connected = False

    async def _run(self):
        delay = self.config.reconnect_min_sec
        while not self._stopped:
            try:
                async with self.socket_factory(
                    self.url, open_timeout=self.config.connect_timeout_sec,
                    close_timeout=self.config.connect_timeout_sec,
                ) as ws:
                    self.socket = ws
                    async with asyncio.timeout(self.config.connect_timeout_sec):
                        welcome = parse_room_welcome(json.loads(await ws.recv()))
                        if welcome.room != self.room_id or welcome.robot_id != self.config.channels[self.channel]:
                            raise ValueError("Room event listener WELCOME identity mismatch")
                        await ws.send(json.dumps({"type": "SUBSCRIBE", "data": {"types": ["*"]}}))
                        while True:
                            message = json.loads(await ws.recv())
                            if message.get("type") == "ACK":
                                ack = parse_subscribe_ack(message)
                                if "*" not in ack.subscriptions:
                                    raise ValueError("Room event listener ACK missing wildcard")
                                break
                    self.connected = True
                    self.last_error = None
                    delay = self.config.reconnect_min_sec
                    async for raw in ws:
                        if not isinstance(raw, str):
                            continue
                        try:
                            message = json.loads(raw)
                            if isinstance(message, dict):
                                self.on_message(message)
                        except (ValueError, TypeError) as exc:
                            logger.warning("Room event rejected room=%s channel=%s: %s",
                                           self.room_id, self.channel, type(exc).__name__)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.last_error = f"{type(exc).__name__}: {str(exc)[:120]}"
                logger.warning("Room listener room=%s channel=%s: %s",
                               self.room_id, self.channel, self.last_error)
            finally:
                self.socket = None
                self.connected = False
            if not self._stopped:
                await asyncio.sleep(delay)
                delay = min(self.config.reconnect_max_sec, delay * 2)

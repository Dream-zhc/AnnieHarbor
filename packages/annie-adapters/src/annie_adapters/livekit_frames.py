"""Newest camera frame from a robot's LiveKit room (2026-11-02).

The main brain joins the robot's LiveKit room as a hidden, subscribe-only
participant and subscribes to one video track only: the configured source
participant's camera (the same identity the exhibit display shows). It keeps
just the newest decoded frame and encodes a small JPEG only when asked, so an
idle room costs no image work. Room, source identity and the API key/secret
are explicit configuration; nothing is guessed from the robot id.

A dropped connection only means no picture until the next reconnect: callers
must treat ``latest_jpeg()`` returning None as "no frame" and carry on.
"""

import asyncio
import base64
import hashlib
import hmac
import io
import json
import logging
import secrets
import time
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

TOKEN_TTL_SECONDS = 6 * 3600  # used once to join; the server refreshes a live session
RETRY_SECONDS = 5.0
CONNECT_TIMEOUT_SECONDS = 10.0


def signed_token(key, secret, identity, room, *, ttl=TOKEN_TTL_SECONDS):
    """A LiveKit access token (HS256 JWT) that may only watch this room."""
    now = int(time.time())
    claims = {"iss": key, "sub": identity, "iat": now, "nbf": now, "exp": now + ttl,
              "video": {"roomJoin": True, "room": room, "canPublish": False,
                        "canSubscribe": True, "canPublishData": False, "hidden": True}}

    def encoded(obj):
        data = json.dumps(obj, separators=(",", ":"), ensure_ascii=False).encode()
        return base64.urlsafe_b64encode(data).rstrip(b"=")

    signing = encoded({"alg": "HS256", "typ": "JWT"}) + b"." + encoded(claims)
    signature = base64.urlsafe_b64encode(
        hmac.new(secret.encode(), signing, hashlib.sha256).digest()).rstrip(b"=")
    return (signing + b"." + signature).decode()


def validate_url(url):
    parsed = urlparse(str(url or "").strip())
    if parsed.scheme not in {"ws", "wss"} or not parsed.hostname:
        raise ValueError("LiveKit url must be ws(s)://host[:port]")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("LiveKit url must not contain credentials, query or fragment")
    return parsed.geturl()


def encode_jpeg(rgb, width, height, *, max_side, quality):
    """JPEG bytes of an RGB24 buffer, scaled so its longer side is at most max_side."""
    from PIL import Image

    image = Image.frombytes("RGB", (width, height), bytes(rgb))
    longest = max(width, height)
    if longest > max_side:
        scale = max_side / longest
        image = image.resize((max(1, round(width * scale)), max(1, round(height * scale))),
                             Image.Resampling.BILINEAR)
    buffer = io.BytesIO()
    image.save(buffer, "JPEG", quality=quality)
    return buffer.getvalue()


class LiveKitFrames:
    """One robot's camera: connect, keep reconnecting, hold the newest frame."""

    def __init__(self, url, api_key, api_secret, room, source, *, label="", max_side=448,
                 jpeg_quality=72, max_age_ms=3000, retry_seconds=RETRY_SECONDS, rtc=None):
        if not (api_key and api_secret and room and source):
            raise ValueError("LiveKit frames need api key, secret, room and source identity")
        if type(max_side) is not int or not 64 <= max_side <= 2048:
            raise ValueError("LiveKit frame max_side must be 64-2048")
        if type(jpeg_quality) is not int or not 30 <= jpeg_quality <= 95:
            raise ValueError("LiveKit frame jpeg_quality must be 30-95")
        if type(max_age_ms) is not int or not 100 <= max_age_ms <= 60000:
            raise ValueError("LiveKit frame max_age_ms must be 100-60000")
        self.url = validate_url(url)
        self.api_key, self.api_secret = api_key, api_secret
        self.room_name, self.source = room, source
        self.label = label or room
        self.max_side, self.jpeg_quality, self.max_age_ms = max_side, jpeg_quality, max_age_ms
        self.retry_seconds = retry_seconds
        self._rtc = rtc  # the livekit.rtc module; injectable for tests
        self.status = "idle"  # idle -> connecting -> connected / waiting_track -> down
        self.error = None
        self.connects = 0
        self.frames = 0
        self._frame = None  # (VideoFrame, monotonic time)
        self._room = None
        self._reader = None
        self._task = None
        self._closed = False

    # ---- lifecycle ----------------------------------------------------------------
    def start(self):
        if self._task is None:
            self._task = asyncio.create_task(self._run(), name=f"livekit-frames:{self.label}")

    async def _run(self):
        while not self._closed:
            disconnected = asyncio.Event()
            try:
                await self._connect(disconnected)
                await disconnected.wait()
                self._mark("down", "disconnected")
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._mark("down", type(exc).__name__)
            finally:
                await self._teardown()
            await asyncio.sleep(self.retry_seconds)

    async def _connect(self, disconnected):
        rtc = self._rtc
        if rtc is None:
            from livekit import rtc
            self._rtc = rtc
        self._mark("connecting", None)
        room = rtc.Room()
        self._room = room

        def subscribe(publication, participant):
            if (participant.identity == self.source
                    and publication.kind == rtc.TrackKind.KIND_VIDEO
                    and not publication.subscribed):
                publication.set_subscribed(True)

        def on_subscribed(track, _publication, participant):
            if participant.identity == self.source and track.kind == rtc.TrackKind.KIND_VIDEO:
                self._watch(track)

        def on_unsubscribed(track, _publication, participant):
            if participant.identity == self.source and track.kind == rtc.TrackKind.KIND_VIDEO:
                self._stop_reader()
                self._mark("waiting_track", None)

        room.on("track_published", subscribe)
        room.on("track_subscribed", on_subscribed)
        room.on("track_unsubscribed", on_unsubscribed)
        room.on("disconnected", lambda *_args: disconnected.set())
        identity = f"annie-brain-{self.label}-{secrets.token_hex(4)}"
        token = signed_token(self.api_key, self.api_secret, identity, self.room_name)
        await asyncio.wait_for(
            room.connect(self.url, token, rtc.RoomOptions(auto_subscribe=False)),
            CONNECT_TIMEOUT_SECONDS)
        self.connects += 1
        self._mark("waiting_track", None)
        for participant in list(room.remote_participants.values()):
            for publication in list(participant.track_publications.values()):
                subscribe(publication, participant)

    def _watch(self, track):
        self._stop_reader()
        stream = self._rtc.VideoStream(track, capacity=1)  # only the newest frame waits
        self._reader = (stream, asyncio.create_task(self._read(stream),
                                                    name=f"livekit-video:{self.label}"))
        self._mark("connected", None)

    async def _read(self, stream):
        try:
            async for event in stream:
                self._frame = (event.frame, time.monotonic())
                self.frames += 1
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("livekit_frames_read_error room=%s error=%s", self.label,
                           type(exc).__name__)

    def _stop_reader(self):
        reader, self._reader = self._reader, None
        if reader is not None:
            stream, task = reader
            task.cancel()
            asyncio.ensure_future(stream.aclose())

    async def _teardown(self):
        self._stop_reader()
        room, self._room = self._room, None
        if room is not None:
            try:
                await asyncio.wait_for(room.disconnect(), 3)
            except Exception:
                pass

    def _mark(self, status, error):
        if status != self.status:
            logger.info("livekit_frames status=%s robot=%s room=%s source=%s error=%s", status,
                        self.label, self.room_name, self.source, error)
        self.status, self.error = status, error

    async def close(self):
        self._closed = True
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None
        await self._teardown()

    # ---- frames -------------------------------------------------------------------
    def frame_age_ms(self):
        if self._frame is None:
            return None
        return round((time.monotonic() - self._frame[1]) * 1000)

    async def latest_jpeg(self):
        """(JPEG bytes, age in ms) of the newest frame, or None if none is fresh enough.
        Scaling and encoding a 1080p frame runs off the event loop."""
        current = self._frame
        if current is None:
            return None
        frame, at = current
        age_ms = round((time.monotonic() - at) * 1000)
        if age_ms > self.max_age_ms:
            return None
        rgb = frame.convert(self._rtc.VideoBufferType.RGB24)
        data = await asyncio.to_thread(encode_jpeg, rgb.data, rgb.width, rgb.height,
                                       max_side=self.max_side, quality=self.jpeg_quality)
        return data, age_ms

    def snapshot(self):
        return {"status": self.status, "error": self.error, "room": self.room_name,
                "source": self.source, "connects": self.connects, "frames": self.frames,
                "frame_age_ms": self.frame_age_ms(), "max_age_ms": self.max_age_ms}

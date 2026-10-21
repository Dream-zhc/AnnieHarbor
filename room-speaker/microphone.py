"""Room microphone: send the board microphone to reb_mic and report it on reb_voice.

Each frame is a Room BinaryFrame of type MIC_AUDIO carrying 20 ms of 16 kHz
mono S16LE. This side only captures, converts and reports what it did.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import socket
import threading
import time
import uuid
from collections import deque

import numpy as np

MIC_RATE = 16000
FRAME_SAMPLES = 320  # 20 ms
FRAME_BYTES = FRAME_SAMPLES * 2
FRAME_TYPE = b"MIC_AUDIO"
QUEUE_FRAMES = 25  # 500 ms; older audio is no use for live listening
MAX_FRAME_AGE = 0.5
logger = logging.getLogger("speaker.mic")


class DeviceUnavailable(Exception):
    """A configured sound card is missing, ambiguous or busy: wait for it."""


class DeviceLost(Exception):
    """A sound card stopped delivering callbacks: restart with a fresh device list."""


def encode_frame(pcm: bytes) -> bytes:
    """Room BinaryFrame: two-byte big-endian type length, type, payload."""
    if len(pcm) != FRAME_BYTES:
        raise ValueError("MIC_AUDIO 每帧必须是 640 字节")
    return len(FRAME_TYPE).to_bytes(2, "big") + FRAME_TYPE + pcm


def _lowpass(rate, taps=63):
    """Windowed-sinc low-pass below the 8 kHz Nyquist of the 16 kHz output."""
    cutoff = 7400 / rate
    n = np.arange(taps) - (taps - 1) / 2
    kernel = np.sinc(2 * cutoff * n) * np.hamming(taps)
    return (kernel / kernel.sum()).astype(np.float64)


class Resampler:
    """Any device rate >= 16 kHz to 16 kHz, continuous across callback blocks."""

    def __init__(self, rate):
        if type(rate) is not int or not MIC_RATE <= rate <= 192000:
            raise ValueError("麦克风采样率需要在 16000 到 192000 Hz 之间")
        self.rate = rate
        self.step = rate / MIC_RATE
        self.taps = None if rate == MIC_RATE else _lowpass(rate)
        self.history = np.zeros(0 if self.taps is None else len(self.taps) - 1)
        self.carry = np.zeros(0)
        self.position = 0.0

    def process(self, block: np.ndarray) -> np.ndarray:
        """Mono float samples in [-1, 1] -> int16 at 16 kHz."""
        block = np.asarray(block, dtype=np.float64)
        if self.taps is None:
            out = block
        else:
            joined = np.concatenate([self.history, block])
            filtered = np.convolve(joined, self.taps, mode="valid")
            self.history = joined[len(joined) - (len(self.taps) - 1):]
            buffer = np.concatenate([self.carry, filtered])
            if len(buffer) < 2:
                self.carry = buffer
                return np.zeros(0, dtype=np.int16)
            points = np.arange(self.position, len(buffer) - 1, self.step)
            index = points.astype(np.int64)
            fraction = points - index
            out = buffer[index] + (buffer[index + 1] - buffer[index]) * fraction
            self.position = (points[-1] + self.step if len(points) else self.position) \
                - (len(buffer) - 1)
            self.carry = buffer[-1:]
        return (np.clip(out, -1.0, 1.0) * 32767).astype(np.int16)


class MicCapture:
    """Owned by the PortAudio thread on feed(), by asyncio on take()/report()."""

    def __init__(self, rate, *, channel=0, queue_frames=QUEUE_FRAMES, clock=time.monotonic):
        if type(channel) is not int or channel < 0:
            raise ValueError("麦克风声道编号无效")
        self.resampler = Resampler(rate)
        self.channel = channel
        self.clock = clock
        self.lock = threading.Lock()
        self.frames = deque()
        self.queue_frames = queue_frames
        self.pending = np.zeros(0, dtype=np.int16)
        self.captured_samples = 0
        self.produced_frames = 0
        self.sent_frames = 0
        self.dropped_frames = 0
        self.overflows = 0
        self.faults = 0
        self.peak = 0

    def feed(self, block, *, overflow=False):
        """block: frames x channels float32 from the input callback."""
        data = np.asarray(block)
        mono = data[:, min(self.channel, data.shape[1] - 1)] if data.ndim == 2 else data
        samples = self.resampler.process(mono)
        now = self.clock()
        with self.lock:
            if overflow:
                self.overflows += 1
            if len(samples):
                self.peak = max(self.peak, int(np.abs(samples.astype(np.int32)).max()))
                self.captured_samples += len(samples)
            pending = np.concatenate([self.pending, samples])
            whole = len(pending) // FRAME_SAMPLES
            for index in range(whole):
                chunk = pending[index * FRAME_SAMPLES:(index + 1) * FRAME_SAMPLES]
                self.frames.append((now, chunk.astype("<i2").tobytes()))
                self.produced_frames += 1
                if len(self.frames) > self.queue_frames:
                    self.frames.popleft()  # keep the freshest audio
                    self.dropped_frames += 1
            self.pending = pending[whole * FRAME_SAMPLES:]

    def take(self):
        """Frames still fresh enough to matter; stale ones are dropped, not sent late."""
        now = self.clock()
        with self.lock:
            fresh = []
            while self.frames:
                at, pcm = self.frames.popleft()
                if now - at > MAX_FRAME_AGE:
                    self.dropped_frames += 1
                    continue
                fresh.append(pcm)
            return fresh

    def discard(self):
        """While reb_mic is down, audio is not buffered for later."""
        with self.lock:
            self.dropped_frames += len(self.frames)
            self.frames.clear()

    def report(self):
        with self.lock:
            peak, self.peak = self.peak, 0
            return {
                "captured_sample_count": self.captured_samples,
                "produced_frames": self.produced_frames,
                "sent_frames": self.sent_frames,
                "dropped_frames": self.dropped_frames,
                "overflows": self.overflows,
                "faults": self.faults,
                "peak_dbfs": round(20 * math.log10(peak / 32768), 1) if peak else None,
            }


def build_mic_receipt(kind, *, identity, seq, counters=None, room_connected=None, reason=None):
    descriptions = {"started": "麦克风开始采集", "progress": "麦克风采集进度",
                    "stopped": "麦克风停止采集"}
    data = {"protocol_version": 1, "sender": "microphone", "source": "microphone",
            **identity, "sample_rate": MIC_RATE, "seq": seq}
    if counters:
        data.update(counters)
    if room_connected is not None:
        data["room_connected"] = bool(room_connected)
    if reason:
        data["reason"] = reason
    return {"type": "mic.capture." + kind, "description": descriptions[kind],
            "ts": time.time_ns() // 1_000_000, "data": data}


def device_identity(role, device_name):
    """Display names plus a per-process instance id; never a stable user identity."""
    return {"instance_id": f"{role}-{uuid.uuid4().hex[:12]}", "host": socket.gethostname()[:64],
            "device_name": str(device_name)[:80]}


async def wait_welcome(ws, room, robot_id, timeout):
    deadline = time.monotonic() + timeout
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("reb_mic WELCOME 超时")
        raw = await asyncio.wait_for(ws.recv(), remaining)
        if not isinstance(raw, str):
            continue
        message = json.loads(raw)
        if not isinstance(message, dict) or message.get("type") != "WELCOME":
            continue
        if message.get("ok") is not True or message.get("room") != room \
                or message.get("robotId") != robot_id:
            raise ValueError(f"reb_mic WELCOME 身份不符，期望 {room}/{robot_id}")
        return message


class MicLink:
    def __init__(self):
        self.connected = False


async def run_mic_channel(url, room, capture: MicCapture, link: MicLink, *, connect=None):
    """Publish MIC_AUDIO only; never SUBSCRIBE to reb_mic."""
    if connect is None:
        from websockets.asyncio.client import connect
    delay = 1.0
    while True:
        try:
            async with connect(url, open_timeout=3, close_timeout=3, proxy=None,
                               max_size=2 ** 20) as ws:
                await wait_welcome(ws, room, "reb_mic", 3)
                capture.discard()  # audio from before the connection is stale
                link.connected = True
                logger.info("麦克风已连接 %s", url)
                delay = 1.0
                reader = asyncio.create_task(_drain(ws))
                try:
                    while not reader.done():
                        frames = capture.take()
                        for pcm in frames:
                            await ws.send(encode_frame(pcm))
                        with capture.lock:
                            capture.sent_frames += len(frames)
                        await asyncio.sleep(0.01)
                    reader.result()
                finally:
                    reader.cancel()
                    await asyncio.gather(reader, return_exceptions=True)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("reb_mic 连接失败：%s", exc)
        finally:
            link.connected = False
            capture.discard()
        await asyncio.sleep(delay)
        delay = min(15.0, delay * 2)


async def _drain(ws):
    """Room may push PING/PRESENCE; read them so the socket never backs up."""
    async for raw in ws:
        if isinstance(raw, str) and '"PRESENCE"' in raw:
            logger.debug("reb_mic PRESENCE %s", raw[:200])


NO_DATA_REPORTS = 5  # seconds without any callback: the card is gone
ZERO_NOTE_REPORTS = 60  # seconds of exact zero before a note in the log


def silence_check(counters, state):
    """"no_data" when the device delivers nothing at all. Zeros are only noted.

    USB microphones with an on-device AGC/noise gate (ALSA 'Auto Gain Control')
    output exact zero for seconds at a time in a quiet room, so zeros alone do
    not mean a broken device.
    """
    captured = counters["captured_sample_count"]
    if captured == state.get("captured"):
        kind = "no_data"
    elif counters["peak_dbfs"] is None:
        kind = "digital_silence"
    else:
        kind = None
    state["captured"] = captured
    state["count"] = state.get("count", 0) + 1 if kind and kind == state.get("kind") else (1 if kind else 0)
    state["kind"] = kind
    count = state["count"]
    if kind == "no_data" and count == NO_DATA_REPORTS:
        logger.warning("麦克风连续 %s 秒没有收到声卡数据", count)
        return kind
    if kind == "digital_silence" and (count == ZERO_NOTE_REPORTS or count % 600 == 0):
        logger.info("麦克风已连续 %s 秒全零。设备自带降噪/自动增益在安静时会这样；"
                    "有人说话仍全零再检查接线、静音键或 amixer 的 Auto Gain Control", count)
    return None


async def report_mic(capture: MicCapture, link: MicLink, voice, identity, *, interval=1.0,
                     watch=False):
    """started once reb_voice is up, then progress every second.

    With watch, 5 s without any data raises DeviceLost. Reopening a vanished
    USB card in-process can abort inside PortAudio, so the program exits and
    starts again with a fresh device list instead.
    """
    seq = 0
    started = False
    silence = {}
    while True:
        counters = capture.report()
        if silence_check(counters, silence) and watch:
            raise DeviceLost("麦克风没有数据，声卡可能已断开")
        if voice.ready:
            seq += 1
            kind = "progress" if started else "started"
            message = build_mic_receipt(kind, identity=identity, seq=seq,
                                        counters=counters, room_connected=link.connected)
            if await voice.send(json.dumps(message, ensure_ascii=False)):
                started = True
            else:
                seq -= 1
        await asyncio.sleep(interval)


async def send_mic_stopped(voice, identity, capture, reason="exit"):
    if not voice.ready:
        return
    message = build_mic_receipt("stopped", identity=identity, seq=1 << 30,
                                counters=capture.report(), reason=reason)
    await voice.send(json.dumps(message, ensure_ascii=False))


def _open_stream(sd, device, rate, count, capture):
    def callback(indata, _frames, _time_info, status):
        try:
            capture.feed(indata, overflow=bool(status and status.input_overflow))
        except Exception:
            capture.faults += 1

    stream = sd.InputStream(samplerate=rate, channels=count, dtype="float32",
                            callback=callback, device=device,
                            blocksize=max(1, rate // 50), latency="low")
    try:
        stream.start()
    except Exception:
        stream.close()
        raise
    return stream


def start_input_stream(capture_factory, device, sd):
    """Open the microphone at 16 kHz, else at its own rate with resampling."""
    try:
        info = sd.query_devices(device, "input")
    except Exception as exc:
        raise DeviceUnavailable(f"麦克风不可用：{exc}") from exc
    native = int(round(float(info["default_samplerate"])))
    channels = max(1, int(info["max_input_channels"]))
    errors = []
    for rate, count in ((MIC_RATE, 1), (native, 1), (native, channels)):
        if rate < MIC_RATE:
            continue
        capture = capture_factory(rate)
        try:
            stream = _open_stream(sd, device, rate, count, capture)
            return stream, capture, rate, count, str(info["name"])
        except Exception as exc:
            errors.append(f"{rate} Hz × {count}：{exc}")
    raise DeviceUnavailable("无法打开麦克风：" + "；".join(errors))


class MicInput:
    """The PortAudio input stream with the rate and channels it was opened at."""

    def __init__(self, sd, device, capture_factory):
        self.stream, self.capture, self.rate, self.channels, self.name = start_input_stream(
            capture_factory, device, sd)

    def stop(self):
        if self.stream is not None:
            self.stream.stop()

    def close(self):
        if self.stream is not None:
            self.stream.close()

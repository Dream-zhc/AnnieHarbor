"""Room speaker (and optional microphone) for one robot Room.

Speaker: play reb_tts and report source=speaker on reb_voice. With --mic the
same process also sends the board microphone to reb_mic (microphone.py).
Cloud AEC stays in Brain. This process only plays, captures and reports what
the audio callbacks really did. One Room has one speaker and one microphone:
do not also run the browser audition or the web microphone. With --head the
program names its robot head, and of two heads in one Room the one started
later takes over (head.py).
"""

from __future__ import annotations

import argparse
import array
import asyncio
import base64
import importlib
import json
import logging
import os
import queue
import re
import signal
import sys
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass

SRC_RATE = 24000
MAX_BUFFER_SECONDS = 30
logger = logging.getLogger("speaker")


@dataclass(frozen=True)
class TtsAudio:
    conversation_id: str
    stream_epoch: int
    seq: int
    start_sample: int
    samples: array.array
    stream_final: bool


@dataclass(frozen=True)
class TtsEnd:
    conversation_id: str
    stream_epoch: int
    seq: int
    end_sample: int


@dataclass(frozen=True)
class TtsStop:
    conversation_id: str
    stream_epoch: int
    seq: int
    reason: str | None


@dataclass(frozen=True)
class PlaybackControl:
    kind: str
    conversation_id: str
    stream_epoch: int
    command_seq: int | None
    fade_ms: int | None
    reason: str | None


def decode_pcm(payload: bytes) -> array.array:
    samples = array.array("h")
    samples.frombytes(payload)
    if sys.byteorder != "little":
        samples.byteswap()
    return samples


def _positive_int(value, label):
    if type(value) is not int or value < 1:
        raise ValueError(f"{label} 必须是正整数")
    return value


def _sample_cursor(data):
    start = data.get("start_sample")
    end = data.get("end_sample")
    if type(start) is not int or type(end) is not int or start < 0 or end < start:
        raise ValueError("TTS 游标无效")
    return start, end


def _identity(data):
    conversation_id = data.get("conversation_id")
    if not isinstance(conversation_id, str) or not conversation_id:
        raise ValueError("conversation_id 无效")
    epoch = _positive_int(data.get("stream_epoch"), "stream_epoch")
    seq = _positive_int(data.get("seq"), "seq")
    return conversation_id, epoch, seq


def parse_tts(message):
    """Return an audio, end, stop event, or None for an empty segment boundary."""
    if not isinstance(message, dict) or message.get("type") != "tts":
        return None
    data = message.get("data")
    if not isinstance(data, dict):
        raise ValueError("TTS 缺少 data")
    conversation_id, epoch, seq = _identity(data)
    start, end = _sample_cursor(data)
    reason = data.get("reason") if isinstance(data.get("reason"), str) else None
    if data.get("stop") is True:
        return TtsStop(conversation_id, epoch, seq, reason)
    audio = data.get("audio") or ""
    stream_final = data.get("stream_final") is True
    if not audio:
        if stream_final:
            return TtsEnd(conversation_id, epoch, seq, end)
        return None
    if (data.get("format") != "pcm_s16le" or data.get("sample_rate") != SRC_RATE
            or data.get("channels") != 1):
        raise ValueError("TTS PCM 不是 24kHz/mono/S16LE")
    if not isinstance(audio, str):
        raise ValueError("TTS PCM 不是 Base64")
    try:
        payload = base64.b64decode(audio, validate=True)
    except Exception as exc:
        raise ValueError("TTS PCM Base64 无效") from exc
    if not payload or len(payload) % 2 or len(payload) > SRC_RATE * 2:
        raise ValueError("TTS PCM 长度无效")
    if len(payload) // 2 != end - start:
        raise ValueError("TTS PCM 长度与游标不符")
    return TtsAudio(conversation_id, epoch, seq, start, decode_pcm(payload), stream_final)


def parse_control(message):
    if not isinstance(message, dict):
        return None
    kind = str(message.get("type") or "")
    if kind not in {"tts.playback.hold", "tts.playback.resume", "tts.playback.stop"}:
        return None
    data = message.get("data")
    if not isinstance(data, dict):
        raise ValueError("播放控制缺少 data")
    if data.get("sender") != "brain":
        return None
    if data.get("protocol_version") != 1:
        raise ValueError("播放控制 protocol_version 无效")
    conversation_id = data.get("conversation_id")
    if not isinstance(conversation_id, str) or not conversation_id:
        raise ValueError("播放控制 conversation_id 无效")
    epoch = _positive_int(data.get("stream_epoch"), "stream_epoch")
    action = kind.removeprefix("tts.playback.")
    command_seq = None
    fade_ms = None
    if action in {"hold", "resume"}:
        command_seq = _positive_int(data.get("command_seq"), "command_seq")
        if "fade_ms" in data and data.get("fade_ms") is not None:
            fade_ms = data.get("fade_ms")
            if type(fade_ms) is not int or not 0 <= fade_ms <= 500:
                raise ValueError("fade_ms 无效")
    reason = data.get("reason") if isinstance(data.get("reason"), str) else None
    return PlaybackControl(action, conversation_id, epoch, command_seq, fade_ms, reason)


def validate_welcome(message, room, robot_id):
    if not isinstance(message, dict) or message.get("type") != "WELCOME" or message.get("ok") is not True:
        raise ValueError("WELCOME 无效")
    if message.get("room") != room or message.get("robotId") != robot_id:
        raise ValueError(f"WELCOME 身份不符，期望 {room}/{robot_id}")
    if not isinstance(message.get("clientId"), str) or not message["clientId"]:
        raise ValueError("WELCOME 缺少 clientId")
    if type(message.get("size")) is not int or message["size"] < 1:
        raise ValueError("WELCOME size 无效")
    subscriptions = message.get("subscriptions")
    if not isinstance(subscriptions, list) or not all(
        isinstance(item, str) and item for item in subscriptions
    ):
        raise ValueError("WELCOME subscriptions 无效")
    catalog = message.get("catalog")
    if not isinstance(catalog, list):
        raise ValueError("WELCOME catalog 无效")
    for entry in catalog:
        if not isinstance(entry, dict) or not isinstance(entry.get("type"), str) or not entry["type"]:
            raise ValueError("WELCOME catalog 无效")


def validate_subscribe_ack(message, required):
    if not isinstance(message, dict) or message.get("type") != "ACK":
        raise ValueError("缺少 ACK")
    data = message.get("data")
    if not isinstance(data, dict) or data.get("what") != "subscribed":
        raise ValueError("ACK 不是 subscribed")
    subscriptions = data.get("subscriptions")
    if not isinstance(subscriptions, list) or not all(
        isinstance(item, str) and item for item in subscriptions
    ):
        raise ValueError("ACK subscriptions 无效")
    have = set(subscriptions)
    if not set(required) <= have and "*" not in have:
        raise ValueError("ACK 未确认订阅")


def build_receipt(kind, *, conversation_id, stream_epoch, playback_id, played, seq,
                  command_seq=None, reason=None):
    descriptions = {
        "started": "扬声器开始播放",
        "progress": "扬声器已播放进度",
        "held": "扬声器已暂停",
        "resumed": "扬声器已恢复",
        "stopped": "扬声器已停止",
        "drained": "扬声器已播放完毕",
    }
    data = {
        "protocol_version": 1,
        "conversation_id": conversation_id,
        "stream_epoch": stream_epoch,
        "playback_id": playback_id,
        "source": "speaker",
        "sender": "speaker",
        "sample_rate": SRC_RATE,
        "seq": seq,
        "played_sample_count": int(played),
    }
    if command_seq is not None:
        data["command_seq"] = int(command_seq)
    if reason:
        data["reason"] = reason
    return {
        "type": "tts.playback." + kind,
        "description": descriptions[kind],
        "ts": time.time_ns() // 1_000_000,
        "data": data,
    }


class CommandGate:
    """Drop a control that reb_tts and reb_voice both deliver."""

    def __init__(self):
        self.seen = set()
        self.order = deque()

    def accept(self, control: PlaybackControl) -> bool:
        if control.kind == "stop":
            key = ("stop", control.conversation_id, control.stream_epoch)
        else:
            key = (control.kind, control.conversation_id, control.stream_epoch, control.command_seq)
        if key in self.seen:
            return False
        self.seen.add(key)
        self.order.append(key)
        if len(self.order) > 2000:
            self.seen.discard(self.order.popleft())
        return True


class Playback:
    """24 kHz source clock mapped onto the device callback. No network I/O."""

    def __init__(self, device_rate, *, prebuffer_ms=180, progress_ms=100):
        if type(device_rate) is not int or not 8000 <= device_rate <= 192000:
            raise ValueError("不支持的输出采样率")
        self.device_rate = device_rate
        self.prebuffer_samples = SRC_RATE * prebuffer_ms // 1000
        self.progress_every = max(1, device_rate * progress_ms // 1000)
        self.max_samples = SRC_RATE * MAX_BUFFER_SECONDS
        self.lock = threading.Lock()
        self.underruns = 0
        self.faults = 0
        self.port_status = ""
        self.volume = 100  # 0-100, set by voice through Brain; 100 = unchanged loudness
        self.volume_gain = 1.0
        self._clear_stream()
        self.blocked = set()
        self.block_order = deque()

    def _clear_stream(self):
        self.key = None
        self.playback_id = None
        self.buffer = array.array("h")
        self.origin = 0
        self.pos_num = 0
        self.pending = deque()
        self.total = None
        self.final = False
        self.incomplete = False
        self.audible = False
        self.last_seq = 0
        self.last_progress_played = -1
        self.frames_since_progress = 0
        self.gain = 1.0
        self.fade_kind = None
        self.fade_left = 0
        self.fade_total = 0
        self.hold_seq = None
        self.state = "idle"

    def push_audio(self, conversation_id, epoch, seq, start, samples, *, stream_final=False):
        with self.lock:
            return self._push_audio(
                conversation_id, epoch, seq, start, samples, stream_final=stream_final,
            )

    def mark_final(self, conversation_id, epoch, seq, end_sample):
        with self.lock:
            return self._mark_final(conversation_id, epoch, seq, end_sample)

    def stop(self, conversation_id, epoch, reason):
        with self.lock:
            return self._stop(conversation_id, epoch, reason)

    def hold(self, conversation_id, epoch, command_seq, fade_ms):
        with self.lock:
            return self._hold(conversation_id, epoch, command_seq, fade_ms)

    def resume(self, conversation_id, epoch, command_seq, fade_ms):
        with self.lock:
            return self._resume(conversation_id, epoch, command_seq, fade_ms)

    def pull(self, frames):
        with self.lock:
            return self._pull(frames)

    def set_volume(self, volume):
        """Software volume after the device mixer; square law, so 50 is about -12 dB."""
        if type(volume) is not int or not 0 <= volume <= 100:
            raise ValueError("音量需要是 0 到 100 的整数")
        with self.lock:
            self.volume = volume
            self.volume_gain = (volume / 100) ** 2

    def room_played(self):
        with self.lock:
            return self._room_played()

    def _block(self, key):
        if key in self.blocked:
            return
        self.blocked.add(key)
        self.block_order.append(key)
        while len(self.block_order) > 2000:
            self.blocked.discard(self.block_order.popleft())

    def _tail_end(self):
        if self.pending:
            start, samples = self.pending[-1]
            return start + len(samples)
        return self.origin + len(self.buffer)

    def _unconsumed(self):
        consumed = min(len(self.buffer), self.pos_num // self.device_rate)
        pending = sum(len(samples) for _, samples in self.pending)
        return max(0, len(self.buffer) - consumed) + pending

    def _room_played(self):
        if self.playback_id is None:
            return 0
        played = self.origin + self.pos_num // self.device_rate
        end = self.origin + len(self.buffer)
        return end if played > end else played

    def _identity_event(self, kind, played, *extra):
        return (kind, self.key[0], self.key[1], self.playback_id, int(played), *extra)

    def _begin(self, conversation_id, epoch):
        self.key = (conversation_id, epoch)
        self.playback_id = str(uuid.uuid4())
        self.buffer = array.array("h")
        self.origin = 0
        self.pos_num = 0
        self.pending = deque()
        self.total = None
        self.final = False
        self.incomplete = False
        self.audible = False
        self.last_seq = 0
        self.last_progress_played = -1
        self.frames_since_progress = 0
        self.gain = 1.0
        self.fade_kind = None
        self.fade_left = 0
        self.fade_total = 0
        self.hold_seq = None
        self.state = "prebuffer"

    def _stop(self, conversation_id, epoch, reason):
        key = (conversation_id, epoch)
        self._block(key)
        if self.key != key or self.playback_id is None:
            return []
        if self.state in {"idle", "done"}:
            return []
        played = self._room_played()
        event = self._identity_event("stopped", played, reason or "")
        self.state = "done"
        self.buffer = array.array("h")
        self.pending.clear()
        self.pos_num = 0
        self.fade_kind = None
        self.gain = 0.0
        return [event]

    def _push_audio(self, conversation_id, epoch, seq, start, samples, *, stream_final=False):
        key = (conversation_id, epoch)
        if key in self.blocked:
            return []
        events = []
        if self.key != key:
            if self.key is not None:
                events.extend(self._stop(self.key[0], self.key[1], "replaced"))
            if key in self.blocked:
                return events
            self._begin(conversation_id, epoch)
        if type(seq) is not int or seq <= self.last_seq or len(samples) == 0:
            return events
        self.last_seq = seq
        if start < self._tail_end():
            self.incomplete = True
            return events
        if self._unconsumed() + len(samples) > self.max_samples:
            events.extend(self._stop(conversation_id, epoch, "overflow"))
            return events
        if len(self.buffer) == 0 and not self.pending:
            self.origin = start
            if start != 0:
                self.incomplete = True
            self.buffer.extend(samples)
        elif start == self._tail_end():
            if self.pending:
                self.pending[-1][1].extend(samples)
            else:
                self.buffer.extend(samples)
        else:
            self.incomplete = True
            self.pending.append((start, array.array("h", samples)))
        if stream_final:
            self.final = True
            self.total = self._tail_end()
        if self.final and (self.origin != 0 or self.pending or self._tail_end() != self.total):
            self.incomplete = True
        return events

    def _mark_final(self, conversation_id, epoch, seq, end_sample):
        key = (conversation_id, epoch)
        if key in self.blocked or self.key != key:
            return []
        if type(seq) is not int or seq <= self.last_seq:
            return []
        self.last_seq = seq
        self.final = True
        self.total = end_sample
        if self.origin != 0 or self.pending or end_sample != self._tail_end():
            self.incomplete = True
        return []

    def _held_event(self):
        return self._identity_event("held", self._room_played(), self.hold_seq)

    def _start_fade(self, kind, fade_ms):
        frames = self.device_rate * max(0, fade_ms) // 1000
        if frames <= 0:
            self.fade_kind = None
            self.gain = 0.0 if kind == "down" else 1.0
            return False
        self.fade_kind = kind
        self.fade_total = frames
        self.fade_left = frames
        return True

    def _hold(self, conversation_id, epoch, command_seq, fade_ms):
        if self.key != (conversation_id, epoch) or self.state in {"idle", "done", "held"}:
            return []
        self.hold_seq = command_seq
        if self.state == "playing":
            if not self._start_fade("down", 40 if fade_ms is None else fade_ms):
                self.state = "held"
                self.gain = 0.0
                return [self._held_event()]
            return []
        self.state = "held"
        self.gain = 0.0
        return [self._held_event()]

    def _resume(self, conversation_id, epoch, command_seq, fade_ms):
        if self.key != (conversation_id, epoch):
            return []
        if self.state != "held" and self.fade_kind != "down":
            return []
        events = []
        if self.fade_kind == "down" or self.state != "held":
            if self.hold_seq is not None and self.fade_kind == "down":
                events.append(self._held_event())
            self.fade_kind = None
        fade = 120 if fade_ms is None else fade_ms
        self._start_fade("up", fade)
        if self.audible or self._ready():
            self.state = "playing"
        else:
            self.state = "prebuffer"
        events.append(self._identity_event("resumed", self._room_played(), command_seq))
        return events

    def _ready(self):
        have = self._unconsumed()
        return have > 0 and (self.final or have >= self.prebuffer_samples)

    def _peek(self):
        if not self.buffer:
            return None
        end_num = len(self.buffer) * self.device_rate
        if self.pos_num >= end_num:
            return None
        idx = self.pos_num // self.device_rate
        frac = self.pos_num % self.device_rate
        if idx >= len(self.buffer):
            return None
        first = int(self.buffer[idx])
        if frac == 0:
            return first
        if idx + 1 < len(self.buffer):
            second = int(self.buffer[idx + 1])
            return first + (second - first) * frac // self.device_rate
        if self.final or self.pending:
            return first
        return None

    def _commit(self):
        self.pos_num += SRC_RATE
        end_num = len(self.buffer) * self.device_rate
        if self.pos_num > end_num:
            self.pos_num = end_num

    def _switch_chunk(self):
        if not self.pending or self.pos_num < len(self.buffer) * self.device_rate:
            return False
        start, samples = self.pending.popleft()
        self.origin = start
        self.buffer = samples
        self.pos_num = 0
        return True

    def _maybe_finish(self, events):
        if self.state != "playing" or self.pending:
            return
        if not self.buffer or self.pos_num < len(self.buffer) * self.device_rate or not self.final:
            return
        if (self.origin == 0 and not self.incomplete and self.total
                and len(self.buffer) == self.total and self.total > 0):
            events.append(self._identity_event("drained", self.total))
        else:
            events.append(("incomplete", self.key[0], self.key[1], self._room_played()))
        self.state = "done"

    def _fade_step(self, events):
        if self.fade_kind is None:
            return self.gain
        self.fade_left -= 1
        if self.fade_kind == "down":
            gain = self.fade_left / self.fade_total
        else:
            gain = (self.fade_total - self.fade_left) / self.fade_total
        if self.fade_left <= 0:
            kind = self.fade_kind
            self.fade_kind = None
            if kind == "down":
                self.state = "held"
                self.gain = 0.0
                events.append(self._held_event())
            else:
                self.gain = 1.0
        return gain

    def _pull(self, frames):
        if type(frames) is not int or frames <= 0:
            return array.array("h"), []
        out = array.array("h", bytes(frames * 2))
        events = []
        if self.state in {"idle", "held", "done"}:
            return out, events
        if self.state == "prebuffer":
            if self.final and self._unconsumed() == 0:
                self.state = "done"
                return out, events
            if not self._ready():
                return out, events
            self.state = "playing"
        index = 0
        while index < frames and self.state == "playing":
            sample = self._peek()
            if sample is None:
                if self._switch_chunk():
                    continue
                starved = (
                    self.pos_num >= len(self.buffer) * self.device_rate
                    and not self.final and not self.pending
                )
                if starved:
                    self.underruns += 1
                break
            if not self.audible:
                events.append(self._identity_event("started", self._room_played()))
                self.audible = True
                self.last_progress_played = self._room_played()
            self._commit()
            gain = self._fade_step(events)
            mixed = int(sample * gain * self.volume_gain)
            if mixed > 32767:
                mixed = 32767
            elif mixed < -32768:
                mixed = -32768
            out[index] = mixed
            index += 1
        self.frames_since_progress += index
        if (self.state == "playing" and self.audible
                and self.frames_since_progress >= self.progress_every):
            played = self._room_played()
            if played > self.last_progress_played:
                events.append(self._identity_event("progress", played))
                self.last_progress_played = played
            self.frames_since_progress = 0
        self._maybe_finish(events)
        return out, events


def _sibling(name):
    """A module next to this file, also when this file is loaded by path in tests."""
    here = os.path.dirname(os.path.abspath(__file__))
    if here not in sys.path:
        sys.path.insert(0, here)
    return importlib.import_module(name)


def mic_module():
    return _sibling("microphone")


def head_module():
    return _sibling("head")


def build_speaker_presence(identity, playback, *, state=None):
    return {
        "type": "speaker.presence",
        "description": "扬声器在线",
        "ts": time.time_ns() // 1_000_000,
        "data": {
            "protocol_version": 1, "sender": "speaker", "source": "speaker", **identity,
            "state": state or playback.state, "underruns": playback.underruns,
            "faults": playback.faults, "volume": playback.volume,
        },
    }


async def report_presence(voice, identity, playback, *, interval=2.0):
    """Lets Brain see an idle speaker, and a second one in the same Room."""
    while True:
        if voice.ready:
            await voice.send(json.dumps(build_speaker_presence(identity, playback),
                                        ensure_ascii=False))
        await asyncio.sleep(interval)


def parse_volume(message):
    """speaker.volume.set from Brain: {"volume": 0-100, "request_id"}; None if not one."""
    if not isinstance(message, dict) or message.get("type") != "speaker.volume.set":
        return None
    data = message.get("data")
    if not isinstance(data, dict) or data.get("sender") != "brain":
        return None
    volume, request_id = data.get("volume"), data.get("request_id")
    if type(volume) is not int or not 0 <= volume <= 100:
        raise ValueError("音量无效")
    if not isinstance(request_id, str) or not 1 <= len(request_id) <= 64:
        raise ValueError("request_id 无效")
    return volume, request_id


class VolumeStore:
    """The last volume set by voice, kept across restarts in one small file."""

    def __init__(self, path):
        self.path = path

    def load(self, default):
        try:
            with open(self.path, encoding="utf-8") as handle:
                value = int(handle.read().strip())
        except (OSError, ValueError):
            return default
        return value if 0 <= value <= 100 else default

    def save(self, volume):
        try:
            with open(self.path, "w", encoding="utf-8") as handle:
                handle.write(f"{volume}\n")
        except OSError as exc:
            logger.warning("音量没有保存：%s", exc)


def build_volume_ack(identity, playback, request_id):
    return {"type": "speaker.volume", "description": "扬声器音量已调整",
            "ts": time.time_ns() // 1_000_000,
            "data": {"protocol_version": 1, "sender": "speaker", "source": "speaker",
                     **identity, "volume": playback.volume, "request_id": request_id}}


def handle_tts(playback: Playback, gate: CommandGate, message):
    parsed = parse_tts(message)
    if parsed is None:
        return []
    if isinstance(parsed, TtsStop):
        control = PlaybackControl(
            "stop", parsed.conversation_id, parsed.stream_epoch, None, None, parsed.reason,
        )
        if not gate.accept(control):
            return []
        return playback.stop(parsed.conversation_id, parsed.stream_epoch, parsed.reason)
    if isinstance(parsed, TtsEnd):
        return playback.mark_final(
            parsed.conversation_id, parsed.stream_epoch, parsed.seq, parsed.end_sample,
        )
    return playback.push_audio(
        parsed.conversation_id, parsed.stream_epoch, parsed.seq,
        parsed.start_sample, parsed.samples, stream_final=parsed.stream_final,
    )


def handle_control(playback: Playback, gate: CommandGate, message):
    if not isinstance(message, dict):
        return []
    kind = str(message.get("type") or "")
    if kind not in {"tts.playback.hold", "tts.playback.resume", "tts.playback.stop"}:
        return []
    parsed = parse_control(message)
    if parsed is None or not gate.accept(parsed):
        return []
    if parsed.kind == "hold":
        return playback.hold(
            parsed.conversation_id, parsed.stream_epoch, parsed.command_seq, parsed.fade_ms,
        )
    if parsed.kind == "resume":
        return playback.resume(
            parsed.conversation_id, parsed.stream_epoch, parsed.command_seq, parsed.fade_ms,
        )
    return playback.stop(parsed.conversation_id, parsed.stream_epoch, parsed.reason)


def enqueue(events, event):
    if event[0] == "progress" and events.qsize() >= 40:
        return
    events.put(event)


class VoiceLink:
    def __init__(self):
        self.socket = None
        self.ready = False

    def up(self, socket):
        self.socket = socket
        self.ready = True

    def down(self):
        self.ready = False
        self.socket = None

    async def send(self, payload):
        if not self.ready:
            return False
        socket = self.socket
        if socket is None:
            return False
        try:
            await socket.send(payload)
        except Exception:
            return False
        return True


async def read_json(ws, timeout):
    raw = await asyncio.wait_for(ws.recv(), timeout)
    if not isinstance(raw, str):
        raise ValueError("Room 控制消息不是文本")
    message = json.loads(raw)
    if not isinstance(message, dict):
        raise ValueError("Room 控制消息不是对象")
    return message


async def handshake(ws, room, robot_id, types, timeout):
    validate_welcome(await read_json(ws, timeout), room, robot_id)
    await ws.send(json.dumps({"type": "SUBSCRIBE", "data": {"types": list(types)}}))
    deadline = time.monotonic() + timeout
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("订阅确认超时")
        message = await read_json(ws, remaining)
        if message.get("type") == "ACK":
            validate_subscribe_ack(message, types)
            return


def next_seq(seqs, playback_id):
    if len(seqs) > 80:
        current = seqs.get(playback_id)
        seqs.clear()
        if current is not None:
            seqs[playback_id] = current
    value = seqs.get(playback_id, 0) + 1
    seqs[playback_id] = value
    return value


def _note_stream(playback, last_under, last_fault, last_under_log):
    now = time.monotonic()
    if playback.faults != last_fault:
        logger.warning("播放回调异常 %s 次", playback.faults)
        last_fault = playback.faults
    if playback.underruns != last_under and now - last_under_log >= 2:
        logger.warning("播放断粮 %s 次", playback.underruns)
        last_under = playback.underruns
        last_under_log = now
    if playback.port_status and now - last_under_log >= 2:
        logger.warning("声卡状态 %s", playback.port_status)
        playback.port_status = ""
        last_under_log = now
    return last_under, last_fault, last_under_log


async def report_receipts(events, voice: VoiceLink, playback: Playback):
    seqs = {}
    last_progress_log = 0.0
    last_under = 0
    last_fault = 0
    last_under_log = 0.0
    while True:
        last_under, last_fault, last_under_log = _note_stream(
            playback, last_under, last_fault, last_under_log,
        )
        try:
            event = events.get_nowait()
        except queue.Empty:
            await asyncio.sleep(0.02)
            continue
        kind = event[0]
        try:
            action, last_progress_log = await _emit_receipt(
                event, kind, seqs, voice, last_progress_log,
            )
        except Exception:
            logger.exception("回执没有发出")
            continue
        if action == "retry":
            with events.mutex:
                events.queue.appendleft(event)
            await asyncio.sleep(0.05)


async def _emit_receipt(event, kind, seqs, voice, last_progress_log):
    if kind == "incomplete":
        logger.warning("音频不完整，不上报 drained：%s epoch=%s 已播 %s", event[1], event[2], event[3])
        return "drop", last_progress_log
    if not voice.ready:
        return ("drop" if kind == "progress" else "retry"), last_progress_log
    conversation_id, epoch, playback_id, played = event[1], event[2], event[3], event[4]
    command_seq = event[5] if kind in {"held", "resumed"} else None
    reason = event[5] if kind == "stopped" and len(event) > 5 else None
    seq = next_seq(seqs, playback_id)
    message = build_receipt(
        kind, conversation_id=conversation_id, stream_epoch=epoch,
        playback_id=playback_id, played=played, seq=seq,
        command_seq=command_seq, reason=reason or None,
    )
    if not await voice.send(json.dumps(message, ensure_ascii=False)):
        seqs[playback_id] = seq - 1
        if seqs[playback_id] <= 0:
            seqs.pop(playback_id, None)
        return ("drop" if kind == "progress" else "retry"), last_progress_log
    now = time.monotonic()
    if kind == "progress":
        if now - last_progress_log >= 1:
            logger.info("播放中 %s 样本 %s", playback_id[:8], played)
            last_progress_log = now
    else:
        logger.info("%s %s epoch=%s 样本 %s", kind, conversation_id, epoch, played)
    return "sent", last_progress_log


async def run_channel(url, room, robot_id, types, on_message, events, voice=None):
    from websockets.asyncio.client import connect

    delay = 1.0
    while True:
        try:
            # LAN Room must not follow the Windows system proxy. proxy=True
            # sends 192.168.x through Clash/V2Ray and the upgrade never finishes.
            async with connect(url, open_timeout=3, close_timeout=3, proxy=None) as ws:
                await handshake(ws, room, robot_id, types, 3)
                if voice is not None:
                    voice.up(ws)
                logger.info("已订阅 %s", url)
                delay = 1.0
                async for raw in ws:
                    if not isinstance(raw, str):
                        continue
                    try:
                        message = json.loads(raw)
                    except json.JSONDecodeError:
                        logger.warning("忽略无法解析的 Room 消息")
                        continue
                    try:
                        produced = on_message(message)
                    except Exception as exc:
                        logger.warning("忽略无法处理的消息：%s", exc)
                        continue
                    for event in produced:
                        enqueue(events, event)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("%s 连接失败：%s", robot_id, exc)
        finally:
            if voice is not None:
                voice.down()
        await asyncio.sleep(delay)
        delay = min(15.0, delay * 2)


async def _flush(events, seconds=0.3):
    """Give report_receipts a moment to send a stop receipt already queued."""
    deadline = time.monotonic() + seconds
    while not events.empty() and time.monotonic() < deadline:
        await asyncio.sleep(0.02)


async def serve(tts_url, voice_url, room, playback, events, *, speaker_identity=None,
                mic=None, head=None, volume_store=None):
    """playback=None runs the microphone alone; mic=(url, capture, identity[, recover]).

    With head (head.py) this program announces its robot head and returns
    "taken_over" once a head started later takes the Room.
    """
    voice = VoiceLink()
    gate = CommandGate()
    taken_over = asyncio.Event()

    def on_tts(message):
        kind = str(message.get("type") or "") if isinstance(message, dict) else ""
        if kind.startswith("tts.playback."):
            return handle_control(playback, gate, message)
        return handle_tts(playback, gate, message)

    acks = set()

    def on_volume(message):
        volume, request_id = parse_volume(message)
        playback.set_volume(volume)
        if volume_store is not None:
            volume_store.save(volume)
        logger.info("音量 %s", volume)
        if speaker_identity is not None:
            ack = json.dumps(build_volume_ack(speaker_identity, playback, request_id),
                             ensure_ascii=False)
            task = asyncio.get_running_loop().create_task(voice.send(ack))
            acks.add(task)
            task.add_done_callback(acks.discard)

    def on_voice(message):
        if head is not None and head.hear(message):
            taken_over.set()
            if playback is not None and playback.key is not None:
                return playback.stop(playback.key[0], playback.key[1], "taken_over")
            return []
        if playback is not None and parse_volume(message) is not None:
            on_volume(message)
            return []
        return handle_control(playback, gate, message) if playback is not None else []

    tasks = [asyncio.create_task(run_channel(
        voice_url, room, "reb_voice", ["*"], on_voice, events, voice,
    ), name="reb_voice")]
    # With a head, device reports wait until the head is announced (head.py).
    reports = voice if head is None else head_module().AnnouncedLink(voice, head)
    if playback is not None:
        tasks += [
            asyncio.create_task(run_channel(
                tts_url, room, "reb_tts", ["tts"], on_tts, events,
            ), name="reb_tts"),
            asyncio.create_task(report_receipts(events, voice, playback), name="receipts"),
        ]
        if speaker_identity is not None:
            tasks.append(asyncio.create_task(
                report_presence(reports, speaker_identity, playback), name="presence"))
    if mic is not None:
        module = mic_module()
        mic_url, capture, mic_identity, *rest = mic
        link = module.MicLink()
        tasks += [
            asyncio.create_task(module.run_mic_channel(mic_url, room, capture, link),
                                name="reb_mic"),
            asyncio.create_task(module.report_mic(capture, link, reports, mic_identity,
                                                  recover=rest[0] if rest else None),
                                name="mic_receipts"),
        ]
    if head is not None:
        tasks.append(asyncio.create_task(head_module().report_head(voice, head), name="head"))
    waiter = asyncio.create_task(taken_over.wait(), name="taken_over")
    try:
        # Not gather(): cancelling gather() cancels the children at once, and
        # the reb_voice link would be gone before the goodbye below is sent.
        done, _ = await asyncio.wait([*tasks, waiter], return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            if task is not waiter:
                task.result()
        if waiter not in done:
            return None
        logger.warning("%s（%s）后启动，已接管本房间；本机停止麦克风和扬声器并退出。"
                       "要让本机重新接管：sudo systemctl restart annie-room-audio",
                       head.taken_over_by.get("head_name", "另一个头"),
                       head.taken_over_by.get("address") or head.taken_over_by.get("host", "?"))
        return "taken_over"
    finally:
        reason = "taken_over" if taken_over.is_set() else "exit"
        # Sound and audio stop first; the reports below still need reb_voice.
        media = [task for task in tasks if task.get_name() in {"reb_tts", "reb_mic"}]
        for task in media + [waiter]:
            task.cancel()
        await asyncio.gather(*media, waiter, return_exceptions=True)

        async def goodbye():
            if playback is not None:
                await _flush(events)
            if mic is not None:
                await mic_module().send_mic_stopped(voice, mic[2], mic[1], reason)
            if playback is not None and speaker_identity is not None:
                await voice.send(json.dumps(build_speaker_presence(
                    speaker_identity, playback, state="stopped"), ensure_ascii=False))
            if head is not None:
                await voice.send(json.dumps(head.released_message(reason), ensure_ascii=False))

        try:
            # Best effort: a clean exit tells Brain at once instead of a 6 s timeout.
            await asyncio.wait_for(goodbye(), 1)  # Python 3.10 has no asyncio.timeout
        except (Exception, asyncio.CancelledError):
            pass
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


async def until_stopped(coroutine):
    """systemctl stop/restart sends SIGTERM: leave like Ctrl+C, with the goodbye reports.

    Without them Brain keeps the old instance for 6 s and sees two devices.
    """
    task = asyncio.ensure_future(coroutine)
    try:
        asyncio.get_running_loop().add_signal_handler(signal.SIGTERM, task.cancel)
    except (NotImplementedError, RuntimeError, AttributeError):
        pass  # Windows: no SIGTERM handler in asyncio
    try:
        return await task
    except asyncio.CancelledError:
        logger.info("收到停止信号，已发出停止回执")
        return None


def import_sounddevice():
    try:
        import sounddevice as sd
    except OSError as exc:
        raise SystemExit(
            "无法加载 PortAudio。Linux/地瓜请先安装，例如：sudo apt-get install -y libportaudio2\n"
            + str(exc)
        ) from exc
    return sd


def list_output_devices(kind="output"):
    sd = import_sounddevice()
    devices = sd.query_devices()
    channels = "max_output_channels" if kind == "output" else "max_input_channels"
    default = None
    try:
        default = sd.query_devices(kind=kind)["index"]
    except Exception:
        raw = sd.default.device
        chosen = (raw[1] if kind == "output" else raw[0]) if isinstance(raw, (list, tuple)) else raw
        if type(chosen) is int and chosen >= 0:
            default = chosen
    lines = []
    for index, item in enumerate(devices):
        if item[channels] < 1:
            continue
        mark = "  [默认]" if index == default else ""
        extra = f"\t{item[channels]} 声道" if kind == "input" else ""
        lines.append(f"{index}\t{item['default_samplerate']:.0f} Hz{extra}\t{item['name']}{mark}")
    return "\n".join(lines) if lines else ("没有输出设备" if kind == "output" else "没有输入设备")


def list_all_devices():
    return ("扬声器（输出）\n" + list_output_devices("output")
            + "\n\n麦克风（输入）\n" + list_output_devices("input"))


def resolve_device(spec, kind="output"):
    if spec is None or spec == "":
        return None
    sd = import_sounddevice()
    devices = sd.query_devices()
    channels = "max_output_channels" if kind == "output" else "max_input_channels"
    label = "输出" if kind == "output" else "输入"
    if spec.isdigit():
        index = int(spec)
        if index < 0 or index >= len(devices) or devices[index][channels] < 1:
            raise SystemExit(f"{label}设备编号无效\n" + list_output_devices(kind))
        return index
    matches = [
        index for index, item in enumerate(devices)
        if item[channels] > 0 and spec.casefold() in str(item["name"]).casefold()
    ]
    if len(matches) != 1:
        raise SystemExit(f"{label}设备“{spec}”匹配到 {len(matches)} 个\n" + list_output_devices(kind))
    return matches[0]


def device_description(device):
    sd = import_sounddevice()
    info = sd.query_devices(device, "output")
    rate = int(round(float(info["default_samplerate"])))
    return str(info["name"]), rate


def start_stream(playback, events, device, rate):
    import numpy as np

    sd = import_sounddevice()

    def callback(outdata, frames, _time_info, status):
        if status:
            playback.port_status = str(status)
        try:
            samples, produced = playback.pull(frames)
            outdata[:, 0] = np.frombuffer(samples, dtype=np.int16)
            for event in produced:
                enqueue(events, event)
        except Exception:
            outdata.fill(0)
            playback.faults += 1

    errors = []
    for blocksize in (max(1, rate // 100), 0):
        stream = None
        try:
            stream = sd.OutputStream(
                samplerate=rate, channels=1, dtype="int16", callback=callback,
                device=device, blocksize=blocksize, latency="low",
            )
            stream.start()
            return stream
        except Exception as exc:
            errors.append(str(exc))
            if stream is not None:
                stream.close()
    raise SystemExit("无法打开输出设备\n" + "\n".join(errors) + "\n" + list_output_devices())


def room_url(host, port, room, channel):
    return f"ws://{host}:{port}/robot/{room}/{channel}"


def env_text(name, default):
    value = os.environ.get(name)
    return default if value is None or value == "" else value


def env_int(name, default):
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise SystemExit(f"{name} 不是整数") from exc


def parse_args(argv):
    parser = argparse.ArgumentParser(
        description="播放 Room TTS 并上报扬声器游标；--mic 同时把麦克风送到 reb_mic")
    parser.add_argument("--host", default=env_text("SPEAKER_HOST", "192.168.1.47"))
    parser.add_argument("--port", type=int, default=env_int("SPEAKER_PORT", 8090))
    parser.add_argument("--room", default=env_text("SPEAKER_ROOM", "roomv2"))
    parser.add_argument("--tts-url", default=env_text("SPEAKER_TTS_URL", ""))
    parser.add_argument("--voice-url", default=env_text("SPEAKER_VOICE_URL", ""))
    parser.add_argument("--device", default=env_text("SPEAKER_DEVICE", ""))
    parser.add_argument("--prebuffer-ms", type=int, default=env_int("SPEAKER_PREBUFFER_MS", 180))
    parser.add_argument("--progress-ms", type=int, default=env_int("SPEAKER_PROGRESS_MS", 100))
    parser.add_argument("--mic", action="store_true", default=env_text("ROOM_MIC", "0") == "1",
                        help="同时采集麦克风，发往 reb_mic")
    parser.add_argument("--no-speaker", action="store_true",
                        default=env_text("ROOM_SPEAKER", "1") == "0", help="不播放，只开麦克风")
    parser.add_argument("--mic-url", default=env_text("MIC_URL", ""))
    parser.add_argument("--mic-device", default=env_text("MIC_DEVICE", ""))
    parser.add_argument("--mic-channel", type=int, default=env_int("MIC_CHANNEL", 0),
                        help="多声道麦克风取第几路，从 0 开始")
    parser.add_argument("--head", default=env_text("ROBOT_HEAD", ""),
                        help="机器人头标识，如 big_head。设置后同一 Room 里后启动的头接管，"
                             "先启动的自动停止")
    parser.add_argument("--head-name", default=env_text("ROBOT_HEAD_NAME", ""),
                        help="工作台和大屏显示的名字，如 大头机器人")
    parser.add_argument("--volume", type=int, default=env_int("SPEAKER_VOLUME", 100),
                        help="首次启动的音量 0-100（100 = 不改变响度）；之后用对话调的音量")
    parser.add_argument("--volume-file", default=env_text(
        "SPEAKER_VOLUME_FILE", os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                            ".speaker-volume")))
    parser.add_argument("--list-devices", action="store_true")
    args = parser.parse_args(argv)
    if not args.list_devices:
        if args.head and not re.fullmatch(r"[A-Za-z0-9_-]{1,40}", args.head):
            raise SystemExit("--head 只能用字母、数字、下划线和短横线，最多 40 个字符")
        if args.head_name and not args.head:
            raise SystemExit("--head-name 需要同时设置 --head")
        if args.no_speaker and not args.mic:
            raise SystemExit("--no-speaker 需要同时加 --mic，否则什么也不做")
        if args.mic_channel < 0:
            raise SystemExit("--mic-channel 不能是负数")
        if not 0 <= args.volume <= 100:
            raise SystemExit("--volume 需要在 0 到 100 之间")
        if not args.room or any(char in args.room for char in "/\\ \t"):
            raise SystemExit("room 无效")
        if not 0 <= args.prebuffer_ms <= 1000:
            raise SystemExit("预缓冲需要在 0 到 1000 毫秒之间")
        if not 40 <= args.progress_ms <= 500:
            raise SystemExit("进度间隔需要在 40 到 500 毫秒之间，云端 AEC 要求 850 毫秒内有新回执")
    return args


def main(argv=None):
    if sys.version_info < (3, 10):
        raise SystemExit("需要 Python 3.10 或更新版本")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")
    args = parse_args(argv)
    if args.list_devices:
        print(list_all_devices())
        return 0
    events = queue.Queue()
    tts_url = args.tts_url or room_url(args.host, args.port, args.room, "reb_tts")
    voice_url = args.voice_url or room_url(args.host, args.port, args.room, "reb_voice")
    streams = []
    playback = speaker_identity = mic = head = volume_store = None
    head_identity = {}
    if args.head:
        module = head_module()
        head = module.Head(args.head, args.head_name,
                           address=module.local_address(args.host, args.port))
        head_identity = head.identity()
        logger.info("机器人头 %s（%s）。同一 Room 里后启动的头接管，先启动的自动停止。",
                    head.head_name, head.address or head.host)
    result = None
    try:
        if not args.no_speaker:
            device = resolve_device(args.device)
            name, rate = device_description(device)
            try:
                playback = Playback(rate, prebuffer_ms=args.prebuffer_ms,
                                    progress_ms=args.progress_ms)
            except ValueError as exc:
                raise SystemExit(str(exc)) from exc
            volume_store = VolumeStore(args.volume_file)
            playback.set_volume(volume_store.load(args.volume))
            streams.append(start_stream(playback, events, device, rate))
            speaker_identity = {**mic_module().device_identity("speaker", name), **head_identity}
            if head is not None:
                head.speaker = name[:80]
            logger.info("扬声器 %s", args.room)
            logger.info("播放 %s", tts_url)
            logger.info("设备 %s @ %s Hz，音量 %s（对话可调）", name, rate, playback.volume)
            logger.info("回执 source=speaker。请关闭同一 Room 的网页调试扬声器。")
        if args.mic:
            module = mic_module()
            device = resolve_device(args.mic_device, "input")
            mic_input = module.MicInput(
                import_sounddevice(), device,
                lambda rate: module.MicCapture(rate, channel=args.mic_channel))
            streams.append(mic_input)
            mic_url = args.mic_url or room_url(args.host, args.port, args.room, "reb_mic")
            mic = (mic_url, mic_input.capture,
                   {**module.device_identity("microphone", mic_input.name), **head_identity},
                   mic_input.reopen)
            if head is not None:
                head.microphone = mic_input.name[:80]
            logger.info("麦克风 %s", mic_url)
            logger.info("设备 %s @ %s Hz × %s 声道，取第 %s 路，发送 16 kHz 单声道",
                        mic_input.name, mic_input.rate, mic_input.channels, args.mic_channel)
            logger.info("回执 source=microphone。请关闭同一 Room 的网页麦克风。")
        logger.info("回执 %s。直连 Room，不走系统代理。", voice_url)
        try:
            result = asyncio.run(until_stopped(serve(
                tts_url, voice_url, args.room, playback, events,
                speaker_identity=speaker_identity, mic=mic, head=head,
                volume_store=volume_store)))
        except KeyboardInterrupt:
            logger.info("已退出。播放中断开不补发 stopped。")
    finally:
        for stream in streams:
            stream.stop()
            stream.close()
    return head_module().EXIT_TAKEN_OVER if result == "taken_over" else 0


if __name__ == "__main__":
    raise SystemExit(main())

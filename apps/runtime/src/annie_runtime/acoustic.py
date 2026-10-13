"""Optional, per-robot, observation-only ASR fanout of already admitted PCM.

No consumer waits on this queue. A gap, stale frame, changed identity or failed
connection retires the entire recognition stream; partial segments never splice.
"""

import asyncio
import time
from collections import OrderedDict
from dataclasses import dataclass

from annie_adapters.acoustic import (
    QwenASRSocket,
    acoustic_audio_append,
    acoustic_session_update,
    parse_acoustic_event,
)


@dataclass(frozen=True)
class _Frame:
    pcm: bytes
    received_at: float
    generation: int
    sample_start: int | None


class _LagExceeded(Exception):
    pass


class _ProviderError(Exception):
    def __init__(self, code):
        self.code = code


class AcousticSidecar:
    """One lazy ASR connection, bounded queue, and revocable identity per robot.

    ``on_observation`` is a synchronous, nonblocking Runtime callback. Input
    ``sample_start`` is the main Realtime WS upload cursor, not a provider item
    ID. A connection's first frame fixes the origin of the sidecar sample axis.
    """

    def __init__(self, *, robot_id, room_id, url, key, on_observation,
                 socket_factory=QwenASRSocket, queue_frames=50, max_lag_ms=250,
                 connect_timeout=8, io_timeout=2, event_timeout=60,
                 idle_timeout=5, max_retries=2, reconnect_delay=0.25,
                 enabled=True):
        if type(queue_frames) is not int or queue_frames < 1:
            raise ValueError("queue_frames must be a positive integer")
        if any(value <= 0 for value in (max_lag_ms, connect_timeout, io_timeout, event_timeout, idle_timeout)):
            raise ValueError("ASR timeouts and max_lag_ms must be positive")
        if type(max_retries) is not int or max_retries < 0 or reconnect_delay < 0:
            raise ValueError("Invalid ASR reconnect limits")
        self.robot_id, self.room_id = robot_id, room_id
        self._url, self._key = url, key
        self._on_observation, self._socket_factory = on_observation, socket_factory
        self._queue = asyncio.Queue(maxsize=queue_frames)
        self._max_lag = max_lag_ms / 1000
        self._connect_timeout, self._io_timeout = connect_timeout, io_timeout
        self._event_timeout, self._idle_timeout = event_timeout, idle_timeout
        self._max_retries, self._reconnect_delay = max_retries, reconnect_delay
        self._enabled, self._running = bool(enabled), False
        self._worker = self._connection_task = self._socket = None
        self._generation = self._failures = self._offered = self._sent = self._invalidated = 0
        self._observed = 0
        self._binding = None
        self._next_sample = None
        self._status, self._reason, self._error = "stopped", "not_started", None

    def start(self):
        if self._running:
            return
        self._running = True
        self._status = "idle" if self._enabled else "disabled"
        self._worker = asyncio.create_task(self._run(), name=f"acoustic-{self.robot_id}")

    def set_enabled(self, enabled: bool):
        if bool(enabled) == self._enabled:
            return
        self._enabled = bool(enabled)
        self.invalidate("enabled" if enabled else "disabled")

    def invalidate(self, reason):
        """Synchronously revoke callbacks and abort an obsolete WS; never await."""
        self._retire(reason)
        self._failures = 0

    def _retire(self, reason):
        self._generation += 1
        self._invalidated += 1
        self._binding = None
        self._next_sample = None
        self._reason = reason
        self._status = "idle" if self._running and self._enabled else "disabled" if self._running else "stopped"
        while not self._queue.empty():
            self._queue.get_nowait()
        if self._socket is not None:
            abort = getattr(self._socket, "abort", None)
            if abort is not None:
                try:
                    abort()
                except Exception as exc:
                    self._error = type(exc).__name__
        if self._connection_task is not None:
            self._connection_task.cancel()

    def offer(self, pcm: bytes, *, identity: dict, stream_epoch: int,
              source_id: str, received_at: float, sample_start: int | None = None):
        """Hot path: validate, bind, and put_nowait only; False means dropped."""
        if not self._running or not self._enabled or self._failures > self._max_retries:
            return False
        if (not isinstance(pcm, bytes) or not pcm or len(pcm) % 2 or
                identity.get("robot_id") != self.robot_id or
                any(name not in identity for name in ("chat_id", "persona_id", "generation")) or
                not source_id or type(stream_epoch) is not int or
                (sample_start is not None and (type(sample_start) is not int or sample_start < 0))):
            self._reason = "invalid_frame"
            return False
        if time.monotonic() - received_at > self._max_lag:
            self.invalidate("lag_exceeded")
            return False
        binding = (dict(identity), stream_epoch, source_id)
        if self._binding is not None and binding != self._binding:
            self.invalidate("identity_changed")
        elif self._binding is not None and sample_start != self._next_sample:
            self.invalidate("sample_gap")
        if self._binding is None:
            self._binding = binding
        frame = _Frame(pcm, received_at, self._generation, sample_start)
        try:
            self._queue.put_nowait(frame)
        except asyncio.QueueFull:
            self.invalidate("queue_overflow")
            return False
        self._next_sample = sample_start + len(pcm) // 2 if sample_start is not None else None
        self._offered += 1
        return True

    async def stop(self):
        self._running = False
        self.invalidate("stopped")
        if self._worker is not None:
            self._worker.cancel()
            await asyncio.gather(self._worker, return_exceptions=True)
            self._worker = None
        self._status = "stopped"

    def snapshot(self):
        return {"enabled": self._enabled, "status": self._status, "reason": self._reason,
                "sidecar_generation": self._generation, "queued_frames": self._queue.qsize(),
                "offered_frames": self._offered, "sent_frames": self._sent,
                "invalidated_streams": self._invalidated, "observations": self._observed,
                "failures": self._failures, "last_error": self._error,
                "connected": self._socket is not None and self._status == "streaming"}

    async def _run(self):
        while self._running:
            frame = await self._queue.get()
            if frame.generation != self._generation:
                continue
            try:
                if self._failures:
                    await asyncio.sleep(min(2, self._reconnect_delay * 2 ** (self._failures - 1)))
                if frame.generation != self._generation:
                    continue
                self._connection_task = asyncio.create_task(self._session(frame))
                await self._connection_task
            except asyncio.CancelledError:
                if not self._running:
                    raise
            except _LagExceeded:
                self._retire("lag_exceeded")
            except Exception as exc:
                self._error = exc.code if isinstance(exc, _ProviderError) else type(exc).__name__
                self._failures += 1
                self._retire("connection_error")
                if self._failures > self._max_retries:
                    self._status = "unavailable"
            else:
                self._retire("idle")
            finally:
                self._connection_task = None

    async def _session(self, first):
        generation, binding = first.generation, self._binding
        socket, children = None, []
        self._status = "connecting"
        try:
            if time.monotonic() - first.received_at > self._max_lag:
                raise _LagExceeded()
            socket = self._socket_factory(self._url, self._key)
            self._socket = socket
            await asyncio.wait_for(socket.open(), self._connect_timeout)
            await asyncio.wait_for(socket.send(acoustic_session_update()), self._io_timeout)
            async with asyncio.timeout(self._connect_timeout):
                while True:
                    event = await socket.recv()
                    if event.get("type") == "session.updated":
                        break
                    parsed = parse_acoustic_event(event)
                    if parsed and parsed["kind"] == "error":
                        raise _ProviderError(parsed["error_code"])
            if generation != self._generation:
                return
            self._status = "streaming"
            self._reason = "receiving"
            state = {"samples": 0, "origin": first.sample_start, "windows": OrderedDict()}
            children = [asyncio.create_task(self._send(socket, first, generation, state)),
                        asyncio.create_task(self._receive(socket, generation, binding, state))]
            done, _ = await asyncio.wait(children, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                task.result()
        finally:
            if generation == self._generation:
                self._status = "closing"
            for task in children:
                task.cancel()
            await asyncio.gather(*children, return_exceptions=True)
            if socket is not None:
                try:
                    await asyncio.wait_for(socket.close(), self._io_timeout)
                except Exception as exc:
                    self._error = type(exc).__name__
            if self._socket is socket:
                self._socket = None

    async def _send(self, socket, first, generation, state):
        frame = first
        while generation == self._generation:
            if frame.generation != generation:
                return
            remaining = self._max_lag - (time.monotonic() - frame.received_at)
            if remaining <= 0:
                raise _LagExceeded()
            try:
                await asyncio.wait_for(socket.send(acoustic_audio_append(frame.pcm)),
                                       min(self._io_timeout, remaining))
            except TimeoutError:
                if time.monotonic() - frame.received_at >= self._max_lag:
                    raise _LagExceeded() from None
                raise
            if generation != self._generation:
                return
            state["samples"] += len(frame.pcm) // 2
            self._sent += 1
            self._failures = 0
            try:
                frame = await asyncio.wait_for(self._queue.get(), self._idle_timeout)
            except TimeoutError:
                return

    async def _receive(self, socket, generation, binding, state):
        identity, epoch, source = binding
        while generation == self._generation:
            event = await asyncio.wait_for(socket.recv(), self._event_timeout)
            if generation != self._generation:
                return
            parsed = parse_acoustic_event(event)
            if parsed is None:
                if event.get("type") == "session.finished":
                    return
                continue
            item = parsed["provider_item_id"]
            windows = state["windows"]
            if item:
                window = windows.setdefault(item, {"start": None, "end": None})
                windows.move_to_end(item)
                while len(windows) > 64:
                    windows.popitem(last=False)
            else:
                window = {"start": None, "end": None}
            if parsed["kind"] == "speech_started":
                window["start"] = parsed["audio_start_ms"]
            elif parsed["kind"] == "speech_stopped":
                window["end"] = parsed["audio_end_ms"]
            origin = state["origin"]
            main_start = (origin + window["start"] * 16
                          if origin is not None and window["start"] is not None else None)
            main_end, end_source = None, None
            if origin is not None and window["end"] is not None:
                main_end, end_source = origin + window["end"] * 16, "provider_vad"
            elif (parsed["kind"] == "partial" and main_start is not None and
                  state["samples"] >= window["start"] * 16):
                # A partial's current extent is a local upload watermark, not
                # an invented provider speech_stopped/audio_end_ms event.
                main_end, end_source = origin + state["samples"], "upload_cursor"
            observed_at = time.monotonic()
            row = {**parsed, **identity, "room_id": self.room_id,
                   "audio_stream_epoch": epoch, "stream_epoch": epoch, "source_id": source,
                   "sidecar_generation": generation, "observed_at": observed_at,
                   "observed_monotonic_ms": observed_at * 1000,
                   "uploaded_samples": state["samples"],
                   "main_sample_start": main_start, "main_sample_end": main_end,
                   "window_end_source": end_source}
            try:
                self._on_observation(row)
                self._observed += 1
            except Exception as exc:
                self._error = type(exc).__name__
            if parsed["kind"] == "error":
                raise _ProviderError(parsed["error_code"])

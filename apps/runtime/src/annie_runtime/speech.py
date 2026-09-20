"""Per-robot independent TTS and the verified reb_tts PCM publication boundary."""

import asyncio
import base64
import logging
import time

from annie_adapters.tts import QwenTtsSocket, command, task_id

logger = logging.getLogger(__name__)
FRAME_BYTES = 960  # 20ms, 24kHz, S16LE, mono; not the 16kHz MIC_AUDIO format.
SENTENCE_END = frozenset("。！？!?；;\n")
CLAUSE_END = frozenset("，,、")
PCM_LEAD_SECONDS = 0.24  # publish ahead of the sample clock to absorb network jitter


class SpeechOutput:
    """One bounded text queue and one synthesis/publisher owner per robot."""

    def __init__(self, robot, publisher, *, url, key, voice, socket_factory=QwenTtsSocket,
                 first_chars=10, max_chars=48, first_wait_ms=360, event=None):
        self.robot = robot
        self.publisher = publisher
        self.url = url
        self.key = key
        self.voice = voice
        self.socket_factory = socket_factory
        self.first_chars = first_chars
        self.max_chars = max_chars
        self.first_wait_ms = first_wait_ms
        self.event = event or (lambda *_args, **_kw: None)
        self.queue = asyncio.Queue(maxsize=8)
        self.worker = None
        self.flush_timer = None
        self.socket = None
        self.turn_id = None
        self.identity = None
        self.pending = ""
        self.segment = 0
        self.seq = 0
        self.samples = 0
        self.inflight_end = 0
        self.frames = 0
        self.first_audio_ms = None
        self.first_publish_ms = None
        self.error = None
        self.cancelled = False
        self.finished = False
        self.published_text = ""
        self.last_published_segment = None
        self.started_at = None
        self.next_pcm_at = None
        self.failed_turn = None

        self.stream_epoch = 0
        self.playback_id = self.playback_source = None
        self.receipt_tracks = {}  # independent browser_test and speaker cursors
        self.played_samples = self.receipt_seq = 0
        self.playback_state = self.playback_evidence = "unknown"
        self.stop_requested_at = self.stop_published_at = self.stop_confirmed_ms = None
        self.started_reported_ms = None
        self.pending_stop = None
        self.pending_stop_identity = None
        self.pending_stop_source = None
        self.stopped_conversation_id = None
        self.last_stop_evidence = "unknown"
        self.synthesized_complete = False
        self.viseme_events = 0
        self.viseme_seen = set()
        self.viseme_state = "not_connected"

    def snapshot(self):
        return {
            "state": "cancelled" if self.cancelled else "error" if self.error else
                     "active" if self.worker and not self.worker.done() else "idle",
            "voice": self.voice, "turn_id": self.turn_id, "queue_depth": self.queue.qsize(),
            "pcm_frames_published": self.frames, "pcm_samples_published": self.samples,
            "first_audio_ms": self.first_audio_ms, "first_publish_ms": self.first_publish_ms,
            "stream_epoch": self.stream_epoch, "playback_state": self.playback_state,
            "playback_source": self.playback_source, "playback_id": self.playback_id,
            "played_sample_count": self.played_samples,
            "playback_evidence": self.playback_evidence, "viseme": self.viseme_state,
            "viseme_events": self.viseme_events,
            "stop_requested_ms": self.stop_requested_at,
            "stop_published_ms": self.stop_published_at,
            "started_reported_ms": self.started_reported_ms,
            "stop_confirmed_ms": self.stop_confirmed_ms,
            "stopped_conversation_id": self.stopped_conversation_id,
            "last_stop_evidence": self.last_stop_evidence,
            "error": self.error,
        }

    def _emit(self, kind, **data):
        self.event(kind, **{**(self.identity or self.robot.session_state()), **data})

    async def _begin(self, turn_id, identity):
        if turn_id == self.turn_id:
            return
        if self.worker or self.turn_id:
            await self.cancel("next_turn")
        self.queue = asyncio.Queue(maxsize=8)
        self.stream_epoch += 1
        self.turn_id = turn_id
        self.identity = dict(identity)
        self.cancelled = False
        self.finished = False
        self.pending = ""
        self.segment = self.seq = self.samples = self.frames = 0
        self.inflight_end = 0
        self.error = self.first_audio_ms = self.first_publish_ms = None
        self.published_text = ""
        self.last_published_segment = None
        self.started_at = time.perf_counter()
        self.next_pcm_at = None
        self.failed_turn = None
        self.playback_id = self.playback_source = None
        self.receipt_tracks.clear()
        self.played_samples = self.receipt_seq = self.viseme_events = 0
        self.playback_state = self.playback_evidence = "unknown"
        self.stop_requested_at = self.stop_published_at = self.stop_confirmed_ms = None
        self.stopped_conversation_id = None
        self.last_stop_evidence = "unknown"
        self.synthesized_complete = False
        self.viseme_state = "not_connected"
        self.viseme_seen.clear()
        self.worker = asyncio.create_task(self._run(turn_id, dict(identity)), name=f"tts:{self.robot.config.robot_id}")

    def _current(self, turn_id, identity):
        return (
            not self.cancelled and self.turn_id == turn_id
            and self.identity == identity and self.robot.session_state() == identity
        )

    def _enqueue(self, segment):
        if not segment.strip():
            return
        try:
            self.queue.put_nowait(segment)
        except asyncio.QueueFull as exc:
            self.error = "TTS text queue full; stopped rather than silently omitting text"
            self._emit("tts.error", error=self.error)
            raise RuntimeError(self.error) from exc

    def _flush(self):
        if self.pending.strip():
            segment, self.pending = self.pending, ""
            self._enqueue(segment)
            self._emit("tts.segment_queued", chars=len(segment))

    async def feed(self, turn_id, identity, delta):
        if not delta or turn_id == self.failed_turn:
            return
        await self._begin(turn_id, identity)
        if self.finished or not self._current(turn_id, identity):
            return
        try:
            self.pending += delta
            while self.pending:
                cut = next((i + 1 for i, char in enumerate(self.pending) if char in SENTENCE_END
                            and i + 1 >= self.first_chars), None)
                if cut is None and len(self.pending) >= 24:
                    cut = next((i + 1 for i, char in enumerate(self.pending) if char in CLAUSE_END
                                and i + 1 >= 12), None)
                if cut is None and len(self.pending) >= self.max_chars:
                    cut = max((i + 1 for i, char in enumerate(self.pending[:self.max_chars])
                               if char in CLAUSE_END and i + 1 >= 12), default=self.max_chars)
                if cut is None:
                    break
                segment, self.pending = self.pending[:cut], self.pending[cut:]
                self._enqueue(segment)
                self._emit("tts.segment_queued", chars=len(segment))
        except RuntimeError:
            self.failed_turn = turn_id
            await self.cancel("tts_queue_full")
            return
        if self.pending and not self.flush_timer:
            self.flush_timer = asyncio.create_task(self._first_flush(turn_id, identity))

    async def _first_flush(self, turn_id, identity):
        try:
            await asyncio.sleep(self.first_wait_ms / 1000)
            if self._current(turn_id, identity) and len(self.pending) >= self.first_chars:
                self._flush()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.error = str(exc)
            self._emit("tts.error", error=self.error)
        finally:
            self.flush_timer = None

    async def finish(self, turn_id):
        if turn_id != self.turn_id or self.finished:
            return
        self.finished = True
        if self.flush_timer:
            self.flush_timer.cancel()
            self.flush_timer = None
        self._flush()
        if self.worker and not self.worker.done():
            try:
                self.queue.put_nowait(None)
            except asyncio.QueueFull:
                try:
                    await asyncio.wait_for(self.queue.put(None), timeout=1)
                except asyncio.TimeoutError:
                    await self.cancel("tts_queue_timeout")

    async def cancel(self, reason="cancel"):
        was_active = self.turn_id is not None
        self.cancelled = True
        if was_active:
            self.stop_requested_at = time.time_ns() // 1_000_000
            self.playback_state = "stop_requested"
            self.pending_stop = (
                self.turn_id, self.stream_epoch,
                max(self.samples, self.inflight_end), self.played_samples,
            )
            self.pending_stop_identity = self.identity
            self.pending_stop_source = (self.playback_source, self.playback_id)
            self._emit("tts.stop_requested", reason=reason,
                       stop_requested_ms=self.stop_requested_at)
        if self.flush_timer:
            self.flush_timer.cancel()
            await asyncio.gather(self.flush_timer, return_exceptions=True)
            self.flush_timer = None
        if self.worker and not self.worker.done():
            self.worker.cancel()
            await asyncio.gather(self.worker, return_exceptions=True)
        if self.socket:
            await self.socket.close()
            self.socket = None
        while not self.queue.empty():
            self.queue.get_nowait()
            self.queue.task_done()
        self.worker = None
        if was_active:
            # The established Room contract only proves publication of clear,
            # not that the device has actually stopped or which samples played.
            try:
                await self.publisher.publish(self._packet(b"", "", final=True, stop=True, reason=reason))
                self.stop_published_at = time.time_ns() // 1_000_000
                self._emit("tts.stop_published", reason=reason, device_stopped="unknown",
                           stop_published_ms=self.stop_published_at)
            except Exception as exc:
                self.error = f"cannot publish stop: {type(exc).__name__}"
                self._emit("tts.error", error=self.error)
        self.turn_id = None
        self.identity = None
        self.pending = ""

    async def close(self):
        await self.cancel("runtime_stop")
        await self.publisher.close()

    def _packet(self, pcm, text, *, final=False, stop=False, segment_id=None,
                reason=None, stream_final=False):
        self.seq += 1
        # Cancellation may interrupt await ws.send() after transport writes a
        # frame but before publish returns. Never cap a stop below that frame.
        start_sample = max(self.samples, self.inflight_end) if stop else self.samples
        return {
            "type": "tts", "ts": time.time_ns() // 1_000_000,
            "data": {
                "audio": base64.b64encode(pcm).decode("ascii"), "format": "pcm_s16le",
                "sample_rate": 24000, "channels": 1, "text": text, "seq": self.seq,
                "final": final, "turn_id": segment_id or self.turn_id,
                "conversation_id": self.turn_id, "source": "robot_main_brain",
                "playback_protocol": "sample_cursor_v1", "protocol_version": 1,
                "stream_epoch": self.stream_epoch, "start_sample": start_sample,
                "end_sample": start_sample + len(pcm) // 2,
                "audio_total_samples": start_sample + len(pcm) // 2,
                **({"stream_final": True} if stream_final else {}),
                **({"stop": True} if stop else {}),
                **({"reason": reason} if reason else {}),
            },
        }

    async def _run(self, turn_id, identity):
        try:
            while True:
                text = await self.queue.get()
                try:
                    if text is None:
                        if self._current(turn_id, identity):
                            await self.publisher.publish(self._packet(b"", "", final=True,
                                                                      stream_final=True))
                        self.synthesized_complete = True
                        return
                    if not self._current(turn_id, identity):
                        return
                    self.segment += 1
                    await self._synthesize(text, f"{turn_id}-{self.segment}", turn_id, identity)
                finally:
                    self.queue.task_done()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.error = f"{type(exc).__name__}: {str(exc)[:160]}"
            logger.warning("TTS robot=%s failed: %s", self.robot.config.robot_id, self.error)
            self._emit("tts.error", error=self.error)
            self.failed_turn = turn_id
            try:
                await self.publisher.publish(self._packet(b"", "", final=True, stop=True))
                self._emit("tts.stop_published", reason="tts_error", device_stopped="unknown")
            except Exception:
                logger.warning("TTS robot=%s stop publish failed after error", self.robot.config.robot_id)
            while not self.queue.empty():
                self.queue.get_nowait()
                self.queue.task_done()

    async def _synthesize(self, text, segment_id, turn_id, identity):
        socket = self.socket_factory(self.url, self.key)
        self.socket = socket
        started = time.perf_counter()
        task = task_id()
        tail = bytearray()
        sample_cursor = self.samples
        first = True
        sent_text = False
        try:
            async with asyncio.timeout(30):
                await socket.open()
                await socket.send(command("run-task", task, voice=self.voice))
                while True:
                    item = await socket.recv()
                    if isinstance(item, bytes):
                        if not self._current(turn_id, identity):
                            return
                        if not item:
                            continue
                        if first:
                            first = False
                            ms = round((time.perf_counter() - started) * 1000, 2)
                            if self.first_audio_ms is None:
                                self.first_audio_ms = ms
                            self._emit("tts.first_audio", first_audio_ms=ms)
                        tail.extend(item)
                        while len(tail) >= FRAME_BYTES:
                            frame = bytes(tail[:FRAME_BYTES])
                            del tail[:FRAME_BYTES]
                            await self._publish_pcm(frame, text, segment_id, turn_id, identity, started)
                    else:
                        header = item.get("header") or {}
                        if header.get("task_id") not in (None, task):
                            raise ValueError("TTS task_id mismatch")
                        event = header.get("event")
                        if event == "task-started":
                            if sent_text:
                                continue
                            sent_text = True
                            await socket.send(command("continue-task", task, text=text))
                            await socket.send(command("finish-task", task))
                        elif event in {"task-failed", "error"}:
                            raise RuntimeError(f"TTS {event}: {str(header.get('error_code', 'provider_error'))[:80]}")
                        elif event == "task-finished":
                            if len(tail) % 2:
                                raise ValueError("TTS PCM has an odd total byte count")
                            if tail:
                                await self._publish_pcm(bytes(tail), text, segment_id, turn_id, identity, started)
                                tail.clear()
                            if self._current(turn_id, identity):
                                await self.publisher.publish(self._packet(b"", "", final=True, segment_id=segment_id))
                                self._emit("tts.segment_done", segment_id=segment_id, samples=self.samples-sample_cursor)
                            return
        finally:
            await socket.close()
            if self.socket is socket:
                self.socket = None

    async def _publish_pcm(self, pcm, text, segment_id, turn_id, identity, started):
        if not self._current(turn_id, identity):
            return
        # Keep up to 240 ms ahead of the PCM sample clock. Strict 1x publication
        # leaves no reserve for WebSocket and browser scheduling jitter.
        now = time.monotonic()
        if self.next_pcm_at is not None and self.next_pcm_at > now:
            await asyncio.sleep(self.next_pcm_at - now)
        if not self._current(turn_id, identity):
            return
        now = time.monotonic()
        self.next_pcm_at = max(now - PCM_LEAD_SECONDS,
                               self.next_pcm_at if self.next_pcm_at is not None else now - PCM_LEAD_SECONDS
                               ) + len(pcm) / 2 / 24000
        self.inflight_end = self.samples + len(pcm) // 2
        async with asyncio.timeout(2):
            await self.publisher.publish(self._packet(pcm, text, segment_id=segment_id))
        self.samples += len(pcm) // 2
        self.frames += 1
        if self.first_publish_ms is None:
            self.first_publish_ms = round((time.perf_counter() - self.started_at) * 1000, 2)
            self._emit("tts.first_published", first_publish_ms=self.first_publish_ms)
        if self.last_published_segment != segment_id:
            self.last_published_segment = segment_id
            self.published_text += text
            now = time.time_ns() // 1_000_000
            self.robot.ledger.enqueue(
                "update_playback", self.robot.config.robot_id, turn_id,
                published_text=self.published_text, spoken_text=None,
                evidence_status="unknown", updated_ms=now,
            )
            turn = self.robot.turn_cache.get(turn_id)
            if turn is not None:
                turn["assistant_published_text"] = self.published_text
        self._emit("tts.pcm_published", segment_id=segment_id, bytes=len(pcm), samples=self.samples)

    def on_playback_receipt(self, message):
        """Reported playback cursor; caller validates wire shape and Room ownership."""
        from annie_adapters.playback import playback_receipt

        playback_receipt(message)
        data = message["data"]
        cid, epoch = data["conversation_id"], data["stream_epoch"]
        active = (cid, epoch) == (self.turn_id, self.stream_epoch)
        stopped = self.pending_stop is not None and (cid, epoch) == self.pending_stop[:2]
        if not active and not stopped:
            return False
        limit = max(self.samples, self.inflight_end) if active else self.pending_stop[2]
        if data["played_sample_count"] > limit:
            return False
        source, playback_id = data["source"], data["playback_id"]
        track = self.receipt_tracks.get(source) if active else None
        if active and track is not None:
            if track["id"] != playback_id or data["seq"] <= track["seq"]:
                return False
            if data["played_sample_count"] < track["played"]:
                return False
        if stopped and not active and data["played_sample_count"] < self.pending_stop[3]:
            return False
        if stopped and not active and self.pending_stop_source[0] is not None and (
            data["source"], data["playback_id"]
        ) != self.pending_stop_source:
            return False
        kind = message["type"].removeprefix("tts.playback.")
        if kind == "started" and (not active or limit == 0):
            return False
        state = track["state"] if track is not None else "unknown"
        if kind in {"progress", "drained"} and state not in {"started", "progress"}:
            return False
        if kind in {"started", "progress"} and (not active or state in {"stopped", "drained"}):
            return False
        if kind == "started" and track is not None:
            return False
        if kind == "drained" and (
            not active or not self.synthesized_complete or
            data["played_sample_count"] != self.samples or self.samples == 0
        ):
            return False
        if kind == "stopped" and not stopped and not active:
            return False
        if stopped and not active and kind != "stopped":
            return False
        received_ms = time.time_ns() // 1_000_000
        preferred = self.playback_source != "speaker" or source == "speaker"
        if active:
            self.receipt_tracks[source] = {
                "id": playback_id, "seq": data["seq"],
                "played": data["played_sample_count"], "state": kind,
            }
            # The test listener cannot eclipse real speaker evidence.
            if preferred:
                self.playback_source = source
                self.playback_id = playback_id
                self.receipt_seq = data["seq"]
                self.played_samples = data["played_sample_count"]
                self.playback_state = kind
                self.playback_evidence = f"{source}_reported_{kind}"
        if kind == "started" and preferred:
            self.started_reported_ms = received_ms
        if kind == "stopped":
            self.stop_confirmed_ms = received_ms
            self.stopped_conversation_id = cid
            self.last_stop_evidence = f"{data['source']}_reported_stopped"
            self.pending_stop = None
            old_identity = self.pending_stop_identity
            self.pending_stop_identity = self.pending_stop_source = None
        else:
            old_identity = None
        evidence = f"{data['source']}_reported_{kind}"
        self._emit("tts.playback." + kind, conversation_id=cid, stream_epoch=epoch,
                   played_sample_count=data["played_sample_count"], source=data["source"],
                   playback_id=data["playback_id"], receipt_ms=received_ms,
                   evidence=evidence, **(old_identity or {}))
        # Only a completed, fully drained turn can claim a complete spoken text.
        # A partial stop has no per-word acoustic boundary, so never infer it.
        if kind == "drained" and data["source"] == "speaker" and cid in self.robot.turn_cache:
            turn = self.robot.turn_cache[cid]
            if turn.get("status") == "completed":
                now = received_ms
                self.robot.ledger.enqueue(
                    "update_playback", self.robot.config.robot_id, cid,
                    published_text=None, spoken_text=self.published_text,
                    evidence_status=self.playback_evidence, updated_ms=now,
                )
                turn["assistant_spoken_text"] = self.published_text
                turn["playback_evidence_status"] = self.playback_evidence
                if self.identity == self.robot.session_state():
                    self.robot._cache_context_turn(turn)
        return True

    def on_mouth_script(self, event):
        """Accept only real acoustic alignment for the currently active PCM."""
        if event.get("type") != "mouth_script" or self.turn_id is None or self.cancelled:
            return False
        if event.get("conversation_id") != self.turn_id:
            return False
        if event.get("stream_epoch", self.stream_epoch) != self.stream_epoch:
            return False
        if not str(event.get("timing_source", "")).startswith("qwen3_forced_aligner"):
            return False
        start, end = event.get("start_sample"), event.get("end_sample")
        if type(start) is not int or type(end) is not int or start < 0 or end <= start:
            return False
        if end > self.samples:
            return False
        if start < self.played_samples:
            return False
        marker = (event.get("stream_id"), event.get("seq"))
        if marker in self.viseme_seen:
            return False
        self.viseme_seen.add(marker)
        self.viseme_state = "aligner_reporting"
        self.viseme_events += 1
        self._emit("viseme.aligned", conversation_id=self.turn_id,
                   stream_epoch=self.stream_epoch, start_sample=start,
                   end_sample=end, viseme=event.get("viseme"), char=event.get("char"),
                   timing_source=event["timing_source"])
        return True

"""Per-robot Qwen-Audio Realtime Session; no extra ASR/LLM or audio publisher."""

import asyncio
import json
import logging
import time
from collections import deque
from uuid import uuid4

from annie_adapters.realtime import QwenRealtimeSocket, audio_append, session_update
from annie_core import RobotUnavailableError

logger = logging.getLogger(__name__)


class RealtimeRobot:
    """One provider Session, one current Chat/Persona, bounded events and PCM."""

    def __init__(self, robot, *, url: str, key: str, socket_factory=QwenRealtimeSocket):
        self.robot = robot
        self.url = url
        self.key = key
        self.socket_factory = socket_factory
        self.speech = None
        self.socket = None
        self.task = None
        self.audio_task = None
        self.ready = False
        self.error = None
        self.session = None
        self.sequence = 0
        self.events = deque(maxlen=512)
        self.event_subscribers = set()
        self.response_id = None
        self.turn_id = None
        self.audio_turn_id = None  # retain the audio turn across response.done for late final ASR
        self.transcript = ""
        self.text = ""
        self.speech_active = False
        self.calls = set()
        self.call_count = 0
        self.followup_needed = False
        self.first_text_ms = None
        self.began = None
        self.frames_sent = 0
        self.frames_dropped = 0
        self.turn_identity = None
        self._stopping = False
        self._opening = False
        self._open_done = asyncio.Event()
        self._open_done.set()
        self._lifecycle = asyncio.Lock()

    def publish(self, kind, **fields):
        self.sequence += 1
        identity = (
            self.turn_identity if self.turn_identity and kind in {"turn.done", "text.delta", "text.done"}
            else self.robot.session_state()
        )
        event = {
            "seq": self.sequence,
            "kind": kind,
            **identity,
            "ts_ms": time.time_ns() // 1_000_000,
            **fields,
        }
        self.events.append(event)
        for queue in tuple(self.event_subscribers):
            if queue.full():
                queue.get_nowait()
            queue.put_nowait(event)
        return event

    def subscribe(self):
        queue = asyncio.Queue(maxsize=128)
        self.event_subscribers.add(queue)
        return queue

    def unsubscribe(self, queue):
        self.event_subscribers.discard(queue)

    def snapshot(self):
        return {
            **self.robot.session_state(),
            "ready": self.ready,
            "state": "connected" if self.ready else "error" if self.error else "connecting" if self.task else "disabled",
            "error": self.error,
            "provider_session_id": self.session,
            "response_id": self.response_id,
            "speech_active": self.speech_active,
            "frames_sent": self.frames_sent,
            "frames_dropped": self.frames_dropped,
            "first_text_ms": self.first_text_ms,
            "speech_output": self.speech.snapshot() if self.speech else {"state": "not_configured"},
        }

    def since(self, after):
        return {
            "events": [event for event in self.events if event["seq"] > after],
            "gap": bool(self.events and after and after < self.events[0]["seq"] - 1),
            "latest": self.sequence,
        }

    def start(self):
        if self.task is None or self.task.done():
            self._stopping = False
            self.task = asyncio.create_task(self._run(), name=f"realtime:{self.robot.config.robot_id}")

    async def stop(self):
        async with self._lifecycle:
            await self._stop_impl()

    async def _stop_impl(self):
        self._stopping = True
        if self.speech:
            await self.speech.close()
        logger.info(
            "realtime_shutdown robot=%s opening=%s ready=%s",
            self.robot.config.robot_id, self._opening, self.ready,
        )
        # Do not cancel an HTTP CONNECT proxy handshake halfway through:
        # websockets 15 can then access an already-cancelled proxy Future.
        if self._opening:
            try:
                await asyncio.wait_for(self._open_done.wait(), 10)
            except asyncio.TimeoutError:
                logger.warning("Realtime handshake shutdown timeout robot=%s", self.robot.config.robot_id)
        for task in (self.task, self.audio_task):
            if task is not None and not task.done():
                task.cancel()
        logger.info("realtime_shutdown robot=%s awaiting_tasks", self.robot.config.robot_id)
        await asyncio.gather(*(t for t in (self.task, self.audio_task) if t is not None), return_exceptions=True)
        if self.socket is not None:
            await self.socket.close()
        self.task = self.audio_task = self.socket = None
        self.ready = False
        self.session = None
        self._reset_turn()
        self.audio_turn_id = None
        logger.info("realtime_shutdown robot=%s complete", self.robot.config.robot_id)

    async def restart(self):
        async with self._lifecycle:
            await self._stop_impl()
            self.robot.audio._clear()
            self.error = None
            self.publish("session.replaced")
            self.start()

    def _reset_turn(self):
        self.turn_id = self.response_id = None
        self.text = self.transcript = ""
        self.began = self.first_text_ms = None
        self.speech_active = False
        self.calls.clear()
        self.call_count = 0
        self.followup_needed = False
        self.turn_identity = None

    async def _run(self):
        retry = 1
        while not self._stopping:
            generation = self.robot.generation
            try:
                self.socket = self.socket_factory(self.url, self.key)
                self._opening = True
                self._open_done.clear()
                try:
                    await self.socket.open()
                finally:
                    self._opening = False
                    self._open_done.set()
                if self._stopping:
                    return
                self.publish("socket.connected")
                await self.socket.send(session_update(
                    "你是小安妮，一位陪伴型机器人。自然、简洁地用中文回应；不要描述没有发生的动作。"
                    f"当前 Persona 标识：{self.robot.persona_id}。"
                    "真实动作未启用；如需动作，只能使用 preview_action 模拟工具。",
                    self.robot.context_limit,
                ))
                # An invalid combined capability must fail visibly before audio enters the model.
                while True:
                    event = await asyncio.wait_for(self.socket.recv(), 8)
                    if event.get("type") == "error":
                        raise RuntimeError(self._provider_error(event))
                    if event.get("type") == "session.updated":
                        config = event.get("session") or {}
                        if config.get("modalities") != ["text"] or (
                            (config.get("turn_detection") or {}).get("type") != "smart_turn"
                        ):
                            raise RuntimeError("Provider did not confirm text-only smart_turn")
                        self.session = config.get("id")
                        break
                if generation != self.robot.generation:
                    continue
                await self._restore_context()
                self.ready = True
                self.error = None
                retry = 1
                self.publish("session.ready", model="qwen-audio-3.0-realtime-flash")
                self.audio_task = asyncio.create_task(self._audio_pump(generation))
                while generation == self.robot.generation:
                    event = await self.socket.recv()
                    await self.handle(event, generation)
                raise ConnectionError("session generation changed")
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # Do not log URL, API key, raw PCM, or user dialogue.
                self.error = type(exc).__name__ + ": " + str(exc)[:200]
                logger.warning("Realtime robot=%s disconnected: %s", self.robot.config.robot_id, self.error)
                self.publish("session.error", error=self.error)
            finally:
                self.ready = False
                if self.speech and self.speech.turn_id is not None:
                    await self.speech.cancel("realtime_disconnected")
                logger.info("realtime_cleanup robot=%s begin", self.robot.config.robot_id)
                if self.audio_task is not None:
                    self.audio_task.cancel()
                    await asyncio.gather(self.audio_task, return_exceptions=True)
                    self.audio_task = None
                logger.info("realtime_cleanup robot=%s audio_done", self.robot.config.robot_id)
                if self.turn_id:
                    self._finish(
                        "discarded" if generation != self.robot.generation else "failed",
                        cancel_source="session_replaced" if generation != self.robot.generation
                        else "provider_disconnected",
                    )
                self._reset_turn()
                self.robot.audio._clear()
                if self.socket is not None:
                    logger.info("realtime_cleanup robot=%s socket_close_begin", self.robot.config.robot_id)
                    await self.socket.close()
                    self.socket = None
                logger.info("realtime_cleanup robot=%s socket_close_done", self.robot.config.robot_id)
                self.session = None
            if self._stopping:
                return
            await asyncio.sleep(retry)
            retry = min(15, retry * 2)

    async def _restore_context(self):
        for turn in self.robot.recent_context():
            user, reply = turn.get("user_text"), turn.get("assistant_context_text") or turn.get(
                "assistant_generated_text"
            )
            if not user or not reply:
                continue
            await self.socket.send({
                "type": "conversation.item.create",
                "item": {"type": "message", "role": "user",
                         "content": [{"type": "input_text", "text": user}]},
            })
            await self.socket.send({
                "type": "conversation.item.create",
                "item": {"type": "message", "role": "assistant",
                         "content": [{"type": "output_text", "text": reply}]},
            })

    async def _audio_pump(self, generation):
        while self.ready and generation == self.robot.generation:
            audio = self.robot.audio
            frame, stream_epoch = await audio.next_frame()
            try:
                if (
                    not self.ready
                    or self._stopping
                    or generation != self.robot.generation
                    or stream_epoch != audio.stream_epoch
                    or frame.source_id != audio.source_id
                    or (time.monotonic() - frame.received_at) * 1000
                    > self.robot.config.audio_max_age_ms
                ):
                    self.frames_dropped += 1
                    continue
                await self.socket.send(audio_append(frame.payload))
                self.frames_sent += 1
            finally:
                audio.queue.task_done()

    def _create_turn(self, user_text, *, audio=False):
        if self.turn_id:
            return
        now = time.time_ns() // 1_000_000
        turn_id = uuid4().hex
        identity = self.robot.session_state()
        fields = {**identity, "turn_id": turn_id, "user_text": user_text, "status": "running",
                  "created_ms": now, "updated_ms": now}
        self.robot.ledger.enqueue("create_conversation_turn", fields)
        self.robot._remember_turn({
            **fields, "id": None, "assistant_planned_text": None, "assistant_generated_text": None,
            "assistant_published_text": None, "assistant_spoken_text": None,
            "playback_evidence_status": "unknown", "interrupted": False, "cancelled": False,
            "cancel_source": None,
        })
        self.turn_id = turn_id
        if audio:
            self.audio_turn_id = turn_id
        self.turn_identity = identity
        self.began = time.perf_counter()
        self.publish("turn.started", turn_id=turn_id, user_text=user_text)

    def _finish(self, status, *, cancel_source=None):
        if not self.turn_id:
            self._reset_turn()
            return
        now = time.time_ns() // 1_000_000
        interrupted = status == "cancelled"
        self.robot.ledger.enqueue(
            "finish_conversation_turn", self.robot.config.robot_id, self.turn_id,
            status=status, generated_text=self.text or None, planned_text=self.text or None,
            cancelled=interrupted, interrupted=interrupted, cancel_source=cancel_source,
            updated_ms=now,
        )
        turn = self.robot.turn_cache.get(self.turn_id)
        if turn is not None:
            turn.update(status=status, assistant_generated_text=self.text or None,
                        assistant_planned_text=self.text or None, updated_ms=now,
                        cancelled=interrupted, interrupted=interrupted, cancel_source=cancel_source)
        if status == "completed" and turn is not None and self.turn_identity == self.robot.session_state():
            # With external speech, generated != heard. Only a speaker receipt
            # of full drain can promote this turn into conversational context.
            if self.speech is None:
                self.robot._cache_context_turn(turn)
            self.robot.snapshot.completed += 1
            self.robot.snapshot.last_reply = self.text
        elif interrupted:
            self.robot.snapshot.cancelled += 1
        elif status != "discarded":
            self.robot.snapshot.failures += 1
        self.publish("turn.done", turn_id=self.turn_id, text=self.text, status=status,
                     first_text_ms=self.first_text_ms, cancel_source=cancel_source)
        self._reset_turn()

    @staticmethod
    def _provider_error(event):
        error = event.get("error") or {}
        return f"{error.get('code', 'provider_error')}: {str(error.get('message', 'error'))[:150]}"

    async def submit_text(self, text):
        if not self.ready or not self.socket:
            raise RobotUnavailableError("Realtime Session not ready")
        if self.speech_active or self.response_id or self.turn_id:
            raise RobotUnavailableError("Realtime busy; do not create a duplicate response")
        self.audio_turn_id = None
        self._create_turn(text)
        try:
            await self.socket.send({
                "type": "conversation.item.create",
                "item": {"type": "message", "role": "user",
                         "content": [{"type": "input_text", "text": text}]},
            })
            await self.socket.send({"type": "response.create", "response": {"modalities": ["text"]}})
        except Exception:
            self._finish("failed", cancel_source="send_failed")
            raise
        return {"status": "accepted", "turn_id": self.turn_id, **self.robot.session_state()}

    async def cancel(self):
        if self.speech:
            await self.speech.cancel("local_cancel")
        if self.socket and self.ready and self.response_id:
            await self.socket.send({"type": "response.cancel"})
        if self.turn_id:
            self._finish("cancelled", cancel_source="local")
        self.publish("turn.cancel_requested")
        return self.snapshot()

    async def handle(self, event, generation):
        if generation != self.robot.generation:
            return
        kind = event.get("type")
        if kind == "error":
            raise RuntimeError(self._provider_error(event))
        if kind == "input_audio_buffer.speech_started":
            self.speech_active = True
            self.audio_turn_id = None
            self.publish("speech.started", item_id=event.get("item_id"))
        elif kind == "input_audio_buffer.speech_stopped":
            invalid = event.get("reason") == "turn_invalid"
            if invalid:
                self.speech_active = False
            self.publish("speech.stopped", invalid=invalid, item_id=event.get("item_id"))
        elif kind == "conversation.item.ambient_audio_transcription.completed":
            self.publish("transcript.ambient", text=event.get("transcript", ""))
        elif kind == "conversation.item.input_audio_transcription.delta":
            self.publish("transcript.delta", text=event.get("text", ""), stash=event.get("stash", ""))
        elif kind == "conversation.item.input_audio_transcription.completed":
            self.transcript = event.get("transcript", "")
            if self.transcript.strip() and not self.turn_id:
                # A final transcript can arrive after response.done; update its
                # audio turn rather than creating a duplicate completed turn.
                if self.audio_turn_id is None:
                    self._create_turn(self.transcript, audio=True)
            target = self.audio_turn_id
            if self.transcript.strip() and target:
                cached = self.robot.turn_cache.get(target)
                if cached is not None and cached.get("user_text") != self.transcript:
                    now = time.time_ns() // 1_000_000
                    cached.update(user_text=self.transcript, updated_ms=now)
                    for context in self.robot.context_cache:
                        if context["turn_id"] == target:
                            context["user_text"] = self.transcript
                    self.robot.ledger.enqueue(
                        "set_conversation_turn_user_text",
                        self.robot.config.robot_id, target, self.transcript, now,
                    )
            self.publish("transcript.done", text=self.transcript, turn_id=target)
        elif kind == "response.created":
            self.response_id = (event.get("response") or {}).get("id")
            self.publish("response.created", response_id=self.response_id)
        elif kind == "response.text.delta":
            if not self.turn_id:
                self._create_turn(self.transcript, audio=True)
            chunk = event.get("delta", "")
            self.text += chunk
            if self.first_text_ms is None and self.began is not None:
                self.first_text_ms = round((time.perf_counter() - self.began) * 1000, 2)
            self.publish("text.delta", turn_id=self.turn_id, delta=chunk, text=self.text)
            if self.speech and chunk:
                await self.speech.feed(self.turn_id, self.turn_identity, chunk)
        elif kind == "response.text.done":
            complete = event.get("text", self.text)
            if self.speech and not self.text and complete and self.turn_id:
                await self.speech.feed(self.turn_id, self.turn_identity, complete)
            self.text = complete
            self.publish("text.done", turn_id=self.turn_id, text=self.text)
        elif kind == "response.function_call_arguments.done":
            await self._tool_call(event)
        elif kind == "response.done":
            response = event.get("response") or {}
            status = response.get("status", "failed")
            self.response_id = None
            if self.followup_needed and status == "completed":
                self.followup_needed = False
                await self.socket.send({"type": "response.create", "response": {"modalities": ["text"]}})
                self.publish("tool.followup")
                return
            if self.turn_id:
                if self.speech:
                    if status == "completed":
                        await self.speech.finish(self.turn_id)
                    else:
                        await self.speech.cancel("response_failed")
                self._finish(status if status in {"completed", "cancelled"} else "failed",
                             cancel_source=(response.get("status_details") or {}).get("reason"))
            else:
                self._reset_turn()

    async def _tool_call(self, event):
        if self.turn_id is None or self.socket is None or not self.ready:
            return
        call_id, name = event.get("call_id"), event.get("name")
        if not call_id or call_id in self.calls:
            self.publish("tool.duplicate", call_id=call_id)
            return
        self.calls.add(call_id)
        self.call_count += 1
        arguments = event.get("arguments", "")
        try:
            if self.call_count > 2:
                raise ValueError("tool call budget exceeded")
            if name != "preview_action":
                raise ValueError("unsupported tool")
            if not isinstance(arguments, str) or len(arguments) > 4096:
                raise ValueError("invalid arguments size")
            args = json.loads(arguments)
            if not isinstance(args, dict) or set(args) != {"description"}:
                raise ValueError("expected exactly description")
            desc = args["description"]
            if not isinstance(desc, str) or not 1 <= len(desc.strip()) <= 100:
                raise ValueError("description must be 1..100 characters")
            result = {"status": "simulated", "description": desc, "real_device_executed": False}
            valid = True
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            result = {"status": "rejected", "reason": str(exc), "real_device_executed": False}
            valid = False
        self.publish("tool.result", call_id=call_id, name=name,
                     arguments=arguments, result=result, valid=valid)
        await self.socket.send({
            "type": "conversation.item.create",
            "item": {"type": "function_call_output", "call_id": call_id,
                     "output": json.dumps(result, ensure_ascii=False)},
        })
        self.followup_needed = True

"""Per-robot duplex Session: Qwen-Audio hears, optional external LLM answers."""

import asyncio
import json
import logging
import time
from collections import deque
from uuid import uuid4

from annie_adapters.realtime import QwenRealtimeSocket, audio_append, session_update
from annie_brain.context import select_restorable_context
from annie_brain.persona import session_instructions
from annie_core import RobotUnavailableError

logger = logging.getLogger(__name__)


class RealtimeRobot:
    """One provider Session, one current Chat/Persona, bounded events and PCM."""

    def __init__(self, robot, *, url: str, key: str, persona_profiles=None, external_llm=None,
                 socket_factory=QwenRealtimeSocket):
        self.robot = robot
        self.url = url
        self.key = key
        self.persona_profiles = persona_profiles or {}
        self.external_llm = external_llm
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
        self.audio_item_turns = {}
        self.pending_audio_item_id = None
        self.transcript = ""
        self.text = ""
        self.speech_active = False
        self.calls = set()
        self.call_count = 0
        self.followup_needed = False
        self.llm_task = None
        self.provider_text_ignored = 0
        self.llm_error = None
        self.first_text_ms = None
        self.began = None
        self.frames_sent = 0
        self.frames_dropped = 0
        self.session_frames_sent = 0
        self.speech_started_count = 0
        self.transcripts_completed = 0
        self.invalid_turns = 0
        self.last_provider_speech_ms = None
        self.last_provider_transcript_ms = None
        self.turn_identity = None
        self.restored_context = None
        self.persona_snapshot = None
        self.social_state = {}
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
            "session_frames_sent": self.session_frames_sent,
            "speech_started_count": self.speech_started_count,
            "transcripts_completed": self.transcripts_completed,
            "invalid_turns": self.invalid_turns,
            "last_provider_speech_ms": self.last_provider_speech_ms,
            "last_provider_transcript_ms": self.last_provider_transcript_ms,
            "first_text_ms": self.first_text_ms,
            "speech_output": self.speech.snapshot() if self.speech else {"state": "not_configured"},
            "restored_context": self.restored_context,
            "persona_snapshot": self.persona_snapshot,
            "cascade_llm": self.external_llm.model if self.external_llm else None,
            "llm_state": "streaming" if self.llm_task and not self.llm_task.done() else "idle",
            "llm_error": self.llm_error,
            "provider_text_ignored": self.provider_text_ignored,
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
        self.restored_context = None
        self.persona_snapshot = None
        self.audio_item_turns.clear()
        self.pending_audio_item_id = None
        if self.speech:
            await self.speech.close()
        if self.llm_task and not self.llm_task.done():
            self.llm_task.cancel()
            await asyncio.gather(self.llm_task, return_exceptions=True)
        self.llm_task = None
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

    async def _interrupt_active_turn(self, reason):
        task = self.llm_task
        self.llm_task = None
        if task and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        if self.speech and self.speech.turn_id:
            await self.speech.cancel(reason)
        if self.turn_id:
            self._finish("cancelled", cancel_source=reason)

    def _history_messages(self, user_text):
        instructions, _snapshot = session_instructions(
            self.robot.persona_id, self.persona_profiles, self.social_state,
        )
        messages = [{"role": "system", "content": instructions}]
        packet = select_restorable_context(
            self.robot.recent_context(), speech_enabled=self.speech is not None,
            max_turns=min(8, self.robot.context_limit),
        )
        for item in packet["turns"]:
            user = item["user_text"]
            reply = item["assistant_text"]
            if user:
                messages.append({"role": "user", "content": user})
            if reply:
                messages.append({"role": "assistant", "content": reply})
        messages.append({"role": "user", "content": user_text})
        # Never slice away system/Persona rules as history grows.
        return [messages[0], *messages[1:][-16:]]

    @staticmethod
    def _parse_preview_action(call):
        if not isinstance(call, dict) or call.get("name") != "preview_action":
            return {"status": "rejected", "reason": "unsupported tool", "real_device_executed": False}
        try:
            args = json.loads(call.get("arguments") or "{}")
            desc = args["description"]
            if not isinstance(desc, str) or not 1 <= len(desc.strip()) <= 100:
                raise ValueError("description must be 1..100 characters")
            return {"status": "simulated", "description": desc.strip(), "real_device_executed": False}
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            return {"status": "rejected", "reason": str(exc), "real_device_executed": False}

    async def _start_external_turn(self, user_text):
        if not self.external_llm or not self.turn_id:
            return
        if self.llm_task and not self.llm_task.done():
            self.llm_task.cancel()
            await asyncio.gather(self.llm_task, return_exceptions=True)
        self.llm_error = None
        if self.error and self.error.startswith("cascade_llm:"):
            self.error = None
        self.llm_task = asyncio.create_task(
            self._run_external_llm(self.turn_id, dict(self.turn_identity), user_text),
            name=f"cascade-llm:{self.robot.config.robot_id}",
        )

    async def _run_external_llm(self, turn_id, identity, user_text):
        parts = []
        tool_results = []
        messages = self._history_messages(user_text)
        try:
            async for item in self.external_llm.stream(messages, tools=True):
                if self.turn_id != turn_id or self.turn_identity != identity:
                    return
                if item.type == "text" and item.text:
                    parts.append(item.text)
                    self.text += item.text
                    if self.first_text_ms is None and self.began is not None:
                        self.first_text_ms = round((time.perf_counter() - self.began) * 1000, 2)
                    self.publish("text.delta", turn_id=turn_id, delta=item.text, text=self.text)
                    if self.speech:
                        await self.speech.feed(turn_id, identity, item.text)
                elif item.type == "tool_call":
                    result = self._parse_preview_action(item.tool_call)
                    tool_results.append((item.tool_call or {}, result))
                    self.publish("tool.result", call_id=(item.tool_call or {}).get("id"),
                                 name=(item.tool_call or {}).get("name"),
                                 arguments=(item.tool_call or {}).get("arguments"),
                                 result=result, valid=result.get("status") == "simulated")
            if tool_results and not parts:
                # Tool-only stream is not a spoken answer. Give the model the
                # simulated result exactly once; never invent real device work.
                messages.append({"role": "assistant", "content": None,
                                 "tool_calls": [{"id": call.get("id") or f"preview-{i}",
                                                 "type": "function",
                                                 "function": {"name": call.get("name") or "preview_action",
                                                              "arguments": call.get("arguments") or "{}"}}
                                                for i, (call, _result) in enumerate(tool_results)]})
                messages.extend({"role": "tool", "tool_call_id": call.get("id") or f"preview-{i}",
                                 "content": json.dumps(result, ensure_ascii=False)}
                                for i, (call, result) in enumerate(tool_results))
                async for item in self.external_llm.stream(messages, tools=False):
                    if self.turn_id != turn_id or self.turn_identity != identity:
                        return
                    if item.type != "text" or not item.text:
                        continue
                    parts.append(item.text)
                    self.text += item.text
                    self.publish("text.delta", turn_id=turn_id, delta=item.text, text=self.text)
                    if self.speech:
                        await self.speech.feed(turn_id, identity, item.text)
            if not parts:
                raise RuntimeError("LLM returned no answer text")
            self.text = "".join(parts).strip() or self.text
            self.publish("text.done", turn_id=turn_id, text=self.text)
            if self.speech:
                await self.speech.finish(turn_id)
            self._finish("completed")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.error = f"cascade_llm: {type(exc).__name__}: {str(exc)[:140]}"
            self.llm_error = self.error
            logger.warning("cascade_llm robot=%s failed: %s", self.robot.config.robot_id, self.error)
            if self.speech:
                await self.speech.cancel("cascade_llm_error")
            self.publish("llm.error", error=self.error)
            self._finish("failed", cancel_source="cascade_llm_error")

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
                social = await asyncio.to_thread(
                    self.robot.store.load_social_state,
                    self.robot.config.robot_id, self.robot.chat_id, self.robot.persona_id,
                    time.time_ns() // 1_000_000,
                )
                self.social_state = social
                instructions, persona_snapshot = session_instructions(
                    self.robot.persona_id, self.persona_profiles, social,
                )
                if self.external_llm:
                    instructions = (
                        "你只作为实时听觉、ASR 与 smart_turn 协处理器。"
                        "不要正式回答用户；如必须产生输出，只输出一个句号。"
                        "最终回复由外部流式 LLM 与独立 TTS 完成。"
                    )
                await self.socket.send(session_update(
                    instructions,
                    self.robot.context_limit,
                    sensor_only=bool(self.external_llm),
                ))
                # An invalid combined capability must fail visibly before audio enters the model.
                while True:
                    event = await asyncio.wait_for(self.socket.recv(), 8)
                    if event.get("type") == "error":
                        raise RuntimeError(self._provider_error(event))
                    if event.get("type") == "session.updated":
                        config = event.get("session") or {}
                        expected_modalities = ["text", "audio"] if self.external_llm else ["text"]
                        if config.get("modalities") != expected_modalities or (
                            (config.get("turn_detection") or {}).get("type") != "smart_turn"
                        ):
                            raise RuntimeError("Provider did not confirm requested smart_turn session")
                        self.session = config.get("id")
                        break
                if generation != self.robot.generation:
                    continue
                self.audio_item_turns.clear()
                self.pending_audio_item_id = None
                self.session_frames_sent = self.speech_started_count = 0
                self.transcripts_completed = self.invalid_turns = 0
                self.last_provider_speech_ms = self.last_provider_transcript_ms = None
                self.persona_snapshot = {**self.robot.session_state(), **persona_snapshot,
                                         "provider_session_id": self.session}
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
                self.restored_context = None
                self.persona_snapshot = None
                if self.llm_task and not self.llm_task.done():
                    self.llm_task.cancel()
                    await asyncio.gather(self.llm_task, return_exceptions=True)
                self.llm_task = None
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
        # This is the history injected into THIS socket, not its subsequent
        # live dialogue (which the provider does not expose through this API).
        self.restored_context = None
        packet = select_restorable_context(
            self.robot.recent_context(), speech_enabled=self.speech is not None,
            max_turns=self.robot.context_limit,
        )
        for turn in packet["turns"]:
            await self.socket.send({
                "type": "conversation.item.create",
                "item": {"type": "message", "role": "user",
                         "content": [{"type": "input_text", "text": turn["user_text"]}]},
            })
            if turn["assistant_text"]:
                await self.socket.send({
                    "type": "conversation.item.create",
                    "item": {"type": "message", "role": "assistant",
                             "content": [{"type": "output_text", "text": turn["assistant_text"]}]},
                })
        self.restored_context = {**self.robot.session_state(), **packet,
                                 "provider_session_id": self.session}

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
                self.session_frames_sent += 1
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
            self.robot._cache_context_turn(turn, allow_generated=self.speech is None)
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
        if self.speech_active or self.turn_id or (self.response_id and not self.external_llm):
            raise RobotUnavailableError("Realtime busy; do not create a duplicate response")
        self.audio_turn_id = None
        self._create_turn(text)
        if self.external_llm:
            await self._start_external_turn(text)
            return {"status": "accepted", "turn_id": self.turn_id, **self.robot.session_state()}
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
        if self.llm_task and not self.llm_task.done():
            self.llm_task.cancel()
            await asyncio.gather(self.llm_task, return_exceptions=True)
            self.llm_task = None
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
            self.pending_audio_item_id = event.get("item_id")
            self.speech_started_count += 1
            self.last_provider_speech_ms = time.time_ns() // 1_000_000
            self.publish("speech.started", item_id=event.get("item_id"))
        elif kind == "input_audio_buffer.speech_stopped":
            invalid = event.get("reason") == "turn_invalid"
            if invalid:
                self.speech_active = False
                self.invalid_turns += 1
            self.publish("speech.stopped", invalid=invalid, item_id=event.get("item_id"))
        elif kind == "conversation.item.ambient_audio_transcription.completed":
            self.publish("transcript.ambient", text=event.get("transcript", ""))
        elif kind == "conversation.item.input_audio_transcription.delta":
            self.publish("transcript.delta", text=event.get("text", ""), stash=event.get("stash", ""))
            if self.external_llm and (self.turn_id or self.response_id) and event.get("text"):
                self.publish("playback.hold_requested", source="smart_turn")
        elif kind == "conversation.item.input_audio_transcription.completed":
            self.transcripts_completed += 1
            self.last_provider_transcript_ms = time.time_ns() // 1_000_000
            self.transcript = event.get("transcript", "")
            final_text = self.transcript.strip()
            item_id = event.get("item_id")
            if self.external_llm and item_id and item_id in self.audio_item_turns:
                # Replayed provider final: neither restart LLM nor cancel this turn.
                self.publish("transcript.duplicate", item_id=item_id)
                return
            if self.external_llm and final_text:
                self.speech_active = False
                self.pending_audio_item_id = None
                if self.turn_id:
                    await self._interrupt_active_turn("valid_smart_turn")
                # _finish() resets self.transcript; retain the actual ASR final
                # across the old-turn cancellation before creating the new one.
                self.transcript = final_text
                self._create_turn(final_text, audio=True)
                if item_id:
                    self.audio_item_turns[item_id] = self.turn_id
                    if len(self.audio_item_turns) > 128:
                        self.audio_item_turns.pop(next(iter(self.audio_item_turns)))
                self.publish("transcript.done", text=final_text, turn_id=self.turn_id)
                await self._start_external_turn(final_text)
                return
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
            if self.external_llm:
                self.provider_text_ignored += 1
                return
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
            if self.external_llm:
                return
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
            if self.external_llm:
                return
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

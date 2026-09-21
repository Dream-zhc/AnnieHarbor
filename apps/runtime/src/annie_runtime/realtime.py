"""Per-robot duplex Session: Qwen-Audio hears, optional external LLM answers."""

import asyncio
import json
import logging
import math
import re
import time
from collections import deque
from uuid import uuid4

from annie_adapters.realtime import QwenRealtimeSocket, audio_append, session_update
from annie_brain.context import INTERRUPTED_USER_SOURCES, select_restorable_context
from annie_brain.persona import session_instructions
from annie_core import RobotUnavailableError

logger = logging.getLogger(__name__)
AMBIENT_FILLER = re.compile(r"^[\s嗯啊呃哦唔诶额哎呀哈呵噢,.，。!?！？~～…]+$")
AMBIENT_BACKCHANNELS = frozenset({"好", "好的", "对", "是", "行", "可以", "知道了", "哈哈", "呵呵"})
AMBIENT_CORRECTIONS = ("不对", "不是", "还没", "等等", "等下", "先别",
                       "我说", "你听错", "为什么", "怎么", "但是", "不过", "别", "停")


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
        self._cancelled_response_ids = set()
        self._response_output_ids = {}
        self._unheard_output_ids = {}
        self.turn_id = None
        self.audio_turn_id = None  # retain the audio turn across response.done for late final ASR
        self.audio_item_turns = {}
        self.pending_audio_item_id = None
        self.superseded_audio_items = {}
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
        self.last_asr_first_delta_ms = None
        self.asr_delta_count = 0
        self._candidate_confirmed = False
        self.turn_identity = None
        self.restored_context = None
        self.last_llm_input = None
        self.persona_snapshot = None
        self.social_state = {}
        self._stopping = False
        self._opening = False
        self._open_done = asyncio.Event()
        self._open_done.set()
        self._lifecycle = asyncio.Lock()
        self._keepalive_task = None
        self._last_activity_t = time.monotonic()
        self._keepalive_pending = False
        self._keepalive_items = set()
        self.keepalive_count = 0
        self.last_provider_stopped_ms = None
        self._turn_timelines = {}
        self._timeline_order = deque(maxlen=32)

    def _timeline_snapshot(self, turn_id):
        record = self._turn_timelines.get(turn_id)
        if record is None:
            return None
        times = dict(record["times"])
        pairs = {
            "speech_to_asr_final_ms": ("speech_started_ms", "asr_final_ms"),
            "endpoint_to_asr_final_ms": ("speech_stopped_ms", "asr_final_ms"),
            "llm_request_to_first_text_ms": ("llm_request_ms", "llm_first_text_ms"),
            "tts_submit_to_pcm_ms": ("tts_submit_ms", "tts_first_pcm_ms"),
            "room_to_speaker_start_ms": ("room_first_publish_ms", "speaker_started_ms"),
            "endpoint_to_speaker_start_ms": ("speech_stopped_ms", "speaker_started_ms"),
        }
        durations = {}
        for name, (start, end) in pairs.items():
            a, b = times.get(start), times.get(end)
            durations[name] = max(0, b - a) if a is not None and b is not None else None
        return {"turn_id": turn_id, "times": times, "durations": durations}

    def _promote_ambient(self, transcript):
        """Demo's conservative semantic fallback; local energy never starts a user turn."""
        cleaned = transcript.strip(" ，。！？!?…~～")
        if (not cleaned or AMBIENT_FILLER.fullmatch(cleaned) or
            len(re.findall(r"[\u4e00-\u9fffA-Za-z0-9]", cleaned)) < 2):
            return False
        compact = cleaned.replace(" ", "")
        if compact in AMBIENT_BACKCHANNELS:
            return False
        speaking = bool(self.turn_id or
                        (self.speech and self.speech.worker and
                         not self.speech.worker.done()))
        return not speaking or len(compact) >= 4 or any(
            marker in compact for marker in AMBIENT_CORRECTIONS)

    def _timeline_percentiles(self):
        fields = ("endpoint_to_speaker_start_ms", "llm_request_to_first_text_ms")
        result = {}
        for field in fields:
            values = sorted(value for turn in self._timeline_order
                            if (value := (self._timeline_snapshot(turn) or {})
                                .get("durations", {}).get(field)) is not None)
            result[field] = {
                "samples": len(values),
                "p50_ms": values[math.ceil(.50 * len(values)) - 1] if values else None,
                "p95_ms": values[math.ceil(.95 * len(values)) - 1] if values else None,
            }
        return result

    def publish(self, kind, **fields):
        if kind == "tts.playback.drained" and fields.get("source") == "speaker":
            self._unheard_output_ids.pop(fields.get("turn_id"), None)
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
        if kind == "turn.started" and fields.get("turn_id"):
            turn_id = fields["turn_id"]
            if len(self._timeline_order) == self._timeline_order.maxlen:
                self._turn_timelines.pop(self._timeline_order[0], None)
            self._timeline_order.append(turn_id)
            self._turn_timelines[turn_id] = {"times": {
                "speech_started_ms": fields.get("speech_started_ms"),
                "speech_stopped_ms": fields.get("speech_stopped_ms"),
                "asr_final_ms": fields.get("asr_final_ms"),
            }}
        else:
            slot = {
                "llm.request_started": "llm_request_ms",
                "text.delta": "llm_first_text_ms",
                "tts.text_submitted": "tts_submit_ms",
                "tts.first_audio": "tts_first_pcm_ms",
                "tts.first_published": "room_first_publish_ms",
                "tts.playback.started": "speaker_started_ms",
            }.get(kind)
            turn_id = fields.get("turn_id")
            if slot and turn_id in self._turn_timelines and (
                slot != "speaker_started_ms" or fields.get("source") == "speaker"
            ):
                self._turn_timelines[turn_id]["times"].setdefault(slot, event["ts_ms"])
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
            "last_asr_first_delta_ms": self.last_asr_first_delta_ms,
            "asr_delta_count": self.asr_delta_count,
            "first_text_ms": self.first_text_ms,
            "speech_output": self.speech.snapshot() if self.speech else {"state": "not_configured"},
            "restored_context": self.restored_context,
            "last_llm_input": self.last_llm_input,
            "persona_snapshot": self.persona_snapshot,
            "cascade_llm": self.external_llm.model if self.external_llm else None,
            "llm_state": "streaming" if self.llm_task and not self.llm_task.done() else "idle",
            "llm_error": self.llm_error,
            "provider_text_ignored": self.provider_text_ignored,
            "keepalive_count": self.keepalive_count,
            "keepalive_pending": self._keepalive_pending,
            "last_turn_timeline": self._timeline_snapshot(self._timeline_order[-1])
            if self._timeline_order else None,
            "latency_percentiles": self._timeline_percentiles(),
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
        self.speech_active = False
        self.restored_context = None
        self.last_llm_input = None
        self.persona_snapshot = None
        self.audio_item_turns.clear()
        self.superseded_audio_items.clear()
        self._cancelled_response_ids.clear()
        self._response_output_ids.clear()
        self._unheard_output_ids.clear()
        self._turn_timelines.clear()
        self._timeline_order.clear()
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
        # Finishing the old answer cannot reset a newer microphone candidate.
        self.speech_active = bool(self.pending_audio_item_id) and self.speech_active
        self.calls.clear()
        self.call_count = 0
        self.followup_needed = False
        self.turn_identity = None

    def _obsolete_audio_item(self, item_id):
        if not item_id:
            return
        self.superseded_audio_items[item_id] = True
        if len(self.superseded_audio_items) > 128:
            self.superseded_audio_items.pop(next(iter(self.superseded_audio_items)))

    def _stale_audio_event(self, kind, item_id):
        if not item_id:
            return False  # Some provider events omit the ID; never invent one.
        stale = (item_id in self.superseded_audio_items or
                 kind != "conversation.item.input_audio_transcription.completed" and
                 item_id in self.audio_item_turns or
                 self.pending_audio_item_id is not None and
                 item_id != self.pending_audio_item_id)
        if stale:
            self.publish("transcript.stale", item_id=item_id, event_kind=kind,
                         pending_item_id=self.pending_audio_item_id)
        return stale

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
            max_turns=min(20, self.robot.context_limit),
        )
        for item in packet["turns"]:
            user = item["user_text"]
            reply = item["assistant_text"]
            if user:
                messages.append({"role": "user", "content": user})
            if reply:
                messages.append({"role": "assistant", "content": reply})
        messages.append({"role": "user", "content": user_text})
        # Context selection already bounds complete exchanges. Another
        # message-level slice can orphan an old assistant reply by dropping
        # its matching user message.
        return messages

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
        messages = self._history_messages(user_text)
        self.last_llm_input = {
            **self.turn_identity, "turn_id": self.turn_id,
            "user_text": user_text,
            "messages": [message.copy() for message in messages],
        }
        self.publish("llm.context_selected", turn_id=self.turn_id,
                     user_text=user_text, history_messages=len(messages) - 2)
        self.llm_task = asyncio.create_task(
            self._run_external_llm(self.turn_id, dict(self.turn_identity), messages),
            name=f"cascade-llm:{self.robot.config.robot_id}",
        )

    async def _run_external_llm(self, turn_id, identity, messages):
        parts = []
        tool_results = []
        self.publish("llm.request_started", turn_id=turn_id)
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
                if (self.last_llm_input is not None and
                    self.last_llm_input["turn_id"] == turn_id):
                    self.last_llm_input["tool_followup_messages"] = [
                        message.copy() for message in messages
                    ]
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
            if self.turn_id != turn_id or self.turn_identity != identity:
                return
            self.text = "".join(parts).strip() or self.text
            self.publish("text.done", turn_id=turn_id, text=self.text)
            if self.speech:
                await self.speech.finish(turn_id)
            if self.turn_id != turn_id or self.turn_identity != identity:
                return
            self._finish("completed")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if self.turn_id != turn_id or self.turn_identity != identity:
                return
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
                self.superseded_audio_items.clear()
                self._cancelled_response_ids.clear()
                self._response_output_ids.clear()
                self._unheard_output_ids.clear()
                self.pending_audio_item_id = None
                self.last_llm_input = None
                self.session_frames_sent = self.speech_started_count = 0
                self.transcripts_completed = self.invalid_turns = 0
                self.last_provider_speech_ms = self.last_provider_transcript_ms = None
                self.last_asr_first_delta_ms = None
                self.asr_delta_count = 0
                self._candidate_confirmed = False
                self.persona_snapshot = {**self.robot.session_state(), **persona_snapshot,
                                         "provider_session_id": self.session}
                await self._restore_context()
                self.ready = True
                self.error = None
                retry = 1
                self.publish("session.ready", model="qwen-audio-3.0-realtime-flash")
                self.audio_task = asyncio.create_task(self._audio_pump(generation))
                self._last_activity_t = time.monotonic()
                self._keepalive_pending = False
                if self.external_llm:
                    self._keepalive_task = asyncio.create_task(
                        self._idle_keepalive(generation), name=f"keepalive:{self.robot.config.robot_id}"
                    )
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
                if self._keepalive_task:
                    self._keepalive_task.cancel()
                    await asyncio.gather(self._keepalive_task, return_exceptions=True)
                    self._keepalive_task = None
                self._keepalive_pending = False
                self._keepalive_items.clear()
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

    async def _idle_keepalive(self, generation):
        """Sensor-only app heartbeat; a normal speaking Realtime must never get an unsolicited reply."""
        while self.ready and not self._stopping and generation == self.robot.generation:
            await asyncio.sleep(5)
            await self._maybe_keepalive(generation)

    async def _maybe_keepalive(self, generation):
        if (not self.external_llm or not self.ready or self._stopping or
            generation != self.robot.generation or self._keepalive_pending or
            self.speech_active or self.turn_id or self.response_id or
            time.monotonic() - self._last_activity_t < 120 or self.socket is None):
            return False
        # The configured sensor_only Session returns a discarded punctuation response.
        # Do not emit a user turn or invoke the independent LLM/TTS.
        self._keepalive_pending = True
        try:
            await self.socket.send({"type": "response.create"})
        except BaseException:
            self._keepalive_pending = False
            raise
        self.keepalive_count += 1
        self.publish("session.keepalive", count=self.keepalive_count)
        return True

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
        self.publish("turn.started", turn_id=turn_id, user_text=user_text,
                     speech_started_ms=(
                         self.last_provider_speech_ms if audio and
                         self.last_provider_speech_ms is not None and
                         0 <= now - self.last_provider_speech_ms < 30000 else None
                     ),
                     speech_stopped_ms=(
                         self.last_provider_stopped_ms if audio and
                         self.last_provider_stopped_ms is not None and
                         0 <= now - self.last_provider_stopped_ms < 30000 else None
                     ),
                     asr_final_ms=now if audio else None)

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
            if (turn is not None and self.speech is not None
                and cancel_source in INTERRUPTED_USER_SOURCES
                and self.turn_identity == self.robot.session_state()):
                # The confirmed user spoke; the cancelled assistant did not
                # necessarily speak. Keep the user input but no assistant text.
                self.robot._cache_context_turn(turn, allow_generated=False)
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
        # Mark ownership before any await: a late response.done must not
        # complete the next turn while the speaker is stopping.
        cancelled_response = self.response_id
        completed_unheard = (self._unheard_output_ids.pop(self.speech.turn_id, set())
                             if self.speech and self.speech.turn_id else set())
        if cancelled_response:
            self._cancelled_response_ids.add(cancelled_response)
            if len(self._cancelled_response_ids) > 128:
                self._cancelled_response_ids.pop()
            self.response_id = None
        if self.socket and self.ready and cancelled_response:
            await self.socket.send({"type": "response.cancel"})
        if self.llm_task and not self.llm_task.done():
            self.llm_task.cancel()
            await asyncio.gather(self.llm_task, return_exceptions=True)
            self.llm_task = None
        if self.speech:
            await self.speech.cancel("local_cancel")
        if self.socket and self.ready and completed_unheard:
            await self._delete_unheard_items(completed_unheard)
        if self.turn_id:
            self._finish("cancelled", cancel_source="local")
        self.publish("turn.cancel_requested")
        return self.snapshot()

    async def _delete_unheard_items(self, item_ids):
        # Qwen supports item deletion, not playback-cursor text truncation.
        # An item with unknown partial playback is wholly excluded rather than
        # falsely claiming that its generated tail was spoken.
        for item_id in sorted(item_ids):
            await self.socket.send({"type": "conversation.item.delete", "item_id": item_id})
        self.publish("context.unheard_delete_requested", count=len(item_ids),
                     provider_confirmed=False)

    async def handle(self, event, generation):
        if generation != self.robot.generation:
            return
        kind = event.get("type")
        if kind == "error":
            raise RuntimeError(self._provider_error(event))
        if kind == "response.done" and not self.external_llm:
            response = event.get("response") or {}
            response_id = response.get("id")
            items = self._response_output_ids.pop(response_id, set()) if response_id else set()
            items.update(item["id"] for item in (response.get("output") or [])
                         if isinstance(item, dict) and item.get("id") and
                         item.get("type") == "message" and item.get("role") == "assistant")
            if response.get("status") == "cancelled":
                if items:
                    await self._delete_unheard_items(items)
                elif self.turn_id or response_id in self._cancelled_response_ids:
                    self.publish("context.unheard_unverified", response_id=response_id)
            elif (response.get("status") == "completed" and items and self.speech
                  and self.turn_id and response_id not in self._cancelled_response_ids):
                self._unheard_output_ids[self.turn_id] = items
                if len(self._unheard_output_ids) > 32:
                    self._unheard_output_ids.pop(next(iter(self._unheard_output_ids)))
        if kind in {"response.created", "response.text.delta", "response.text.done",
                    "response.function_call_arguments.done", "response.done"}:
            response_id = (event.get("response") or {}).get("id") if kind in {
                "response.created", "response.done"
            } else event.get("response_id")
            if response_id and (response_id in self._cancelled_response_ids or
                                kind != "response.created" and self.response_id and
                                response_id != self.response_id):
                self.publish("response.stale", response_id=response_id, event_kind=kind)
                return
        if kind == "input_audio_buffer.speech_started":
            self._last_activity_t = time.monotonic()
            new_item_id = event.get("item_id")
            if new_item_id and new_item_id == self.pending_audio_item_id and self.speech_active:
                self.publish("speech.duplicate", item_id=new_item_id)
                return
            if new_item_id and (new_item_id in self.audio_item_turns or
                                new_item_id in self.superseded_audio_items):
                self.publish("transcript.stale", item_id=new_item_id,
                             event_kind=kind, pending_item_id=self.pending_audio_item_id)
                return
            if self.pending_audio_item_id and self.pending_audio_item_id != new_item_id:
                self._obsolete_audio_item(self.pending_audio_item_id)
            self.speech_active = True
            self._candidate_confirmed = False
            self.last_asr_first_delta_ms = None
            self.asr_delta_count = 0
            self.pending_audio_item_id = new_item_id
            self.speech_started_count += 1
            self.last_provider_speech_ms = time.time_ns() // 1_000_000
            self.publish("speech.started", item_id=event.get("item_id"))
            # A finished LLM turn may still be speaking. Freeze its local
            # playback without cancelling TTS until smart_turn accepts speech.
            if self.external_llm and self.speech and self.speech.turn_id:
                self.publish("playback.hold_requested", turn_id=self.speech.turn_id,
                             source="smart_turn")
                self.speech.request_playback_control("hold", reason="speech_started")
        elif kind == "input_audio_buffer.speech_stopped":
            if self._stale_audio_event(kind, event.get("item_id")):
                return
            invalid = event.get("reason") == "turn_invalid"
            if invalid:
                self.speech_active = False
                self.invalid_turns += 1
                self.last_provider_stopped_ms = None
            else:
                self.last_provider_stopped_ms = time.time_ns() // 1_000_000
            self.publish("speech.stopped", invalid=invalid, item_id=event.get("item_id"))
            if (invalid and not self._candidate_confirmed and self.external_llm
                and self.speech and self.speech.turn_id):
                self.publish("playback.resume_requested", turn_id=self.speech.turn_id,
                             reason="turn_invalid")
                self.speech.request_playback_control("resume", reason="turn_invalid")
        elif kind == "conversation.item.ambient_audio_transcription.completed":
            if self._stale_audio_event(kind, event.get("item_id")):
                return
            final_text = str(event.get("transcript", "")).strip()
            item_id = event.get("item_id")
            if (self.external_llm and final_text and
                (not item_id or item_id not in self.audio_item_turns) and
                self._promote_ambient(final_text)):
                self.publish("transcript.ambient_promoted", text=final_text, item_id=item_id)
                self.publish("transcript.final_preview", text=final_text, item_id=item_id)
                self.speech_active = False
                if self.turn_id:
                    await self._interrupt_active_turn("meaningful_ambient")
                self.transcript = final_text
                self._create_turn(final_text, audio=True)
                if item_id:
                    self.audio_item_turns[item_id] = self.turn_id
                self.pending_audio_item_id = None
                self.publish("transcript.done", text=final_text, turn_id=self.turn_id)
                await self._start_external_turn(final_text)
                return
            # Demo: an ambient-only outcome rejects a speculative hold. Qwen
            # need not emit turn_invalid first; without this the TTS player can
            # remain silently HELD while the microphone keeps hearing it.
            if (self.external_llm and self.speech and self.speech.turn_id and
                getattr(self.speech, "control_state", "") in {
                    "hold_requested", "held_confirmed",
                }):
                self.publish("playback.resume_requested", turn_id=self.speech.turn_id,
                             reason="ambient_audio")
                self.speech.request_playback_control("resume", reason="ambient_audio")
            self.speech_active = False
            self.pending_audio_item_id = None
            self.publish("transcript.ambient", text=final_text)
        elif kind == "conversation.item.ambient_audio_transcription.delta":
            if self._stale_audio_event(kind, event.get("item_id")):
                return
            self.publish("transcript.ambient_delta", text=event.get("text", ""),
                         stash=event.get("stash", ""), item_id=event.get("item_id"))
        elif kind == "conversation.item.input_audio_transcription.delta":
            if self._stale_audio_event(kind, event.get("item_id")):
                return
            if self.last_asr_first_delta_ms is None:
                self.last_asr_first_delta_ms = time.time_ns() // 1_000_000
                self.publish("transcript.first_delta",
                             after_speech_ms=(self.last_asr_first_delta_ms-self.last_provider_speech_ms)
                             if self.last_provider_speech_ms else None)
            self.asr_delta_count += 1
            self.publish("transcript.delta", text=event.get("text", ""),
                         stash=event.get("stash", ""), item_id=event.get("item_id"))
            stable = str(event.get("text") or "").strip()
            # Demo v1.7 confirms an interruption from the stable text only,
            # never speculative stash or local microphone energy.
            meaningful = len(re.findall(r"[\u4e00-\u9fffA-Za-z0-9]", stable)) >= 3
            if (meaningful and not AMBIENT_FILLER.fullmatch(stable)
                and not self._candidate_confirmed and self.external_llm and self.speech
                and self.speech.turn_id and getattr(self.speech, "control_state", "") in {
                    "hold_requested", "held_confirmed",
                }):
                self._candidate_confirmed = True
                self.publish("playback.interrupt_confirmed",
                             turn_id=self.speech.turn_id, reason="stable_asr_prefix")
                await self._interrupt_active_turn("stable_asr_prefix")
                self.speech_active = True  # next ASR final still owns the microphone turn
        elif kind == "conversation.item.input_audio_transcription.completed":
            if self._stale_audio_event(kind, event.get("item_id")):
                return
            self._last_activity_t = time.monotonic()
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
                # The demo shows ASR final before waiting for old TTS stop / LLM
                # cancellation; UI must not inherit the stop round-trip latency.
                self.publish("transcript.final_preview", text=final_text, item_id=item_id)
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
        elif kind == "response.output_item.added":
            if not self.external_llm:
                item = event.get("item") or {}
                response_id = event.get("response_id") or self.response_id
                if (response_id and item.get("id") and item.get("type") == "message"
                        and item.get("role") == "assistant"):
                    self._response_output_ids.setdefault(response_id, set()).add(item["id"])
                    if len(self._response_output_ids) > 32:
                        self._response_output_ids.pop(next(iter(self._response_output_ids)))
            if self.external_llm and self._keepalive_pending:
                item = event.get("item") or {}
                if item.get("role") == "assistant" and item.get("id"):
                    self._keepalive_items.add(item["id"])
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
            self._last_activity_t = time.monotonic()
            if self.external_llm:
                if self._keepalive_pending:
                    self._keepalive_pending = False
                    for item_id in self._keepalive_items:
                        await self.socket.send({
                            "type": "conversation.item.delete", "item_id": item_id,
                        })
                    self._keepalive_items.clear()
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

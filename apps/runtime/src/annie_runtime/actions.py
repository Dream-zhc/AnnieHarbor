"""Phase 7: one-robot, one-Room action dispatch with durable idempotency.

Every action goes out as action_trigger, the only motion message the embedded
player runs. Legacy actions use the first published PCM; word actions wait
until the real reply contains the requested word and its first PCM is out.
Published is UNKNOWN, never a claim that the embedded motor finished; only the
device's reb_main action.started / action.finished / action.dropped receipts
(matched by conversation_id + script_id) move an action past that.
"""

import asyncio
import re
import time

from annie_brain.action_catalog import resolve_action

EXPRESSIONS = frozenset({"喜", "怒", "哀", "乐", "中"})
WORD_REQUIRED_SCRIPTS = frozenset({"surprise", "smirk", "proud", "skeptical", "shy"})
ANCHOR_TIMEOUT = 15
# Embedded receipt -> durable status. finished is what the brain trusts as done;
# dropped carries the device's reason (unknown script, expired, cooldown, ...).
RECEIPT_STATUS = {"action.started": "device_started", "action.finished": "device_finished",
                  "action.dropped": "device_dropped"}
# Only an action we actually sent can advance; finished/dropped are final.
RECEIPT_FROM = {
    "device_started": frozenset({"send_unknown", "published_unknown"}),
    "device_finished": frozenset({"send_unknown", "published_unknown", "device_started"}),
    "device_dropped": frozenset({"send_unknown", "published_unknown", "device_started"}),
}
DEVICE_COMPLETION = {"device_started": "started", "device_finished": "finished",
                     "device_dropped": "dropped"}


def current_action_not_vetoed(text):
    """Minimal veto for a current, explicit cancellation; semantics belong to Qwen.

    Do not turn this into a second, handwritten Chinese language model.
    Identity, capability, cooldown and Room ownership remain hard gates.
    """
    if not isinstance(text, str):
        return False
    plain = text.strip()
    if not plain or len(plain) > 1500:
        return False
    return re.search(
        r"别动|不要|不想|不需要|不用|不准|别(?:看|点头|眨眼|做动作|做表情)"
        r"|不许|停止|取消|禁止", plain,
    ) is None


class ActionCoordinator:
    def __init__(self, store, catalog, robots, main_publishers, emit, *,
                 enabled=False, allowlist=()):
        self.store = store
        self.catalog = catalog
        self.robots = robots
        self.main_publishers = main_publishers
        self.emit = emit
        self.enabled = enabled
        self.allowlist = tuple(allowlist)
        self.pending = {}  # (robot_id,turn_id) -> (call_id,session identity)
        self.word_anchors = {}  # same key -> bounded, current speech/word identity
        self.word_sends = {}  # (robot_id,call_id) -> (turn_id, cancellable anchor)
        self.word_turns = {}  # robot -> current authoritative turn.started identity
        self.pending_expressions = {}  # (robot_id,call_id) -> turn_id
        self.tasks = set()
        self.connection_errors = {}
        self.receipts = {"applied": 0, "ignored": 0, "unmatched": 0, "invalid": 0}
        self.locks = {robot_id: asyncio.Lock() for robot_id in robots}

    async def start(self):
        await asyncio.to_thread(self.store.recover_actions)
        if self.enabled:
            for robot_id, publisher in self.main_publishers.items():
                if hasattr(publisher, "open"):
                    self._schedule(self._warm(robot_id, publisher))

    async def _warm(self, robot_id, publisher):
        try:
            await asyncio.wait_for(publisher.open(), 5)
            self.connection_errors.pop(robot_id, None)
        except Exception as exc:
            self.connection_errors[robot_id] = type(exc).__name__

    async def list(self, robot_id, limit=50):
        if robot_id not in self.robots:
            raise KeyError(robot_id)
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("invalid action history limit")
        return await asyncio.to_thread(self.store.list_actions, robot_id, limit)

    def _event(self, row, *, status=None, error=None, executed=False, **receipt):
        status = status or row["status"]
        self.emit(
            row["robot_id"], "action.state", turn_id=row["turn_id"],
            call_id=row["call_id"], action_kind=row["kind"], action_value=row["value"],
            room_id=row["room_id"], generation=row["generation"],
            status=status, error=error,
            real_device_executed=executed,
            device_completion=DEVICE_COMPLETION.get(status, "unknown"), **receipt,
        )

    def on_receipt(self, robot_id, message):
        """reb_main action.started/finished/dropped from the embedded player."""
        after = RECEIPT_STATUS.get(message.get("type"))
        data = message.get("data")
        if after is None:
            return False
        script = data.get("script_id") if isinstance(data, dict) else None
        cid = data.get("conversation_id") if isinstance(data, dict) else None
        if (robot_id not in self.robots or not isinstance(script, str)
                or not 1 <= len(script) <= 64 or not isinstance(cid, str)
                or not 1 <= len(cid) <= 128):
            self.receipts["invalid"] += 1
            return False
        reason = data.get("reason")
        detail = {"device_" + key: data[key] for key in ("offset_ms", "duration_ms")
                  if type(data.get(key)) in (int, float)}
        if type(message.get("ts")) is int:
            detail["device_ts_ms"] = message["ts"]
        self._schedule(self._apply_receipt(
            robot_id, cid, script, after,
            reason[:64] if isinstance(reason, str) and reason else None, detail,
        ))
        return True

    async def _apply_receipt(self, robot_id, cid, script, after, reason, detail):
        async with self.locks[robot_id]:
            row = await asyncio.to_thread(self.store.find_action, robot_id, cid, "action", script)
            if row is None:
                self.receipts["unmatched"] += 1
                self.emit(robot_id, "action.receipt_unmatched", turn_id=cid,
                          action_value=script, receipt_status=after, reason=reason)
                return
            before = row["status"]
            changed = before in RECEIPT_FROM[after] and await asyncio.to_thread(
                self.store.set_action_status, robot_id, row["call_id"], before, after, reason)
            if not changed:
                # e.g. a started arriving after finished: never move backwards.
                self.receipts["ignored"] += 1
                return
            self.receipts["applied"] += 1
            # A drop after started still moved the motor part of the way.
            self._event(row, status=after, error=reason,
                        executed=after != "device_dropped" or before == "device_started",
                        **detail)

    async def request(self, robot_id, call_id, turn_id, generation, kind, value, *,
                      speech, user_text, anchor_text=None, anchor_occurrence=1):
        if not self.enabled:
            return {"status": "not_configured", "real_device_executed": False}
        if robot_id not in self.robots or speech is None or robot_id not in self.main_publishers:
            return {"status": "not_configured", "reason": "room_or_tts_missing",
                    "real_device_executed": False}
        if (not isinstance(call_id, str) or not 1 <= len(call_id) <= 128 or
                not isinstance(turn_id, str) or not turn_id or
                type(generation) is not int):
            raise ValueError("invalid action turn identity")
        if not current_action_not_vetoed(user_text):
            raise ValueError("physical action vetoed by current user")
        if kind == "action":
            if value not in self.allowlist:
                raise ValueError("action is not enabled for physical execution")
            action = resolve_action(self.catalog, value)
            cooldown = action["duration_ms"]
        elif kind == "expression" and type(value) is str and value in EXPRESSIONS:
            cooldown = 0
        else:
            raise ValueError("unknown expression/action kind")
        if anchor_text is not None and (
            kind != "action" or not isinstance(anchor_text, str)
            or not anchor_text.strip() or len(anchor_text) > 80
            or type(anchor_occurrence) is not int or not 1 <= anchor_occurrence <= 8000
        ):
            raise ValueError("invalid speech anchor")
        if anchor_text is None and anchor_occurrence != 1:
            raise ValueError("anchor_occurrence requires anchor_text")
        # All five expressive scripts require true acoustic word timing. The
        # Runtime cannot reliably distinguish an explicit request from an
        # autonomous suggestion; do not fall back to first-PCM in either case.
        if kind == "action" and value in WORD_REQUIRED_SCRIPTS and anchor_text is None:
            raise ValueError("expressive action requires anchor_text")
        waiting_status = "awaiting_text_anchor" if anchor_text is not None else "awaiting_audio_anchor"
        robot = self.robots[robot_id]
        if generation != robot.generation:
            raise ValueError("stale_generation")
        async with self.locks[robot_id]:
            # turn.started revokes synchronously, while durable cleanup is
            # asynchronous. Finish that cleanup here if a new tool wins the lock.
            retired = [key for key, anchor in self.word_anchors.items()
                       if key[0] == robot_id and anchor["cancelled"]]
            for key in retired:
                self.word_anchors.pop(key, None)
                pending = self.pending.pop(key, None)
                if pending is not None:
                    changed = await asyncio.to_thread(
                        self.store.set_action_status, robot_id, pending[0],
                        "awaiting_text_anchor", "cancelled_before_send",
                    )
                    if changed:
                        self.emit(robot_id, "action.state", turn_id=key[1], call_id=pending[0],
                                  status="cancelled_before_send", real_device_executed=False,
                                  device_completion="unknown")
            if kind == "action":
                # One script per turn even after its physical cooldown elapsed.
                previous = await asyncio.to_thread(self.store.list_actions, robot_id, 100)
                if any(item["turn_id"] == turn_id and item["kind"] == "action"
                       and item["call_id"] != call_id and item["value"] != value
                       for item in previous):
                    return {"status": "conflict_or_cooldown", "real_device_executed": False}
            row, duplicate = await asyncio.to_thread(
                self.store.reserve_action, robot_id, call_id, turn_id, generation,
                robot.config.room_id, kind, value, cooldown,
                word_aligned=anchor_text is not None,
            )
            if row is None:
                return {"status": "conflict_or_cooldown", "real_device_executed": False}
            if duplicate:
                self._event(row, status="duplicate_" + row["status"])
                return {"status": "duplicate_" + row["status"],
                        "real_device_executed": False}
            self._event(row)
            if anchor_text is not None and self.word_turns.get(robot_id, turn_id) != turn_id:
                await asyncio.to_thread(
                    self.store.set_action_status, robot_id, call_id,
                    waiting_status, "cancelled_before_send",
                )
                self._event(row, status="cancelled_before_send")
                return {"status": "cancelled_before_send", "real_device_executed": False}
            if kind == "action":
                if anchor_text is None and speech.turn_id == turn_id and (
                    speech.frames or getattr(speech, "inflight_end", 0)
                ):
                    await asyncio.to_thread(
                        self.store.set_action_status, robot_id, call_id, waiting_status,
                        "rejected_late",
                    )
                    self._event(row, status="rejected_late")
                    return {"status": "rejected_late", "real_device_executed": False}
                key = robot_id, turn_id
                self.pending[key] = (call_id, generation)
                if anchor_text is not None:
                    self.word_anchors[key] = {
                        "speech": speech, "identity": dict(robot.session_state()),
                        "epoch": speech.stream_epoch + (speech.turn_id != turn_id),
                        "text": anchor_text, "occurrence": anchor_occurrence, "cancelled": False,
                    }
                    # Realtime may have delivered the text and PCM before the
                    # tool call. Dispatch immediately when the exact word exists.
                    if (self._word_active(robot_id, turn_id, generation,
                                          self.word_anchors[key]) and
                            self._word_ready(self.word_anchors[key])):
                        self._schedule(self._dispatch_text(robot_id, turn_id, call_id,
                                                           generation, self.word_anchors[key]))
                self._schedule(self._expire(robot_id, turn_id, call_id, generation))
                return {"status": waiting_status, "script_id": value,
                        "room_id": robot.config.room_id,
                        "real_device_executed": False}
            # Expression goes out ahead of its corresponding first PCM, on
            # the SAME publisher socket as speech. No device ack is implied.
            if (speech.turn_id == turn_id and (
                    speech.frames or getattr(speech, "inflight_end", 0)) or
                    speech.turn_id not in (None, turn_id) and
                    getattr(speech, "worker", None) is not None and
                    not speech.worker.done()):
                await asyncio.to_thread(self.store.set_action_status, robot_id, call_id, "reserved",
                                        "rejected_late")
                self._event(row, status="rejected_late")
                return {"status": "rejected_late", "real_device_executed": False}
            self.pending_expressions[(robot_id, call_id)] = turn_id
            self._schedule(self._dispatch_expression(row, generation, speech))
            return {"status": "queued", "room_id": robot.config.room_id,
                    "real_device_executed": False, "device_completion": "unknown"}

    async def _dispatch_expression(self, row, generation, speech):
        """The Realtime tool response must never await a Room websocket."""
        robot_id, call_id, turn_id = row["robot_id"], row["call_id"], row["turn_id"]
        robot = self.robots[robot_id]
        async with self.locks[robot_id]:
            if self.pending_expressions.pop((robot_id, call_id), None) != turn_id:
                return
            if generation != robot.generation or getattr(speech, "cancelled", False):
                await asyncio.to_thread(
                    self.store.set_action_status, robot_id, call_id,
                    "reserved", "cancelled_before_send",
                )
                self._event(row, status="cancelled_before_send")
                return
            changed = await asyncio.to_thread(
                self.store.set_action_status, robot_id, call_id,
                "reserved", "send_unknown",
            )
            if not changed:
                return
        packet = {"type": "expression", "ts": time.time_ns() // 1_000_000,
                  "data": {"expression": row["value"], "turn_id": turn_id,
                           "source": "robot_main_brain"}}
        self._event(row, status="send_unknown")

        def expression_still_before_pcm():
            previous = speech.turn_id not in (None, turn_id)
            worker = getattr(speech, "worker", None)
            return (
                generation == robot.generation
                and not getattr(speech, "cancelled", False)
                and (not previous or worker is None or worker.done())
                and not (speech.turn_id == turn_id and (
                    speech.frames or getattr(speech, "inflight_end", 0)))
            )

        try:
            if hasattr(speech.publisher, "publish_if"):
                sent = await asyncio.wait_for(
                    speech.publisher.publish_if(packet, expression_still_before_pcm), 6,
                )
            elif expression_still_before_pcm():
                await asyncio.wait_for(speech.publisher.publish(packet), 6)
                sent = True
            else:
                sent = False
        except Exception as exc:
            await asyncio.to_thread(
                self.store.set_action_status, robot_id, call_id,
                "send_unknown", "send_unknown", type(exc).__name__,
            )
            self._event(row, status="send_unknown", error=type(exc).__name__)
            return
        if not sent:
            await asyncio.to_thread(
                self.store.set_action_status, robot_id, call_id,
                "send_unknown", "rejected_late",
            )
            self._event(row, status="rejected_late")
            return
        await asyncio.to_thread(
            self.store.set_action_status, robot_id, call_id,
            "send_unknown", "published_unknown",
        )
        self._event(row, status="published_unknown")

    def _schedule(self, awaitable):
        task = asyncio.create_task(awaitable)
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)

    async def _expire(self, robot_id, turn_id, call_id, generation):
        await asyncio.sleep(ANCHOR_TIMEOUT)
        if self.pending.get((robot_id, turn_id)) != (call_id, generation):
            return
        async with self.locks[robot_id]:
            if self.pending.pop((robot_id, turn_id), None) == (call_id, generation):
                word = self.word_anchors.pop((robot_id, turn_id), None)
                expiry = "expired_no_text_anchor" if word is not None else "expired_no_pcm"
                before = "awaiting_text_anchor" if word is not None else "awaiting_audio_anchor"
                changed = await asyncio.to_thread(
                    self.store.set_action_status, robot_id, call_id, before,
                    expiry,
                )
                if changed:
                    self.emit(robot_id, "action.state", turn_id=turn_id,
                              call_id=call_id, status=expiry,
                              real_device_executed=False)

    def on_first_pcm(self, event):
        key = event["robot_id"], event.get("turn_id")
        pending = self.pending.get(key)
        if not pending or pending[1] != event.get("generation"):
            return
        if key in self.word_anchors:
            self.on_text_ready(event)  # its word may already be in the reply
        else:
            self._schedule(self._dispatch(*key, *pending))

    def _word_ready(self, anchor):
        # The device only runs action_trigger on arrival (2026-10-22): a word
        # action waits for its word and for the reply's first published PCM.
        return bool(anchor["speech"].frames) and self._anchor_position(anchor) is not None

    @staticmethod
    def _anchor_position(anchor):
        speech = anchor["speech"]
        submitted = getattr(speech, "generated_text", "")
        # Index in the exact generated text, including spaces/punctuation;
        # never compute from audio durations or fabricate a future suffix.
        if not isinstance(submitted, str):
            return None
        offset = 0
        for _ in range(anchor["occurrence"]):
            position = submitted.find(anchor["text"], offset)
            if position < 0:
                return None
            offset = position + len(anchor["text"])
        return position

    def _word_active(self, robot_id, turn_id, generation, anchor):
        speech = anchor["speech"]
        robot = self.robots[robot_id]
        speaker = getattr(speech, "receipt_tracks", {}).get("speaker")
        return (
            generation == robot.generation and not speech.cancelled and not anchor["cancelled"]
            and self.word_turns.get(robot_id, turn_id) == turn_id
            and not getattr(speech, "error", None)
            and (speaker is None or speaker.get("state") not in {"stopped", "drained"})
            and speech.turn_id == turn_id and speech.stream_epoch == anchor["epoch"]
            and speech.identity == anchor["identity"]
            and robot.session_state() == anchor["identity"]
        )

    def on_turn_started(self, event):
        """Revoke old word work before this turn has produced text or PCM."""
        robot_id, turn_id = event.get("robot_id"), event.get("turn_id")
        if robot_id not in self.locks or not isinstance(turn_id, str) or not turn_id:
            return
        self.word_turns[robot_id] = turn_id
        for key, anchor in self.word_anchors.items():
            if key[0] == robot_id and key[1] != turn_id:
                anchor["cancelled"] = True
                self._schedule(self.cancel_pending(robot_id, key[1]))
        for key, (pending_turn, anchor) in self.word_sends.items():
            if key[0] == robot_id and pending_turn != turn_id:
                anchor["cancelled"] = True

    def on_text_ready(self, event):
        key = event.get("robot_id"), event.get("turn_id")
        pending, anchor = self.pending.get(key), self.word_anchors.get(key)
        if (pending and anchor is not None and
                self._word_active(*key, pending[1], anchor) and self._word_ready(anchor)):
            self._schedule(self._dispatch_text(*key, *pending, anchor))

    async def _dispatch_text(self, robot_id, turn_id, call_id, generation, anchor):
        key = robot_id, turn_id
        sending_key = robot_id, call_id
        owns_send = False
        try:
            async with self.locks[robot_id]:
                if self.pending.get(key) != (call_id, generation) or self.word_anchors.get(key) is not anchor:
                    return
                valid = self._word_active(robot_id, turn_id, generation, anchor)
                if valid and not anchor["speech"].frames:
                    return  # stays pending: the first PCM retries
                position = self._anchor_position(anchor) if valid else None
                after = "send_unknown" if position is not None else "cancelled_before_send"
                # Move the token atomically before the first durable-state await.
                # turn.started is synchronous and must see it even while SQLite
                # is transitioning awaiting_text_anchor -> send_unknown.
                if after == "send_unknown":
                    self.word_sends[sending_key] = turn_id, anchor
                    owns_send = True
                self.pending.pop(key, None)
                self.word_anchors.pop(key, None)
                changed = await asyncio.to_thread(
                    self.store.set_action_status, robot_id, call_id, "awaiting_text_anchor", after,
                )
                row = await asyncio.to_thread(self.store.get_action, robot_id, call_id)
                if not changed or row is None:
                    return
                self._event(row, status=after)
                if after != "send_unknown":
                    return
                # The embedded player ignores action_prepare (phoneme_anchor_v1);
                # it runs the same action_trigger as every other script.
                packet = {
                    "type": "action_trigger", "ts": time.time_ns() // 1_000_000,
                    "data": {"script_id": row["value"], "offset_ms": 0,
                             "conversation_id": turn_id, "anchor_source": "robot_main_brain"},
                }

            def current_and_valid():
                return (self._word_active(robot_id, turn_id, generation, anchor)
                        and self._anchor_position(anchor) == position)

            await self._publish_action(
                row, packet, guard=current_and_valid,
                rejected=lambda: "cancelled_before_send",
            )
        finally:
            # A duplicate dispatch must not remove another task's live token.
            if owns_send and self.word_sends.get(sending_key, (None, None))[1] is anchor:
                self.word_sends.pop(sending_key, None)

    async def _dispatch(self, robot_id, turn_id, call_id, generation):
        async with self.locks[robot_id]:
            if ((robot_id, turn_id) in self.word_anchors
                    or self.pending.get((robot_id, turn_id)) != (call_id, generation)):
                return
            self.pending.pop((robot_id, turn_id), None)
            robot = self.robots[robot_id]
            if generation != robot.generation:
                after = "cancelled_before_send"
            else:
                after = "send_unknown"  # reserved before I/O; never blindly retry on crash
            changed = await asyncio.to_thread(
                self.store.set_action_status, robot_id, call_id, "awaiting_audio_anchor", after,
            )
            if not changed or after != "send_unknown":
                return
            row = await asyncio.to_thread(self.store.get_action, robot_id, call_id)
            if row is None:
                return
            packet = {
                "type": "action_trigger", "ts": time.time_ns() // 1_000_000,
                "data": {"script_id": row["value"], "offset_ms": 0,
                         "conversation_id": turn_id, "anchor_source": "robot_main_brain"},
            }
        await self._publish_action(row, packet)

    async def _publish_action(self, row, packet, *, guard=None, rejected=None):
        robot_id, call_id = row["robot_id"], row["call_id"]
        # The DB now says send_unknown. A cancel racing this publish cannot
        # assert "not executed": the transport may already have accepted it.
        try:
            publisher = self.main_publishers[robot_id]
            if guard is not None and hasattr(publisher, "publish_if"):
                sent = await asyncio.wait_for(publisher.publish_if(packet, guard), 6)
            elif guard is None or guard():
                await asyncio.wait_for(publisher.publish(packet), 6)
                sent = True
            else:
                sent = False
        except Exception as exc:
            await asyncio.to_thread(
                self.store.set_action_status, robot_id, call_id, "send_unknown", "send_unknown",
                type(exc).__name__,
            )
            self._event(row, status="send_unknown", error=type(exc).__name__)
            return
        after = "published_unknown" if sent else rejected()
        await asyncio.to_thread(
            self.store.set_action_status, robot_id, call_id, "send_unknown", after,
        )
        self._event(row, status=after)

    async def perform_now(self, robot_id, call_id, conversation_id, generation, script, *,
                          user_text=None):
        """A runtime-chosen motion that starts now: listening, a sentence's gesture or
        her own reaction. The caller picks the moment; the same allowlist, veto,
        durable reservation, physical cooldown and device receipts apply as for
        every other action. ``conversation_id`` is the reply's turn when there is one."""
        if not self.enabled or robot_id not in self.main_publishers:
            return {"status": "not_configured", "real_device_executed": False}
        if script not in self.allowlist:
            raise ValueError("action is not enabled for physical execution")
        if user_text is not None and not current_action_not_vetoed(user_text):
            raise ValueError("physical action vetoed by current user")
        if (not isinstance(call_id, str) or not 1 <= len(call_id) <= 128
                or not isinstance(conversation_id, str) or not conversation_id
                or type(generation) is not int):
            raise ValueError("invalid action identity")
        robot = self.robots[robot_id]
        if generation != robot.generation:
            raise ValueError("stale_generation")
        action = resolve_action(self.catalog, script)
        async with self.locks[robot_id]:
            row, duplicate = await asyncio.to_thread(
                self.store.reserve_action, robot_id, call_id, conversation_id, generation,
                robot.config.room_id, "action", script, action["duration_ms"],
            )
            if row is None:
                return {"status": "conflict_or_cooldown", "real_device_executed": False}
            if duplicate:
                return {"status": "duplicate_" + row["status"], "real_device_executed": False}
            self._event(row)
            changed = await asyncio.to_thread(
                self.store.set_action_status, robot_id, call_id, "awaiting_audio_anchor",
                "send_unknown",
            )
            row = await asyncio.to_thread(self.store.get_action, robot_id, call_id)
            if not changed or row is None:
                return {"status": "cancelled_before_send", "real_device_executed": False}
            packet = {
                "type": "action_trigger", "ts": time.time_ns() // 1_000_000,
                "data": {"script_id": script, "offset_ms": 0,
                         "conversation_id": conversation_id, "anchor_source": "robot_main_brain"},
            }
        await self._publish_action(row, packet)
        row = await asyncio.to_thread(self.store.get_action, robot_id, call_id)
        return {"status": row["status"] if row else "unknown", "real_device_executed": False}

    async def cancel_pending(self, robot_id, turn_id=None):
        if robot_id not in self.locks:
            return
        async with self.locks[robot_id]:
            # Keep a revocable token through the final transport predicate.
            # Once the transport actually writes, cancellation still cannot
            # assert that the device did not receive the action.
            for key, (pending_turn, anchor) in self.word_sends.items():
                if key[0] == robot_id and (turn_id is None or pending_turn == turn_id):
                    anchor["cancelled"] = True
            expressions = [
                (key, pending_turn) for key, pending_turn in self.pending_expressions.items()
                if key[0] == robot_id and (turn_id is None or pending_turn == turn_id)
            ]
            for key, pending_turn in expressions:
                self.pending_expressions.pop(key, None)
                changed = await asyncio.to_thread(
                    self.store.set_action_status, robot_id, key[1],
                    "reserved", "cancelled_before_send",
                )
                if changed:
                    self.emit(robot_id, "action.state", turn_id=pending_turn,
                              call_id=key[1], status="cancelled_before_send",
                              real_device_executed=False, device_completion="unknown")
            keys = [key for key in self.pending
                    if key[0] == robot_id and (turn_id is None or key[1] == turn_id)]
            for key in keys:
                call_id, _generation = self.pending.pop(key)
                word = self.word_anchors.pop(key, None)
                changed = await asyncio.to_thread(
                    self.store.set_action_status, robot_id, call_id,
                    "awaiting_text_anchor" if word is not None else "awaiting_audio_anchor",
                    "cancelled_before_send",
                )
                if changed:
                    self.emit(robot_id, "action.state", turn_id=key[1],
                              call_id=call_id, status="cancelled_before_send",
                              real_device_executed=False)

    async def close(self):
        for robot_id in self.locks:
            await self.cancel_pending(robot_id)
        for task in tuple(self.tasks):
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        self.tasks.clear()
        await asyncio.gather(
            *(publisher.close() for publisher in self.main_publishers.values()),
            return_exceptions=True,
        )

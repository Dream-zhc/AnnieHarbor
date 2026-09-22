"""Phase 7: one-robot, one-Room action dispatch with durable idempotency.

An action_trigger is tied to the first published PCM, not a wall-clock guess.
The historical wire has no action completion receipt; published is UNKNOWN,
never a claim that the embedded motor finished.
"""

import asyncio
import re
import time

from annie_brain.action_catalog import resolve_action

EXPRESSIONS = frozenset({"喜", "怒", "哀", "乐", "中"})
ANCHOR_TIMEOUT = 15


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
                 enabled=False, allowlist=(), social_actions=False):
        self.store = store
        self.catalog = catalog
        self.robots = robots
        self.main_publishers = main_publishers
        self.emit = emit
        self.enabled = enabled
        self.allowlist = tuple(allowlist)
        self.social_actions = bool(enabled and social_actions)
        self.social_allowlist = tuple(
            script_id for script_id in self.allowlist
            if catalog[script_id]["category"] == "micro"
            or script_id == "agree_micro_nod"
        ) if self.social_actions else ()
        self.pending = {}  # (robot_id,turn_id) -> (call_id,session identity)
        self.tasks = set()
        self.connection_errors = {}
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

    def _event(self, row, *, status=None, error=None):
        self.emit(
            row["robot_id"], "action.state", turn_id=row["turn_id"],
            call_id=row["call_id"], action_kind=row["kind"], action_value=row["value"],
            room_id=row["room_id"], generation=row["generation"],
            status=status or row["status"], error=error,
            real_device_executed=False,
            device_completion="unknown",
        )

    async def request(self, robot_id, call_id, turn_id, generation, kind, value, *,
                      speech, user_text, social=False):
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
        if social and (not self.social_actions or kind != "action"
                       or value not in self.social_allowlist):
            raise ValueError("social motion is not permitted for this robot")
        if kind == "action":
            if value not in self.allowlist:
                raise ValueError("action is not enabled for physical execution")
            action = resolve_action(self.catalog, value)
            cooldown = action["duration_ms"]
        elif kind == "expression" and type(value) is str and value in EXPRESSIONS:
            cooldown = 0
        else:
            raise ValueError("unknown expression/action kind")
        robot = self.robots[robot_id]
        if generation != robot.generation:
            raise ValueError("stale_generation")
        async with self.locks[robot_id]:
            row, duplicate = await asyncio.to_thread(
                self.store.reserve_action, robot_id, call_id, turn_id, generation,
                robot.config.room_id, kind, value, cooldown, self.catalog,
            )
            if row is None:
                return {"status": "conflict_or_cooldown", "real_device_executed": False}
            if duplicate:
                self._event(row, status="duplicate_" + row["status"])
                return {"status": "duplicate_" + row["status"],
                        "real_device_executed": False}
            self._event(row)
            if kind == "action":
                if speech.turn_id == turn_id and (
                    speech.frames or getattr(speech, "inflight_end", 0)
                ):
                    await asyncio.to_thread(
                        self.store.set_action_status, robot_id, call_id, "awaiting_audio_anchor",
                        "rejected_late",
                    )
                    self._event(row, status="rejected_late")
                    return {"status": "rejected_late", "real_device_executed": False}
                self.pending[(robot_id, turn_id)] = (call_id, generation)
                self._schedule(self._expire(robot_id, turn_id, call_id, generation))
                return {"status": "awaiting_audio_anchor", "script_id": value,
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
            await asyncio.to_thread(
                self.store.set_action_status, robot_id, call_id, "reserved", "send_unknown",
            )
            packet = {"type": "expression", "ts": time.time_ns() // 1_000_000,
                      "data": {"expression": value, "turn_id": turn_id,
                               "source": "robot_main_brain"}}
        # Lock protects reservations only. Room I/O may stall, but cancel and
        # other action requests must still be able to acquire this robot's lock.
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
            return {"status": "send_unknown", "real_device_executed": False}
        if not sent:
            await asyncio.to_thread(
                self.store.set_action_status, robot_id, call_id,
                "send_unknown", "rejected_late",
            )
            self._event(row, status="rejected_late")
            return {"status": "rejected_late", "real_device_executed": False}
        await asyncio.to_thread(
            self.store.set_action_status, robot_id, call_id,
            "send_unknown", "published_unknown",
        )
        self._event(row, status="published_unknown")
        return {"status": "published_unknown", "real_device_executed": False,
                "device_completion": "unknown"}

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
                changed = await asyncio.to_thread(
                    self.store.set_action_status, robot_id, call_id, "awaiting_audio_anchor",
                    "expired_no_pcm",
                )
                if changed:
                    self.emit(robot_id, "action.state", turn_id=turn_id,
                              call_id=call_id, status="expired_no_pcm",
                              real_device_executed=False)

    def on_first_pcm(self, event):
        key = event["robot_id"], event.get("turn_id")
        pending = self.pending.get(key)
        if pending and pending[1] == event.get("generation"):
            self._schedule(self._dispatch(*key, *pending))

    async def _dispatch(self, robot_id, turn_id, call_id, generation):
        async with self.locks[robot_id]:
            if self.pending.get((robot_id, turn_id)) != (call_id, generation):
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
        # The DB now says send_unknown. A cancel racing this publish cannot
        # assert "not executed": the transport may already have accepted it.
        try:
            await asyncio.wait_for(self.main_publishers[robot_id].publish(packet), 6)
        except Exception as exc:
            await asyncio.to_thread(
                self.store.set_action_status, robot_id, call_id, "send_unknown", "send_unknown",
                type(exc).__name__,
            )
            self._event(row, status="send_unknown", error=type(exc).__name__)
            return
        await asyncio.to_thread(
            self.store.set_action_status, robot_id, call_id, "send_unknown", "published_unknown",
        )
        self._event(row, status="published_unknown")

    async def cancel_pending(self, robot_id, turn_id=None):
        if robot_id not in self.locks:
            return
        async with self.locks[robot_id]:
            keys = [key for key in self.pending
                    if key[0] == robot_id and (turn_id is None or key[1] == turn_id)]
            for key in keys:
                call_id, _generation = self.pending.pop(key)
                changed = await asyncio.to_thread(
                    self.store.set_action_status, robot_id, call_id, "awaiting_audio_anchor",
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

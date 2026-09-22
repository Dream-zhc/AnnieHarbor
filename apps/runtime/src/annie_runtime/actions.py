"""Phase 7: one-robot, one-Room action dispatch with durable idempotency.

An action_trigger is tied to the first published PCM, not a wall-clock guess.
The historical wire has no action completion receipt; published is UNKNOWN,
never a claim that the embedded motor finished.
"""

import asyncio
import re
import sqlite3
import time
from contextlib import contextmanager

from annie_brain.action_catalog import resolve_action

EXPRESSIONS = frozenset({"喜", "怒", "哀", "乐", "中"})
ANCHOR_TIMEOUT = 15
_LEAD = (
    r"(?:小安妮[，, ]*)?"
    r"(?:(?:请你|请|你(?:能不能|可不可以|可以|能)?|给我|帮我|来|能不能|可不可以)"
    r"(?:给我|帮我)?)?\s*"
)
_TAIL = r"(?:一下|两下|一眼|吧|呀|啊|哦|好吗|可以吗|行吗|吗)?"
# Only the four default trial scripts have reviewed text-intent grammars.
# This does NOT establish that the physical device or motion is safe.
# An additional catalog ID cannot become motor-enabled merely by
# editing ANNIE_ACTION_ALLOWLIST; it needs an explicit reviewed grammar.
_ACTION_PHRASES = {
    "agree_micro_nod": r"点(?:一?下|个|两下)?头",
    "look_left": (
        r"(?:看(?:一下|一眼)?(?:往|向)?左(?:边|面)?|"
        r"(?:往|向)?左(?:边|面)?看(?:一下|一眼)?)"
    ),
    "look_right": (
        r"(?:看(?:一下|一眼)?(?:往|向)?右(?:边|面)?|"
        r"(?:往|向)?右(?:边|面)?看(?:一下|一眼)?)"
    ),
    "fast_double_blink": r"(?:连|快)?眨(?:两|二|2)下(?:眼(?:睛)?)?",
}
_EXPRESSION_PHRASES = {
    "喜": r"(?:开心|高兴|喜悦)",
    "怒": r"(?:生气|愤怒)",
    "哀": r"(?:难过|悲伤|伤心)",
    "乐": r"(?:快乐|欢乐)",
    "中": r"(?:中性|平静)",
}


def explicit_physical_request(text, *, kind=None, value=None):
    """Require an imperative for the EXACT selected motion/expression.

    A narrative, question about a gesture, or left/right mismatch must never
    turn into motor execution merely because the LLM chose a known ID.
    """
    if not isinstance(text, str):
        return False
    plain = text.strip(" 。？!！?，,")
    if not plain or len(plain) > 64:
        return False
    if re.search(r"别|不要|不能|不许|停止|取消|不再", plain):
        return False
    actions = (
        [(value, _ACTION_PHRASES[value])] if kind == "action" and value in _ACTION_PHRASES
        else list(_ACTION_PHRASES.items()) if kind is None else []
    )
    for _ident, phrase in actions:
        if re.fullmatch(_LEAD + phrase + _TAIL, plain):
            return True
    expressions = (
        [(value, _EXPRESSION_PHRASES[value])]
        if kind == "expression" and value in _EXPRESSION_PHRASES
        else list(_EXPRESSION_PHRASES.items()) if kind is None else []
    )
    for _ident, phrase in expressions:
        if re.fullmatch(
            _LEAD + r"(?:做(?:个|一个)?|露(?:个|一个)?|摆(?:个|一个)?|展示|来个)?"
            + phrase + r"(?:的)?(?:表情|脸|笑脸)" + _TAIL,
            plain,
        ):
            return True
    return False


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
        self.tasks = set()
        self.connection_errors = {}
        self.locks = {robot_id: asyncio.Lock() for robot_id in robots}

    @contextmanager
    def _db(self):
        connection = sqlite3.connect(self.store.path, timeout=3)
        connection.row_factory = sqlite3.Row
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def _initialize(self):
        with self._db() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS action_requests (
                robot_id TEXT NOT NULL, call_id TEXT NOT NULL, turn_id TEXT NOT NULL,
                generation INTEGER NOT NULL, room_id TEXT NOT NULL,
                kind TEXT NOT NULL, value TEXT NOT NULL, status TEXT NOT NULL,
                error TEXT, ts_ms INTEGER NOT NULL, updated_ms INTEGER NOT NULL,
                PRIMARY KEY(robot_id,call_id),
                UNIQUE(robot_id,turn_id,kind,value))""")
            db.execute("""CREATE INDEX IF NOT EXISTS action_requests_recent
                ON action_requests(robot_id,updated_ms DESC)""")
            # No restarted process can publish its previous PCM anchor.
            # Retire abandoned in-memory requests instead of blocking motions
            # indefinitely or falsely reporting that the device moved.
            now = time.time_ns() // 1_000_000
            db.execute(
                """UPDATE action_requests SET status='expired_no_pcm',
                   error='process_restarted',updated_ms=?
                   WHERE status='awaiting_audio_anchor'""",
                (now,),
            )
            db.execute(
                """UPDATE action_requests SET status='cancelled_before_send',
                   error='process_restarted',updated_ms=?
                   WHERE status='reserved'""",
                (now,),
            )

    async def start(self):
        await asyncio.to_thread(self._initialize)
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

    def _rows(self, robot_id, limit):
        with self._db() as db:
            return [dict(row) for row in db.execute(
                "SELECT * FROM action_requests WHERE robot_id=? ORDER BY ts_ms DESC LIMIT ?",
                (robot_id, limit),
            )]

    async def list(self, robot_id, limit=50):
        if robot_id not in self.robots:
            raise KeyError(robot_id)
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("invalid action history limit")
        return await asyncio.to_thread(self._rows, robot_id, limit)

    def _reserve(self, robot_id, call_id, turn_id, generation, room_id, kind, value, cooldown):
        now = time.time_ns() // 1_000_000
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            existing = db.execute(
                "SELECT * FROM action_requests WHERE robot_id=? AND call_id=?",
                (robot_id, call_id),
            ).fetchone()
            if existing:
                return dict(existing), True
            same = db.execute(
                """SELECT * FROM action_requests
                   WHERE robot_id=? AND turn_id=? AND kind=? AND value=?""",
                (robot_id, turn_id, kind, value),
            ).fetchone()
            if same:
                return dict(same), True
            # Do not queue multiple motions at once, or a second one during
            # the catalog's own motion duration. No guessed strength/duration.
            if kind == "action":
                last = db.execute(
                    """SELECT value, updated_ms, status FROM action_requests
                       WHERE robot_id=? AND kind='action'
                       ORDER BY ts_ms DESC LIMIT 1""",
                    (robot_id,),
                ).fetchone()
                if last and (last["status"] == "awaiting_audio_anchor" or
                             last["status"] in {"published_unknown", "send_unknown"} and
                             now - last["updated_ms"] < max(
                                 cooldown,
                                 self.catalog.get(last["value"], {}).get("duration_ms", cooldown),
                             )):
                    return None, False
            db.execute(
                """INSERT INTO action_requests
                   VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (robot_id, call_id, turn_id, generation, room_id, kind, value,
                 "awaiting_audio_anchor" if kind == "action" else "reserved",
                 None, now, now),
            )
            row = db.execute(
                "SELECT * FROM action_requests WHERE robot_id=? AND call_id=?",
                (robot_id, call_id),
            ).fetchone()
        return dict(row), False

    def _status(self, robot_id, call_id, before, after, error=None):
        now = time.time_ns() // 1_000_000
        with self._db() as db:
            cursor = db.execute(
                """UPDATE action_requests SET status=?,error=?,updated_ms=?
                   WHERE robot_id=? AND call_id=? AND status=?""",
                (after, error, now, robot_id, call_id, before),
            )
        return cursor.rowcount == 1

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
                      speech, user_text):
        if not self.enabled:
            return {"status": "not_configured", "real_device_executed": False}
        if robot_id not in self.robots or speech is None or robot_id not in self.main_publishers:
            return {"status": "not_configured", "reason": "room_or_tts_missing",
                    "real_device_executed": False}
        if (not isinstance(call_id, str) or not 1 <= len(call_id) <= 128 or
                not isinstance(turn_id, str) or not turn_id or
                type(generation) is not int):
            raise ValueError("invalid action turn identity")
        if not explicit_physical_request(user_text, kind=kind, value=value):
            raise ValueError("physical action requires an explicit CURRENT user request")
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
                self._reserve, robot_id, call_id, turn_id, generation,
                robot.config.room_id, kind, value, cooldown,
            )
            if row is None:
                return {"status": "conflict_or_cooldown", "real_device_executed": False}
            if duplicate:
                self._event(row, status="duplicate_" + row["status"])
                return {"status": "duplicate_" + row["status"],
                        "real_device_executed": False}
            self._event(row)
            if kind == "action":
                if speech.turn_id == turn_id and speech.frames:
                    await asyncio.to_thread(
                        self._status, robot_id, call_id, "awaiting_audio_anchor",
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
            if (speech.turn_id == turn_id and speech.frames or
                    speech.turn_id not in (None, turn_id) and
                    getattr(speech, "worker", None) is not None and
                    not speech.worker.done()):
                await asyncio.to_thread(self._status, robot_id, call_id, "reserved",
                                        "rejected_late")
                self._event(row, status="rejected_late")
                return {"status": "rejected_late", "real_device_executed": False}
            await asyncio.to_thread(
                self._status, robot_id, call_id, "reserved", "send_unknown",
            )
            packet = {"type": "expression", "ts": time.time_ns() // 1_000_000,
                      "data": {"expression": value, "turn_id": turn_id,
                               "source": "robot_main_brain"}}
            try:
                await asyncio.wait_for(speech.publisher.publish(packet), 6)
            except Exception as exc:
                await asyncio.to_thread(
                    self._status, robot_id, call_id, "send_unknown", "send_unknown",
                    type(exc).__name__,
                )
                self._event(row, status="send_unknown", error=type(exc).__name__)
                return {"status": "send_unknown", "real_device_executed": False}
            await asyncio.to_thread(
                self._status, robot_id, call_id, "send_unknown", "published_unknown",
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
                    self._status, robot_id, call_id, "awaiting_audio_anchor",
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
                self._status, robot_id, call_id, "awaiting_audio_anchor", after,
            )
            if not changed or after != "send_unknown":
                return
            rows = await asyncio.to_thread(self._rows, robot_id, 100)
            row = next((item for item in rows if item["call_id"] == call_id), None)
            if row is None:
                return
            packet = {
                "type": "action_trigger", "ts": time.time_ns() // 1_000_000,
                "data": {"script_id": row["value"], "offset_ms": 0,
                         "conversation_id": turn_id, "anchor_source": "robot_main_brain"},
            }
            try:
                await asyncio.wait_for(self.main_publishers[robot_id].publish(packet), 6)
            except Exception as exc:
                await asyncio.to_thread(
                    self._status, robot_id, call_id, "send_unknown", "send_unknown",
                    type(exc).__name__,
                )
                self._event(row, status="send_unknown", error=type(exc).__name__)
                return
            await asyncio.to_thread(
                self._status, robot_id, call_id, "send_unknown", "published_unknown",
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
                    self._status, robot_id, call_id, "awaiting_audio_anchor",
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

"""Long-term facts, after the memory of DeepSeek Harness' Aurabot.

The SQLite row is canonical and the conversation ledger stays untouched. Once a
conversation goes quiet, what the user said is compared with related old facts
and facts are added, merged or retired; a retired fact stays restorable. Every
user turn searches the facts locally (BM25 and a forgetting curve, no model
call) and the hits ride along with the next reply. A forgotten fact keeps its
row as a tombstone, so no later conversation can teach it again.
"""

import asyncio
import logging
import re
import sqlite3
import time
from collections import deque
from contextlib import contextmanager
from uuid import uuid4

from annie_brain.memory_consolidate import KINDS, consolidate_request, parse_ops
from annie_brain.memory_index import (
    DAY_MS,
    MemoryIndex,
    faded,
    importance_of,
    recall,
    recall_hint,
    retention,
    similarity,
    weight,
    when_text,
)
from annie_brain.memory_scope import memory_namespace
from annie_brain.recap import parse_recap, recap_request

log = logging.getLogger(__name__)

LONG_KINDS = ("semantic", "persona", "episodes")
BOOTSTRAP_BYTES = 1024
BOOTSTRAP_CHAT_FACTS = 4
BOOTSTRAP_LONG_FACTS = 8
BOOTSTRAP_HEADER = (
    "\n【你记得的事（方括号是什么时候听到的）】是以前和来访者聊天记下的，来的人很多，不一定是眼前这位说的；"
    "对方现在说的对不上就以现在为准，日期按方括号推算，别把过去的安排当成还没发生，也别说“根据记忆”。\n"
)
MANUAL_IMPORTANCE = 0.7
EVIDENCE_LIMIT = 4
TOOL_MIN_SCORE = 0  # she asked for it: any shared word is evidence
SAME_FACT = 0.6
INDEX_LIMIT = 2000
INDEX_TTL_MS = 3_600_000
RELATED_LIMIT = 15
RELATED_MIN_SCORE = 0.2
PENDING_LIMIT = 400
DIALOGUE_LIMIT = 48
STALE_RUNNING_MS = 300_000
CONSOLIDATE_TOKENS = 900
CONSOLIDATE_ATTEMPTS = 3
RECAP_TOKENS = 300
POLL_SECONDS = 5
RECALL_LOG = 20
CONSOLIDATION_LOG = 10
BEIJING_OFFSET_MS = 8 * 3_600_000
CHANGES = ("added", "merged", "updated", "outdated")
COLUMNS = ("id,robot_id,persona_id,kind,fact_key,text,source_turn_id,source_created_ms,"
           "version,status,updated_ms,index_state,importance,hits,hit_days,last_hit_ms,"
           "superseded_by,origin")

_EMPTY_FACT_KEY = re.compile(
    r"^(?:no[-_]?extractable(?:[-_]?content)?|no[-_]?memory|no[-_]?fact|"
    r"no[-_]?extraction|empty[-_]?memory|none|null|placeholder|no[-_]?durable[-_]?fact)$", re.I,
)
_EMPTY_FACT_TEXT = re.compile(
    r"(?:未提取到|未发现|无(?:法|明确)?|没有|暂无|未能).{0,45}"
    r"(?:长期记忆|持久偏好|有意义经历|事实|提取|可记忆内容)"
)
_NOT_FACT_PREFIXES = ("placeholder", "用户询问", "用户曾询问", "用户问过", "用户只是询问",
                      "用户提及地点为", "用户称呼助手", "本轮没有", "本轮未")
_REMINDER_REQUEST = re.compile(r"(?:提醒|闹钟|定时|倒计时).{0,35}(?:分钟|小时|秒|点)|(?:分钟|小时|秒)后.{0,16}提醒")
# What the user calls Annie is not a fact about the user; a model once read
# "用户称呼当前AI角色为“小bug”" as the user's own name and greeted them with it.
_ASSISTANT_NICKNAME = re.compile(
    r"用户(?:称呼|叫|管)(?:当前|这个)?(?:的)?(?:AI|ai|助手|机器人|角色|小安妮|你)")
# Many visitors talk to one robot: "the user's name" is not a fact about whoever
# speaks next (10/21 demo: every visitor was greeted by one visitor's name).
# Names come from the camera's face library only.
_USER_NAME = re.compile(r"用户(?:的)?(?:名字|姓名|名)(?:是|叫|为)|用户(?:自称|名叫|叫做)"
                        r"|用户(?:说|称|表示|介绍)自己(?:叫|名叫)")
_USER_NAME_KEY = re.compile(r"^(?:user_)?(?:real_)?(?:name|nickname|called)(?:_|$)", re.I)
# "是" alone may be a job ("用户说自己是老师"); under a name key it is a name.
_NAME_TOKEN_KEY = re.compile(r"(?:^|_)(?:name|nickname|called)(?:_|$)", re.I)
_USER_IS = re.compile(r"用户(?:说|称|表示|介绍)?(?:自己)?(?:是|叫)")
# Keeping "asked to forget X" keeps X; "asked to leave" is a one-off gate action.
_ONE_SHOT_REQUEST = re.compile(r"用户.{0,6}(?:要求|让|请求|叫).{0,8}(?:忘记|忘掉|退下|闭嘴|休眠)")
_NO_OBJECT = re.compile(r"用户不?喜欢(?:带|吃|穿|用|看|买|去)?[。.]?")
_CAPABILITY_TEST = re.compile(r"测试(?:其|她|小安妮|机器人)?.{0,6}能力")
# Consolidated facts have no key to tell a name from a job: "说自己是张三" is a
# self-introduction unless the word ends like a role (老师, 杭州人, 上班族).
_SELF_INTRO = re.compile(r"用户(?:说|称|表示|介绍)自己(?:是|叫)([\u4e00-\u9fff]{2,3})[。.！!]?$")
_ROLE_ENDINGS = "员师生家长工手士者领管总人的迷粉控党族"
# One memory serves every visitor: a "别骂我" said once must not tame a roasting
# persona for everyone after, unless the wish is said to last ("以后都别骂我").
_ROAST_BAN = re.compile(r"(?:不要|别|不能|不许|不准|不想|不希望|不喜欢|不愿意).{0,8}(?:骂|怼|毒舌)")
_LASTING = re.compile(r"以后|一直|永远|每次|总是|再也|从来|长期|任何时候")
# One visitor's tiring day is no news to the next visitor.
_TODAY_MOOD = re.compile(r"今天.{0,8}(?:累|辛苦|困|烦|忙)")
# "我喜欢你" is a thing the user said, not the model's verdict on their feelings:
# it is kept as one plain sentence, and only when the user's own words say it.
_HER = r"(?:当前|这个)?(?:的)?(?:AI|ai|助手|机器人|角色|小安妮|安妮)(?:助手|角色)?"
_FONDNESS = re.compile(rf"(?:对|向|跟){_HER}.{{0,8}}?(?P<feel>喜欢|(?<!可)爱|好感|心动)"
                       rf"|(?P<verb>喜欢|(?<!可)爱)上?了?{_HER}[了啦呀哦]?(?![\u4e00-\u9fff])")
_SAID_FONDNESS = re.compile(r"(?:对|跟)(?:你|小安妮|安妮).{0,6}?(?P<feel>喜欢|(?<!可)爱|好感|心动)"
                            r"|(?P<verb>喜欢|(?<!可)爱)上?了?(?:你|小安妮|安妮)")
_NEGATION = re.compile(r"[不没别]")
FONDNESS = "用户说过喜欢小安妮。"


def now_ms():
    return time.time_ns() // 1_000_000


def is_meta_memory(key, value):
    """Not a fact about the user: empty-result prose, a self-reported name, a one-off command."""
    value = value.strip()
    return bool(_EMPTY_FACT_KEY.fullmatch(key) or _EMPTY_FACT_TEXT.search(value)
                or _USER_NAME_KEY.search(key)
                or (_NAME_TOKEN_KEY.search(key) and _USER_IS.search(value))
                or _ONE_SHOT_REQUEST.search(value) or value.strip("。. ") in {"无", "没有", "暂无"}
                or value.startswith(_NOT_FACT_PREFIXES)
                or _ASSISTANT_NICKNAME.search(value) or _USER_NAME.search(value)
                or (_REMINDER_REQUEST.search(value) and "用户要求" in value)
                or _NO_OBJECT.fullmatch(value) or _CAPABILITY_TEST.search(value))


def prepare_consolidated_fact(kind, text):
    """The model's sentence when it may become a memory, else None."""
    text = " ".join(str(text or "").split())
    if kind not in KINDS or not 1 <= len(text) <= 300:
        return None
    if "你" in text[:3] or text.startswith(("我", "助手", "机器人", "小安妮", "安妮")):
        return None
    intro = _SELF_INTRO.search(text)
    if intro and intro.group(1)[-1] not in _ROLE_ENDINGS:
        return None
    if kind != "chat" and _TODAY_MOOD.search(text):
        return None
    return None if is_meta_memory("", text) else text


def _fondness(pattern, text):
    """None when text says nothing about liking her, else whether one mention is not negated."""
    found = None
    for match in pattern.finditer(text or ""):
        start = match.start(match.lastgroup)
        before = text[max(0, start - 3):start].rsplit("\n", 1)[-1]
        found = bool(found) or not _NEGATION.search(before)
    return found


def ground_fondness(ops, said):
    """Ops saying the user likes her: FONDNESS when the user's lines said so, else ungrounded."""
    voiced = _fondness(_SAID_FONDNESS, said)
    grounded = []
    for op in ops:
        fond = _fondness(_FONDNESS, op["text"])
        if fond is None:
            grounded.append(op)
        elif fond and voiced:
            grounded.append({**op, "kind": "persona", "text": FONDNESS})
        else:
            grounded.append({**op, "ungrounded": True})
    return grounded


def safe_consolidated_scope(kind, text):
    """A roasting ban holds for this chat only, unless it says it lasts."""
    if kind in ("semantic", "persona") and _ROAST_BAN.search(text) and not _LASTING.search(text):
        return "chat"
    return kind


def _said(row):
    return row["turn_source"] == "user" and bool((row["user_text"] or "").strip())


def _busy(row, now):
    return row["status"] == "running" and now - row["updated_ms"] < STALE_RUNNING_MS


def ready_batch(rows, now, idle_ms, max_turns):
    """(rows, last row id) once the talk went quiet or grew long; None while it goes on."""
    if not rows:
        return None
    said = 0
    for position, row in enumerate(rows):
        if _busy(row, now):
            break
        if row["status"] != "running" and _said(row):
            said += 1
            if said >= max_turns:
                return rows[:position + 1], row["id"]
    if any(_busy(row, now) for row in rows) or now - max(row["updated_ms"] for row in rows) < idle_ms:
        return None
    return rows, rows[-1]["id"]


def conversations(rows):
    """[(persona, chat, turns, last user row)]; turns are (created_ms, user_text, reply)."""
    groups = {}
    for row in rows:
        if row["status"] == "running":
            continue
        user = row["user_text"].strip() if _said(row) else ""
        reply = (row.get("assistant_spoken_text") or row.get("assistant_generated_text") or "").strip()
        if not user and not reply:
            continue
        group = groups.setdefault((row["persona_id"], row["chat_id"]), {"turns": [], "source": None})
        group["turns"].append((row["created_ms"], user, reply))
        if user:
            group["source"] = row
    return [(persona, chat, group["turns"][-DIALOGUE_LIMIT:], group["source"])
            for (persona, chat), group in groups.items() if group["source"] is not None]


def _public(row, now):
    item = {**row, "retention": round(retention(row, now), 3), "faded": faded(row, now)}
    if item["status"] == "deleted":
        item["text"] = ""  # the row only stops consolidation from learning it again
    return item


class MemoryService:
    """Async boundary over canonical SQLite rows, the local recall index and the consolidator."""

    def __init__(self, store, chat, robot_ids, *, idle_seconds=120, max_turns=24):
        self.store = store
        self.chat = chat  # complete(system, user, *, max_tokens) -> JSON text
        self.robot_ids = tuple(robot_ids)
        self.idle_ms = int(idle_seconds * 1000)
        self.max_turns = max(1, int(max_turns))
        self.tasks = []
        self.running = False
        self.errors = {}
        self.on_change = None  # local canonical changes, not a per-turn model call
        self.recalls = {robot: deque(maxlen=RECALL_LOG) for robot in self.robot_ids}
        self.recall_turns = dict.fromkeys(self.robot_ids, 0)
        self.consolidations = {robot: deque(maxlen=CONSOLIDATION_LOG) for robot in self.robot_ids}
        self.pending = {}
        self.recaps = {}
        self._locks = {robot: asyncio.Lock() for robot in self.robot_ids}
        self._indexes = {}
        self._generations = dict.fromkeys(self.robot_ids, 0)

    def _changed(self, robot):
        self._generations[robot] = self._generations.get(robot, 0) + 1
        for key in [key for key in self._indexes if key[0] == robot]:
            del self._indexes[key]
        if self.on_change is not None:
            self.on_change(robot)

    async def _canonical_write(self, robot, method, *args, changed=lambda result: result is not None):
        # to_thread cancellation cannot abort SQLite: notify from the committed
        # worker, even when its tool/request coroutine has already been retired.
        loop = asyncio.get_running_loop()

        def write():
            result = method(*args)
            if changed(result):
                try:
                    loop.call_soon_threadsafe(self._changed, robot)
                except RuntimeError:
                    log.debug("memory committed after event loop closed robot=%s", robot)
            return result

        return await asyncio.to_thread(write)

    @contextmanager
    def _connect(self):
        db = sqlite3.connect(self.store.path, timeout=2)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def _rows(self, robot, persona, statuses):
        marks = ",".join("?" for _ in statuses)
        with self._connect() as db:
            rows = db.execute(
                f"""SELECT {COLUMNS} FROM long_term_memories
                    WHERE robot_id=? AND (kind='semantic' OR persona_id=?)
                    AND status IN ({marks}) ORDER BY updated_ms DESC LIMIT {INDEX_LIMIT}""",
                (robot, persona, *statuses),
            ).fetchall()
        return [dict(row) for row in rows]

    def _build_index(self, robot, persona):
        now = now_ms()
        rows = [row for row in self._rows(robot, persona, ("active",))
                if not is_meta_memory(row["fact_key"], row["text"])]
        live = [row for row in rows if not faded(row, now)]
        return {"built_ms": now, "live_rows": live,
                "full": MemoryIndex(rows), "live": MemoryIndex(live)}

    async def _index(self, robot, persona):
        """BM25 over the active facts; `live` leaves out the faded ones."""
        key = (robot, persona)
        cached = self._indexes.get(key)
        if cached is not None and now_ms() - cached["built_ms"] < INDEX_TTL_MS:
            return cached
        generation = self._generations.get(robot, 0)
        entry = await asyncio.to_thread(self._build_index, robot, persona)
        if generation == self._generations.get(robot, 0):
            self._indexes[key] = entry
        return entry

    async def list(self, robot, persona, include_deleted=False):
        memory_namespace(robot, persona, "persona")
        statuses = ("active", "outdated", "deleted") if include_deleted else ("active",)
        rows = await asyncio.to_thread(self._rows, robot, persona, statuses)
        now = now_ms()
        return [_public(row, now) for row in rows]

    async def chat_list(self, robot, persona, chat, include_deleted=False):
        memory_namespace(robot, persona, "persona")
        if not isinstance(chat, str) or not chat.strip():
            raise ValueError("chat_id required")
        rows = await asyncio.to_thread(
            self.store.list_chat_memories, robot, persona, chat, include_deleted,
        )
        return [{**row, "kind": "chat", "index_state": "not_required"} for row in rows]

    async def chat_put(self, robot, persona, chat, key, text, *, source_turn_id=None,
                       source_created_ms=None, expires_at_ms=None):
        memory_namespace(robot, persona, "persona")
        if (not isinstance(chat, str) or not chat.strip() or
                not isinstance(key, str) or not 1 <= len(key) <= 64 or
                not isinstance(text, str) or not 1 <= len(text.strip()) <= 300):
            raise ValueError("invalid chat memory")
        if expires_at_ms is not None and (
            type(expires_at_ms) is not int or expires_at_ms <= now_ms()
        ):
            raise ValueError("expiration must be in the future")
        if source_created_ms is None:
            source_created_ms = now_ms()
        async with self._locks[robot]:
            result = await self._canonical_write(
                robot, self.store.put_chat_memory, robot, persona, chat, key, text.strip(),
                source_turn_id, source_created_ms, expires_at_ms,
            )
        return result

    async def chat_change(self, robot, persona, chat, ident, version, text=None):
        memory_namespace(robot, persona, "persona")
        if text is not None and not 1 <= len(text.strip()) <= 300:
            raise ValueError("invalid chat memory text")
        async with self._locks[robot]:
            next_version = await self._canonical_write(
                robot, self.store.change_chat_memory, robot, persona, chat,
                ident, version, text.strip() if text else None,
            )
        return {"id": ident, "version": next_version,
                "status": "active" if text else "deleted",
                "scope": "chat_only", "conversation_history_deleted": False}

    async def bootstrap(self, robot, persona, chat, *, with_metadata=False):
        """What a Realtime session opens with: this chat's constraints, then the weightiest facts."""
        entry = await self._index(robot, persona)
        chats = await self.chat_list(robot, persona, chat)
        now = now_ms()
        longs = sorted(entry["live_rows"], key=lambda row: -weight(row, now))
        groups = (
            ("这次聊天", [row for row in chats
                       if not is_meta_memory(row["fact_key"], row["text"])][:BOOTSTRAP_CHAT_FACTS]),
            ("", longs[:BOOTSTRAP_LONG_FACTS]),
        )
        used, lines, expiries = 0, [], []
        for scope, rows in groups:
            for row in rows:
                when = when_text(row.get("source_created_ms"), now)
                line = f"[{scope}·{when}] {row['text']}" if scope else f"[{when}] {row['text']}"
                size = len(line.encode("utf-8"))
                if used + size > BOOTSTRAP_BYTES:
                    continue
                lines.append(line)
                used += size
                if type(row.get("expires_at_ms")) is int:
                    expiries.append(row["expires_at_ms"])
        text = BOOTSTRAP_HEADER + "\n".join(lines) if lines else ""
        if not with_metadata:
            return text
        return {"text": text, "expires_at_ms": min(expiries) if expiries else None}

    async def bootstrap_snapshot(self, robot, persona, chat):
        return await self.bootstrap(robot, persona, chat, with_metadata=True)

    def _log_recall(self, robot, persona, query, hits, now, source):
        if robot not in self.recalls or not hits and source == "turn":
            return
        self.recalls[robot].appendleft({
            "at_ms": now, "source": source, "persona_id": persona, "query": query[:120],
            "hits": [{"id": hit["row"]["id"], "text": hit["row"]["text"],
                      "score": round(hit["score"], 2), "why": hit["why"]} for hit in hits],
        })

    def _mark_hits(self, robot, ids, now):
        today = (now + BEIJING_OFFSET_MS) // DAY_MS
        with self._connect() as db:
            db.executemany(
                """UPDATE long_term_memories SET hits=hits+1,
                   hit_days=hit_days+(last_hit_ms IS NULL OR (last_hit_ms+?)/?<?),
                   last_hit_ms=? WHERE robot_id=? AND id=? AND status='active'""",
                [(BEIJING_OFFSET_MS, DAY_MS, today, now, robot, ident) for ident in ids],
            )

    async def _hit(self, robot, hits, now):
        """Being recalled slows forgetting. Statistics only: no version, no change event."""
        today = (now + BEIJING_OFFSET_MS) // DAY_MS
        for hit in hits:
            row = hit["row"]
            last = row.get("last_hit_ms")
            if last is None or (last + BEIJING_OFFSET_MS) // DAY_MS < today:
                row["hit_days"] = (row.get("hit_days") or 0) + 1
            row["hits"] = (row.get("hits") or 0) + 1
            row["last_hit_ms"] = now
        try:
            await asyncio.to_thread(self._mark_hits, robot, [hit["row"]["id"] for hit in hits], now)
        except Exception as exc:
            log.warning("memory hit update failed robot=%s: %s", robot, type(exc).__name__)

    async def recall_hint(self, robot, persona, query, previous=""):
        """Old facts the user's words point at, as an instruction block for the next reply."""
        if not isinstance(query, str) or not query.strip():
            return ""
        entry = await self._index(robot, persona)
        now = now_ms()
        hits = await asyncio.to_thread(recall, entry["live"], query[:300], now,
                                       previous=previous or "")
        if robot in self.recall_turns:
            self.recall_turns[robot] += 1
        self._log_recall(robot, persona, query, hits, now, "turn")
        if hits:
            await self._hit(robot, hits, now)
        return recall_hint(hits, now)

    async def recall(self, robot, persona, query):
        """The recall_memory tool: the same local search, with IDs and versions for forget/correct."""
        memory_namespace(robot, persona, "persona")
        if not isinstance(query, str) or not 1 <= len(query.strip()) <= 300:
            raise ValueError("invalid memory query")
        entry = await self._index(robot, persona)
        now = now_ms()
        hits = await asyncio.to_thread(entry["full"].search, query, now, limit=EVIDENCE_LIMIT,
                                       min_score=TOOL_MIN_SCORE)
        self._log_recall(robot, persona, query, hits, now, "tool")
        if hits:
            await self._hit(robot, hits, now)
        return {"status": "ok", "evidence": [
            {"id": row["id"], "version": row["version"], "kind": row["kind"],
             "fact_key": row["fact_key"], "text": row["text"],
             "when": when_text(row.get("source_created_ms"), now)}
            for row in (hit["row"] for hit in hits)
        ]}

    def _put(self, robot, persona, kind, key, text, source_turn_id, source_created_ms,
             expected_version, importance):
        scope = memory_namespace(robot, persona, kind)
        actual_persona = "" if kind == "semantic" else scope[3]
        now = now_ms()
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                """SELECT id,version,status,source_created_ms FROM long_term_memories
                   WHERE robot_id=? AND persona_id=? AND kind=? AND fact_key=?""",
                (robot, actual_persona, kind, key),
            ).fetchone()
            if source_turn_id is None and row is not None and expected_version is None:
                raise ValueError("memory key exists; use the versioned correction endpoint")
            if expected_version is not None and (row is None or row["version"] != expected_version):
                raise ValueError("memory version conflict")
            # A forgotten key stays forgotten.
            if row is not None and row["status"] == "deleted":
                return None
            if row is not None and source_created_ms <= row["source_created_ms"]:
                return None
            ident, version = (row["id"], row["version"] + 1) if row else (uuid4().hex, 1)
            db.execute(
                """INSERT INTO long_term_memories
                   (id,robot_id,persona_id,kind,fact_key,text,source_turn_id,source_created_ms,
                    version,status,updated_ms,index_state,embedding,importance,origin)
                   VALUES (?,?,?,?,?,?,?,?,?,'active',?,'not_required',NULL,?,?)
                   ON CONFLICT(robot_id,persona_id,kind,fact_key) DO UPDATE SET
                     text=excluded.text,source_turn_id=excluded.source_turn_id,
                     source_created_ms=excluded.source_created_ms,version=excluded.version,
                     status='active',updated_ms=excluded.updated_ms,index_state='not_required',
                     embedding=NULL,importance=excluded.importance,origin=excluded.origin,
                     superseded_by=NULL""",
                (ident, robot, actual_persona, kind, key, text, source_turn_id,
                 source_created_ms, version, now, importance,
                 "manual" if source_turn_id is None else "auto"),
            )
        return ident, version

    async def put(self, robot, persona, kind, key, text, *, source_turn_id=None,
                  source_created_ms=None, expected_version=None, importance=None):
        if (not isinstance(key, str) or not 1 <= len(key) <= 64 or
                not isinstance(text, str) or not 1 <= len(text.strip()) <= 300):
            raise ValueError("invalid memory key or text")
        if kind not in LONG_KINDS:
            raise ValueError("invalid memory kind")
        if is_meta_memory(key, text):
            raise ValueError("not a fact about the user")
        if importance is not None and (isinstance(importance, bool) or
                                       not isinstance(importance, (int, float)) or
                                       not 0 <= importance <= 1):
            raise ValueError("importance must be between 0 and 1")
        if source_created_ms is None:
            source_created_ms = now_ms()
        async with self._locks[robot]:
            return await self._canonical_write(
                robot, self._put, robot, persona, kind, key, text.strip(), source_turn_id,
                source_created_ms, expected_version,
                MANUAL_IMPORTANCE if importance is None else float(importance),
            )

    def _change(self, robot, persona, ident, version, text, source_turn_id):
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                """SELECT version,status FROM long_term_memories
                   WHERE id=? AND robot_id=? AND (kind='semantic' OR persona_id=?)""",
                (ident, robot, persona),
            ).fetchone()
            if row is None:
                raise KeyError(ident)
            allowed = ("active",) if text else ("active", "outdated")
            if row["version"] != version or row["status"] not in allowed:
                raise ValueError("memory version conflict")
            now = now_ms()
            if text:
                # A fact a person corrected is theirs: consolidation never revises it.
                db.execute(
                    """UPDATE long_term_memories SET text=?,version=version+1,source_turn_id=?,
                       source_created_ms=?,updated_ms=?,origin='manual',superseded_by=NULL
                       WHERE id=?""",
                    (text, source_turn_id, now, now, ident),
                )
            else:
                db.execute(
                    """UPDATE long_term_memories SET status='deleted',version=version+1,
                       index_state='deleted',embedding=NULL,updated_ms=? WHERE id=?""",
                    (now, ident),
                )
        return version + 1

    async def change(self, robot, persona, ident, version, text=None, *, source_turn_id=None):
        if text is not None and not 1 <= len(text.strip()) <= 300:
            raise ValueError("invalid memory text")
        async with self._locks[robot]:
            next_version = await self._canonical_write(
                robot, self._change, robot, persona, ident, version,
                text.strip() if text else None, source_turn_id,
            )
        return {"id": ident, "version": next_version,
                "status": "active" if text else "deleted",
                "scope": "long_term_memory_only",
                "conversation_history_deleted": False}

    def _restore(self, robot, persona, ident, version):
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                """SELECT version,status FROM long_term_memories
                   WHERE id=? AND robot_id=? AND (kind='semantic' OR persona_id=?)""",
                (ident, robot, persona),
            ).fetchone()
            if row is None:
                raise KeyError(ident)
            if row["version"] != version or row["status"] != "outdated":
                raise ValueError("memory version conflict")
            now = now_ms()
            db.execute(
                """UPDATE long_term_memories SET status='active',superseded_by=NULL,
                   version=version+1,updated_ms=?,last_hit_ms=? WHERE id=?""",
                (now, now, ident),
            )
        return version + 1

    async def restore(self, robot, persona, ident, version):
        """Undo a retirement the consolidator got wrong; forgotten facts stay forgotten."""
        memory_namespace(robot, persona, "persona")
        async with self._locks[robot]:
            next_version = await self._canonical_write(
                robot, self._restore, robot, persona, ident, version,
            )
        return {"id": ident, "version": next_version, "status": "active"}

    def _insert(self, db, robot, persona, kind, text, importance, source, now):
        ident = uuid4().hex
        row = {"id": ident, "robot_id": robot, "persona_id": "" if kind == "semantic" else persona,
               "kind": kind, "fact_key": f"auto_{ident[:16]}", "text": text,
               "source_turn_id": source["turn_id"], "source_created_ms": source["created_ms"],
               "version": 1, "status": "active", "updated_ms": now,
               "index_state": "not_required", "importance": importance, "hits": 0,
               "hit_days": 0, "last_hit_ms": None, "superseded_by": None, "origin": "auto"}
        names = COLUMNS.split(",")
        db.execute(
            f"INSERT INTO long_term_memories ({COLUMNS}) VALUES ({','.join('?' for _ in names)})",
            tuple(row[name] for name in names),
        )
        return row

    def _learn(self, db, robot, persona, kind, text, importance, rows, source, now, *, skip=None):
        """Add a fact unless it was forgotten or is already known: (result, row)."""
        for row in rows:
            if (row["status"] == "deleted" and row["text"]
                    and similarity(row["text"], text) >= SAME_FACT):
                return "blocked", row
        twin = next((row for row in rows if row is not skip and row["status"] == "active"
                     and similarity(row["text"], text) >= SAME_FACT), None)
        if twin is not None and (twin["text"] == text or twin["origin"] == "manual"
                                 and similarity(twin["text"], text) >= 0.9):
            twin["importance"] = max(importance_of(twin), importance)
            db.execute("UPDATE long_term_memories SET importance=? WHERE id=?",
                       (twin["importance"], twin["id"]))
            return "known", twin
        if twin is not None and twin["origin"] != "manual":
            # A close restatement replaces the older wording, as in Aurabot.
            twin.update(text=text, version=twin["version"] + 1, updated_ms=now,
                        importance=max(importance_of(twin), importance),
                        source_turn_id=source["turn_id"], source_created_ms=source["created_ms"])
            db.execute(
                """UPDATE long_term_memories SET text=?,version=?,updated_ms=?,importance=?,
                   source_turn_id=?,source_created_ms=? WHERE id=?""",
                (text, twin["version"], now, twin["importance"], source["turn_id"],
                 source["created_ms"], twin["id"]),
            )
            return "merged", twin
        row = self._insert(db, robot, persona, kind, text, importance, source, now)
        rows.append(row)
        return "added", row

    def _apply_one(self, db, robot, persona, op, rows, source, now):
        entry = {"op": op["op"], "kind": op["kind"], "text": op["text"],
                 "importance": op["importance"]}
        text = None if op.get("ungrounded") else prepare_consolidated_fact(op["kind"], op["text"])
        if text is None:
            return {**entry, "result": "rejected"}
        entry["text"] = text
        target = next((row for row in rows if row["id"] == op["target"]
                       and row["status"] == "active" and row["origin"] != "manual"), None)
        if op["op"] == "update" and target is not None:
            if any(row["status"] == "deleted" and row["text"] and
                   similarity(row["text"], text) >= SAME_FACT for row in rows):
                return {**entry, "result": "blocked"}
            target.update(text=text, version=target["version"] + 1, updated_ms=now,
                          importance=max(importance_of(target), op["importance"]),
                          source_turn_id=source["turn_id"], source_created_ms=source["created_ms"])
            db.execute(
                """UPDATE long_term_memories SET text=?,version=?,updated_ms=?,importance=?,
                   source_turn_id=?,source_created_ms=? WHERE id=?""",
                (text, target["version"], now, target["importance"], source["turn_id"],
                 source["created_ms"], target["id"]),
            )
            return {**entry, "result": "updated", "id": target["id"]}
        kind = target["kind"] if target is not None else op["kind"]
        result, row = self._learn(db, robot, persona, kind, text, op["importance"], rows, source,
                                  now, skip=target)
        if op["op"] != "outdated" or target is None:
            return {**entry, "result": result, "id": row["id"]}
        newer = None if result == "blocked" else row["id"]
        target.update(status="outdated", superseded_by=newer, version=target["version"] + 1,
                      updated_ms=now)
        db.execute(
            """UPDATE long_term_memories SET status='outdated',superseded_by=?,version=?,updated_ms=?
               WHERE id=?""",
            (newer, target["version"], now, target["id"]),
        )
        return {**entry, "result": "outdated", "id": newer, "target": target["id"]}

    def _apply(self, robot, persona, chat, ops, source):
        """Write one conversation's ops: [{op, kind, text, importance, result, id, target}]."""
        now = now_ms()
        results = []
        ops = [{**op, "kind": safe_consolidated_scope(op["kind"], op["text"])}
               if op["op"] == "add" else op for op in ops]
        longs = [op for op in ops if op["kind"] != "chat"]
        if longs:
            with self._connect() as db:
                db.execute("BEGIN IMMEDIATE")
                rows = [dict(row) for row in db.execute(
                    f"""SELECT {COLUMNS} FROM long_term_memories
                        WHERE robot_id=? AND (kind='semantic' OR persona_id=?)
                        AND status IN ('active','deleted')""",
                    (robot, persona),
                )]
                results = [self._apply_one(db, robot, persona, op, rows, source, now)
                           for op in longs]
        existing = None
        for op in ops:
            if op["kind"] != "chat":
                continue
            entry = {"op": "add", "kind": "chat", "text": op["text"], "importance": op["importance"]}
            text = None if op.get("ungrounded") else prepare_consolidated_fact("chat", op["text"])
            if text is None:
                results.append({**entry, "result": "rejected"})
                continue
            if existing is None:
                existing = self.store.list_chat_memories(robot, persona, chat, False)
            if any(similarity(row["text"], text) >= SAME_FACT for row in existing):
                results.append({**entry, "text": text, "result": "known"})
                continue
            saved = self.store.put_chat_memory(
                robot, persona, chat, f"auto_{uuid4().hex[:16]}", text,
                source["turn_id"], source["created_ms"], None,
            )
            if saved is not None:
                existing.append({"text": text})
            results.append({**entry, "text": text, "result": "added" if saved else "skipped",
                            "id": saved[0] if saved else None})
        return results

    async def _consolidate(self, robot, persona, chat, turns, source):
        started = now_ms()
        entry = await self._index(robot, persona)
        said = "\n".join(user for _created, user, _reply in turns if user)
        related = [hit["row"] for hit in await asyncio.to_thread(
            entry["full"].search, said[-2000:], started,
            limit=RELATED_LIMIT, min_score=RELATED_MIN_SCORE,
        )]
        system, user, aliases = consolidate_request(turns, related, started)
        reply = await self.chat.complete(system, user, max_tokens=CONSOLIDATE_TOKENS)
        ops = ground_fondness(parse_ops(reply, aliases), said)
        async with self._locks[robot]:
            results = await self._canonical_write(
                robot, self._apply, robot, persona, chat, ops, source,
                changed=lambda items: any(item["result"] in CHANGES for item in items),
            )
        self.consolidations[robot].appendleft({
            "at_ms": now_ms(), "persona_id": persona, "chat_id": chat,
            "turns": sum(1 for _created, user, _reply in turns if user),
            "related": len(related), "results": results, "ms": now_ms() - started,
        })

    def _pending(self, robot):
        """Turns after the cursor; on first enable the cursor starts at the end."""
        with self._connect() as db:
            progress = db.execute(
                "SELECT turn_row_id FROM memory_progress WHERE robot_id=?", (robot,),
            ).fetchone()
            if progress is None:
                # Do not unexpectedly bill for years of old dialogue on first enable.
                last = db.execute(
                    "SELECT COALESCE(MAX(id),0) FROM conversation_turns WHERE robot_id=?",
                    (robot,),
                ).fetchone()[0]
                db.execute("INSERT INTO memory_progress VALUES (?,?)", (robot, last))
                return []
            rows = db.execute(
                """SELECT id,turn_id,chat_id,persona_id,user_text,assistant_generated_text,
                   assistant_spoken_text,turn_source,status,created_ms,updated_ms
                   FROM conversation_turns WHERE robot_id=? AND id>? ORDER BY id LIMIT ?""",
                (robot, progress["turn_row_id"], PENDING_LIMIT),
            ).fetchall()
        return [dict(row) for row in rows]

    def _advance(self, robot, row_id):
        with self._connect() as db:
            db.execute("UPDATE memory_progress SET turn_row_id=? WHERE robot_id=? AND turn_row_id<?",
                       (row_id, robot, row_id))

    def _note_pending(self, robot, rows):
        said = [row for row in rows if _said(row)]
        self.pending[robot] = {
            "turns": len(said), "since_ms": said[0]["created_ms"],
            "last_ms": max(row["updated_ms"] for row in rows),
        } if said else None

    async def _worker(self, robot):
        failures = 0
        while self.running:
            batch = None
            try:
                rows = await asyncio.to_thread(self._pending, robot)
                self._note_pending(robot, rows)
                batch = ready_batch(rows, now_ms(), self.idle_ms, self.max_turns)
                if batch is None:
                    await asyncio.sleep(POLL_SECONDS)
                    continue
                chosen, last_id = batch
                for persona, chat, turns, source in conversations(chosen):
                    await self._consolidate(robot, persona, chat, turns, source)
                await asyncio.to_thread(self._advance, robot, last_id)
                failures = 0
                self.errors.pop(robot, None)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                failures += 1
                self.errors[robot] = type(exc).__name__
                log.exception("memory consolidation failed robot=%s", robot)
                if batch is not None and failures >= CONSOLIDATE_ATTEMPTS:
                    # A batch the model keeps failing on must not stall memory for good.
                    self.consolidations[robot].appendleft({
                        "at_ms": now_ms(), "error": type(exc).__name__, "skipped_to": batch[1],
                    })
                    await asyncio.to_thread(self._advance, robot, batch[1])
                    failures = 0
                await asyncio.sleep(POLL_SECONDS * 6 * max(1, failures))

    async def recap(self, robot, previous, turns):
        """Fold turns that left the realtime window into this conversation's recap."""
        system, user = recap_request(previous, turns)
        text = parse_recap(await self.chat.complete(system, user, max_tokens=RECAP_TOKENS))
        folded = (self.recaps.get(robot) or {}).get("folded", 0) if previous else 0
        self.recaps[robot] = {"text": text, "updated_ms": now_ms(), "folded": folded + len(turns)}
        return text

    def end_recap(self, robot):
        self.recaps.pop(robot, None)

    def insight(self, robot):
        """What the workbench shows about recall and consolidation."""
        return {
            "recalls": list(self.recalls.get(robot, ())),
            "recall_turns": self.recall_turns.get(robot, 0),
            "consolidations": list(self.consolidations.get(robot, ())),
            "pending": self.pending.get(robot),
            "recap": self.recaps.get(robot),
            "error": self.errors.get(robot),
            "idle_seconds": self.idle_ms // 1000,
            "max_turns": self.max_turns,
        }

    async def start(self):
        self.running = True
        self.tasks = [
            asyncio.create_task(self._worker(robot), name=f"memory:{robot}")
            for robot in self.robot_ids
        ]

    async def close(self):
        self.running = False
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        self.tasks.clear()
        close = getattr(self.chat, "close", None)
        if close is not None:
            await close()

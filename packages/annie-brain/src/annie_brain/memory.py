"""Bounded, per-robot long-term facts. The conversation ledger stays untouched.

The SQLite row is canonical. Embeddings are optional derived data and never
participate in correction/deletion authority. A tombstone defeats old workers.
"""

import asyncio
import json
import logging
import re
import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import httpx
import sqlite_vec
from langchain_openai import ChatOpenAI
from langmem import create_memory_manager
from pydantic import BaseModel, Field

from annie_brain.memory_scope import memory_namespace

log = logging.getLogger(__name__)
MAX_CANDIDATES = 8
MAX_EVIDENCE = 4
EVIDENCE_BYTES = 256  # byte proxy, not a provider token measurement
MEMORY_REINDEX_DEADLINE_SEC = 1.2
# Session-start profile. A small store is cheaper to inject than to recall:
# recall_memory costs an extra model round on the spoken path.
BOOTSTRAP_BYTES = 1024
BOOTSTRAP_LONG_FACTS = 6
BEIJING = timezone(timedelta(hours=8))
RECALL_DEADLINE_SEC = 1.2  # explicit memory tool only; normal chat never waits

_EMPTY_FACT_KEY = re.compile(
    r"^(?:no[-_]?extractable(?:[-_]?content)?|no[-_]?memory|no[-_]?fact|"
    r"no[-_]?extraction|empty[-_]?memory|none|null|placeholder|no[-_]?durable[-_]?fact)$", re.I,
)
_EMPTY_FACT_TEXT = re.compile(
    r"(?:未提取到|未发现|无(?:法|明确)?|没有|暂无|未能).{0,45}"
    r"(?:长期记忆|持久偏好|有意义经历|事实|提取|可记忆内容)"
)
_DIRECT_LIKING = re.compile(r"^(?:(?:小安妮|安妮)[，, ]*)?我喜欢你[。！!~～ ]*$")
_LIKING_FACT = re.compile(r"(?:表达|表示|说过|说了).{0,10}喜欢|[（(“\"]我喜欢你")
_RELATIVE_DATE = re.compile(r"下周|下星期|下个月|明天|后天|这周末|下周末")
_VISIT = re.compile(r"朋友|同学|家人|亲戚|客户")
_MEETING = re.compile(r"来|找|见|约|聚|拜访")
_UNCERTAIN = re.compile(r"[?？]|(?:吗|是否|会不会|有没有|如果|假如|假设|可能|取消|不来|没来|不找|没找|不会来|没有来)")
_VISIT_RETRACTED = re.compile(r"取消|不来|没来|不找|没找|不会来|没有来|忘记|遗忘|删掉|不再记|不要记|改期|改天")
_REMINDER_REQUEST = re.compile(r"(?:提醒|闹钟|定时|倒计时).{0,35}(?:分钟|小时|秒|点)|(?:分钟|小时|秒)后.{0,16}提醒")
_SINGLE_LOCATION = re.compile(r"^(?:北京|上海|广州|深圳|成都|杭州|西安|天津|重庆|武汉|南京|苏州|郑州|长沙)(?:的|吧|呢|呀|[。！! ]*)$")
_INCOMPLETE_PREFERENCE = re.compile(r"^(?:我)?不喜欢(?:带|吃|穿|用|看|买|去)[。！! ]*$")
_PLAN_CAR = re.compile(r"我.{0,12}(?:准备|打算|计划).{0,12}看车")
_PLAN_CAR_CANCEL = re.compile(r"(?:我.{0,8})?(?:不准备|不打算|不想|取消|放弃).{0,6}看车|(?:车已经买|已经买车|买好车了)")
_PLAN_KEY = "ongoing_plan_car_viewing"
# What the user calls Annie is not a fact about the user; a model once read
# "用户称呼当前AI角色为“小bug”" as the user's own name and greeted them with it.
_ASSISTANT_NICKNAME = re.compile(
    r"用户(?:称呼|叫|管)(?:当前|这个)?(?:的)?(?:AI|ai|助手|机器人|角色|小安妮|你)")
# Many visitors talk to one robot: "the user's name" is not a fact about whoever
# speaks next (10/21 demo: every visitor was greeted as “海超”). Names come from
# the camera's face library only.
_USER_NAME = re.compile(r"用户(?:的)?(?:名字|姓名|名)(?:是|叫|为)|用户(?:自称|名叫|叫做)")
_USER_NAME_KEY = re.compile(r"^(?:user_)?(?:real_)?(?:name|nickname|called)(?:_|$)", re.I)
_PLAN_LIFETIME_MS = 30 * 86_400_000


def _explicit_visit(text):
    """Only an affirmative user utterance, never a question or model summary."""
    return bool(_RELATIVE_DATE.search(text) and _VISIT.search(text)
                and _MEETING.search(text) and not _UNCERTAIN.search(text))


def _grounded_temporal_memory(row):
    """Legacy episode summaries cannot claim a visit absent from their source."""
    if not _RELATIVE_DATE.search(row["text"]) or row.get("source_turn_id") is None:
        return True
    return _explicit_visit(row.get("source_user_text") or "")


def _grounded_extraction(row):
    """A proven malformed old extraction is excluded without deleting it."""
    source = row.get("source_user_text")
    if not row.get("source_turn_id") or source is None:
        return True
    candidate = {"kind": row["kind"], "key": row["fact_key"], "text": row["text"]}
    result = prepare_extracted_fact(candidate, source)
    return result is not None and result[0] == row["kind"]


def is_meta_memory(key, value):
    """Historical empty-result prose is not conversation evidence."""
    return bool(_EMPTY_FACT_KEY.fullmatch(key) or _EMPTY_FACT_TEXT.search(value)
                or _USER_NAME_KEY.search(key)
                or value.startswith(("placeholder", "用户询问", "用户曾询问",
                                     "用户问过", "用户只是询问", "用户提及地点为",
                                     "用户称呼助手", "本轮没有", "本轮未"))
                or _ASSISTANT_NICKNAME.search(value) or _USER_NAME.search(value)
                or (_REMINDER_REQUEST.search(value) and "用户要求" in value)
                or value in {"用户不喜欢带。", "用户不喜欢吃。", "用户不喜欢穿。"}
                or (key.startswith("chat_") and "用户最近准备去看车" in value)
                or re.search(r"测试其(?:物理动作|安装软件)能力", value))


def prepare_extracted_fact(fact, user_text):
    """Validate a model suggestion; absence of facts is never a memory fact."""
    if not isinstance(fact, dict):
        return None
    kind, key, value = fact.get("kind"), fact.get("key"), fact.get("text")
    if (not isinstance(kind, str) or
            kind not in ("semantic", "persona", "episodes", "chat") or
            not isinstance(key, str) or not isinstance(value, str)):
        return None
    key, value = key.strip(), value.strip()
    original = user_text.strip()
    # Plans are dated, cross-Chat episodic facts; never global permanent tastes.
    # Normalize the key, scope and grammatical subject independently of the
    # model's arbitrary extraction labels.
    if _PLAN_CAR.search(original) and not _PLAN_CAR_CANCEL.search(original):
        return "episodes", _PLAN_KEY, "用户最近准备去看车，进展尚未确认。"
    if (_REMINDER_REQUEST.search(original) or
            _SINGLE_LOCATION.fullmatch(original) or
            _INCOMPLETE_PREFERENCE.fullmatch(original) or
            "用户提及地点" in value or "用户称呼助手" in value or
            _ASSISTANT_NICKNAME.search(value) or _USER_NAME.search(value) or
            _USER_NAME_KEY.search(key) or
            "用户要求" in value and _REMINDER_REQUEST.search(value)):
        return None
    if _PLAN_CAR_CANCEL.search(original):
        return None
    if (kind == "episodes" and re.search(r"今天工作|上班.*累|感觉.*辛苦", original)):
        return None
    if (kind == "persona" and re.search(r"好感|喜欢|爱意|爱你", value)
            and not re.search(r"我(?:喜欢|爱)你|爱小安妮|喜欢小安妮", original)):
        return None
    if (kind == "persona" and "要求" in value and
            not re.search(r"别|不要|不许|不准|以后|希望|要求", original)):
        return None
    if ("你" in value[:3] or value.startswith(("我", "助手", "机器人"))):
        return None
    if (not 1 <= len(key) <= 64 or not 1 <= len(value) <= 300 or
            is_meta_memory(key, value) or
            value.startswith(("用户询问", "用户曾询问", "用户问过", "用户只是询问",
                              "本轮没有", "本轮未", "placeholder")) or
            re.search(r"测试其(?:物理动作|安装软件)能力", value)):
        return None
    kind = safe_extracted_scope(kind, user_text)
    if _RELATIVE_DATE.search(value) and _VISIT.search(value) and not _explicit_visit(user_text):
        return None
    if kind == "persona" and _DIRECT_LIKING.fullmatch(user_text.strip()):
        # Record the observed utterance, not a permanent affection assessment.
        return kind, "user_expressed_liking", "用户曾向当前 Persona 表达喜欢。"
    return kind, key, value


def same_extracted_fact(existing, kind, key, value):
    """Conservative same-scope deduplication, not speculative semantic merging."""
    normalized = re.sub(r"[\s，。、“”‘’'\"（）()！!？?：:；;]+", "", value).casefold()
    for row in existing:
        if row["kind"] != kind:
            continue
        if (row["fact_key"] == key and key == _PLAN_KEY
                and "进展尚未确认" in row["text"]):
            return True
        previous = row["text"]
        if (normalized == re.sub(
            r"[\s，。、“”‘’'\"（）()！!？?：:；;]+", "", previous,
        ).casefold()):
            return True
        if (kind == "persona" and key == "user_expressed_liking" and
                (row["fact_key"] == key or _LIKING_FACT.search(previous))):
            return True
    return False


def safe_extracted_scope(kind, user_text):
    """A narrow hard boundary: a one-off prohibition cannot become a profile."""
    if kind not in ("semantic", "persona", "episodes"):
        return kind
    text = user_text.strip()
    if (re.fullmatch(r"(?:你)?(?:不能|不要|别|不许|不准)骂我[。！!？?]?", text)
            or kind == "episodes" and re.search(r"下周|明天|后天|下个月", text)):
        return "chat"
    return kind


class MemoryCandidate(BaseModel):
    key: str = Field(min_length=1, max_length=64)
    kind: str = Field(pattern="^(semantic|persona|episodes|chat)$")
    text: str = Field(min_length=1, max_length=300)


class CloudMemoryModel:
    """Alibaba Cloud OpenAI-compatible text extraction and embedding."""

    def __init__(self, key, base_url, extract_model="qwen3.8-flash",
                 embedding_model="text-embedding-v4", dimensions=256):
        self.key = key
        self.url = base_url.rstrip("/")
        self.extract_model = extract_model
        self.embedding_model = embedding_model
        self.dimensions = dimensions
        if dimensions not in (64, 128, 256, 512, 768, 1024, 1536, 2048):
            raise ValueError("unsupported embedding dimensions")
        self.client = httpx.AsyncClient(
            headers={"Authorization": f"Bearer {key}"}, timeout=12,
            limits=httpx.Limits(max_connections=2, max_keepalive_connections=2),
        )
        # LangMem performs extraction only after a turn; canonical writes stay
        # in our versioned SQLite rows so an old model task cannot undo forget.
        self.manager = create_memory_manager(
            ChatOpenAI(
                model=extract_model, api_key=key, base_url=self.url,
                timeout=12, max_retries=0,
                extra_body={"enable_thinking": False},
            ),
            schemas=[MemoryCandidate],
            enable_deletes=False,
            instructions=(
                "只从用户自己的原话提取，不推测、不记助手编造内容。最多两条，可不提取。"
                "没有真实可保存事实必须返回空列表，绝不可把‘未提取到内容’、"
                "‘用户只是询问’或分类结论当成记忆。"
                "记忆正文使用客观第三人称，以‘用户’为主语，不写助手自述、"
                "永久情感判断或‘对该机器人persona有正面好感’等推论。"
                "如用户说‘我喜欢你’，只记‘用户曾向当前角色表达喜欢’，"
                "不能升级为‘用户永远喜欢机器人’。"
                "当前Chat的说话方式与临时约束用chat，例如‘你不能骂我’、"
                "‘今天别开玩笑’，绝不擅自升级永久偏好。"
                "用户明确表示最近准备去看车是跨Chat有时效的进行中计划，"
                "用episodes，key=ongoing_plan_car_viewing，状态必须待确认；"
                "完成或取消不再当成待做计划。不能把‘北京的’推断为居住北京。"
                "提醒、查询、称呼、残缺短句、临时疲惫和重复闲聊都不提取。"
                "明确稳定的用户偏好用semantic，明确针对当前AI角色的长期关系事实"
                "用persona；已发生且值得记住的角色经历用episodes。"
                "询问能力、测试口令、一次性动作请求、假设、闲聊及助手说过的话不抽取；"
                "不要把‘用户曾询问/测试某功能’当成长期关系或个人经历。"
                "只有用户明确表示‘以后一直’等持久意图时才把当轮限制写入长期。"
                "同义重复不要生成新记录，改口复用existing的fact key；简短中文。"
            ),
        )

    async def close(self):
        await self.client.aclose()

    async def extract(self, user_text, existing, *, context=None):
        if not user_text.strip():
            return []
        previous = [
            (row["id"], "MemoryCandidate", MemoryCandidate(
                key=row["fact_key"], kind=row["kind"], text=row["text"],
            ))
            for row in existing[:16]
            if row["kind"] in ("semantic", "persona", "episodes", "chat")
        ]
        current = user_text[:1500]
        if context:
            current = (
                "以下旧对话仅供指代消歧，不能从旧消息或助手回复抽取事实；"
                "只抽取 current_user 中用户本人明确说出的事实。\n"
                + json.dumps({"previous_dialogue": context, "current_user": current},
                             ensure_ascii=False)
            )
        extracted = await self.manager.ainvoke({
            "messages": [{"role": "user", "content": current}],
            "existing": previous, "max_steps": 1,
        })
        return [
            item.content.model_dump()
            for item in extracted if isinstance(item.content, MemoryCandidate)
        ][:2]

    async def embed(self, text):
        response = await self.client.post(self.url + "/embeddings", json={
            "model": self.embedding_model, "input": text, "dimensions": self.dimensions,
            "encoding_format": "float",
        })
        response.raise_for_status()
        vector = response.json()["data"][0]["embedding"]
        if len(vector) != self.dimensions:
            raise ValueError("embedding dimension mismatch")
        return vector


class MemoryService:
    """Async boundary over canonical SQLite rows and a bounded background scan."""

    def __init__(self, store, model, robot_ids):
        self.store = store
        self.model = model
        self.robot_ids = tuple(robot_ids)
        self.tasks = []
        self.running = False
        self.errors = {}
        self._locks = {robot: asyncio.Lock() for robot in self.robot_ids}
        self.on_change = None  # local canonical changes, not a per-turn model call

    def _changed(self, robot):
        if self.on_change is not None:
            self.on_change(robot)

    async def _canonical_write(self, robot, method, *args):
        # to_thread cancellation cannot abort SQLite: notify from the committed
        # worker, even when its tool/request coroutine has already been retired.
        loop = asyncio.get_running_loop()

        def write():
            result = method(*args)
            if result is not None:
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
            db.enable_load_extension(True)
            sqlite_vec.load(db)
            db.enable_load_extension(False)
            with db:
                yield db
        finally:
            db.close()

    def _list(self, robot, persona, include_deleted=False):
        with self._connect() as db:
            rows = db.execute(
                """SELECT m.id,m.robot_id,m.persona_id,m.kind,m.fact_key,m.text,
                   m.source_turn_id,m.source_created_ms,m.version,m.status,
                   m.updated_ms,m.index_state,t.user_text AS source_user_text
                   FROM long_term_memories m LEFT JOIN conversation_turns t
                   ON t.robot_id=m.robot_id AND t.turn_id=m.source_turn_id
                   WHERE m.robot_id=? AND (m.kind='semantic' OR m.persona_id=?)
                   AND (? OR m.status='active') ORDER BY m.updated_ms DESC LIMIT 256""",
                (robot, persona, int(include_deleted)),
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["source_valid"] = _grounded_extraction(item) and (
                                    item["kind"] != "episodes" or
                                    _grounded_temporal_memory(item)) and (
                                        item["fact_key"] != _PLAN_KEY or
                                        bool(_PLAN_CAR.search(item.get("source_user_text") or "")))
            item.pop("source_user_text", None)
            result.append(item)
        return result

    async def list(self, robot, persona, include_deleted=False):
        memory_namespace(robot, persona, "persona")
        return await asyncio.to_thread(self._list, robot, persona, include_deleted)

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
            type(expires_at_ms) is not int or expires_at_ms <= time.time_ns() // 1_000_000
        ):
            raise ValueError("expiration must be in the future")
        if source_created_ms is None:
            source_created_ms = time.time_ns() // 1_000_000
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
        """Bounded local evidence when a Realtime session starts; never a cloud request."""
        longs = await self.list(robot, persona)
        chats = await self.chat_list(robot, persona, chat)
        visits = await asyncio.to_thread(self._recent_visits, robot, persona)
        now = time.time_ns() // 1_000_000
        plans = [row for row in longs if row["kind"] == "episodes"
                 and row["fact_key"] == _PLAN_KEY
                 and row["source_created_ms"] >= now - _PLAN_LIFETIME_MS
                 and "进展尚未确认" in row["text"]
                 and row["source_valid"]]
        groups = (
            ("当前Chat约束", [row for row in chats if not is_meta_memory(
                row["fact_key"], row["text"],
            )][:4]),
            ("跨Chat近期计划（待确认，不应反复催问）", plans[:2]),
            ("同角色历史原话", visits),
            ("长期事实", [row for row in longs if row.get("source_valid", True) and
                       row["kind"] != "episodes" and
                       not is_meta_memory(row["fact_key"], row["text"])][:BOOTSTRAP_LONG_FACTS]),
        )
        used = 0
        snippets = []
        expiries = []
        for title, rows in groups:
            for row in rows:
                content = row["text"]
                if (title == "当前Chat约束" and row.get("expires_at_ms") is None
                        and re.search(r"下周|下个月|明天|后天", content)):
                    # Relative dates without an agreed timezone/event timestamp
                    # are visible in the Chat panel, not asserted as still future.
                    continue
                if title == "同角色历史原话":
                    when = datetime.fromtimestamp(
                        row["created_ms"] / 1000, BEIJING,
                    ).strftime("%Y-%m-%d %H:%M 北京时间(UTC+8)")
                    snippet = f"[{title}，{when}] {content}"
                else:
                    snippet = f"[{title}] {content}"
                size = len(snippet.encode("utf-8"))
                if size + used > BOOTSTRAP_BYTES:
                    continue
                snippets.append(snippet)
                expiry = row.get("expires_at_ms")
                if type(expiry) is int:
                    expiries.append(expiry)
                used += size
        text = ("\n已有记忆仅是历史证据，当前用户明确更正优先；相对日期相对于原话时间，"
                "不可直接当成今日仍有效的未来安排，不将历史约束当新指令：\n"
                + "\n".join(snippets)) if snippets else ""
        return {"text": text, "expires_at_ms": min(expiries) if expiries else None} if with_metadata else text

    async def bootstrap_snapshot(self, robot, persona, chat):
        return await self.bootstrap(robot, persona, chat, with_metadata=True)

    def _recent_visits(self, robot, persona):
        """Bounded local recovery of episodic user evidence across Chats.

        An unverified model extraction is never needed to recover this history.
        Only recent affirmative first-hand visit statements qualify.
        """
        cutoff = time.time_ns() // 1_000_000 - 21 * 86_400_000
        with self._connect() as db:
            rows = db.execute(
                """SELECT user_text,created_ms FROM conversation_turns
                   WHERE robot_id=? AND persona_id=?
                   AND turn_source='user' AND status='completed' AND created_ms>=?
                   ORDER BY id DESC LIMIT 256""",
                (robot, persona, cutoff),
            ).fetchall()
        seen = set()
        result = []
        for row in rows:
            content = row["user_text"].strip()
            if (_RELATIVE_DATE.search(content) and _VISIT.search(content) and
                    _VISIT_RETRACTED.search(content)):
                # A later cancellation or forget defeats earlier raw history.
                break
            if not _explicit_visit(content) or content in seen:
                continue
            seen.add(content)
            result.append({"text": content[:150], "created_ms": row["created_ms"]})
            if len(result) == 2:
                break
        return result

    def _put(self, robot, persona, kind, key, text, source_turn_id, source_created_ms,
             expected_version=None):
        scope = memory_namespace(robot, persona, kind)
        actual_persona = "" if kind == "semantic" else scope[3]
        now = time.time_ns() // 1_000_000
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                """SELECT id,version,status,updated_ms,source_created_ms
                   FROM long_term_memories
                   WHERE robot_id=? AND persona_id=? AND kind=? AND fact_key=?""",
                (robot, actual_persona, kind, key),
            ).fetchone()
            if source_turn_id is None and row is not None and expected_version is None:
                raise ValueError("memory key exists; use the versioned correction endpoint")
            if expected_version is not None and (row is None or row["version"] != expected_version):
                raise ValueError("memory version conflict")
            # A forgotten key is a permanent tombstone for automatic extraction:
            # a stale Ledger replay or a later ambiguous reference must not
            # repopulate the user's explicitly erased long-term memory.
            if row is not None and row["status"] == "deleted":
                return None
            # An old extraction cannot undo a manual correction or forget.
            if row is not None and source_created_ms <= row["source_created_ms"]:
                return None
            ident, version = (row["id"], row["version"] + 1) if row else (uuid4().hex, 1)
            db.execute(
                """INSERT INTO long_term_memories
                   (id,robot_id,persona_id,kind,fact_key,text,source_turn_id,
                    source_created_ms,version,status,updated_ms,index_state,embedding)
                   VALUES (?,?,?,?,?,?,?,?,?,'active',?,'pending',NULL)
                   ON CONFLICT(robot_id,persona_id,kind,fact_key) DO UPDATE SET
                     text=excluded.text,source_turn_id=excluded.source_turn_id,
                     source_created_ms=excluded.source_created_ms,version=excluded.version,
                     status='active',updated_ms=excluded.updated_ms,
                     index_state='pending',embedding=NULL""",
                (ident, robot, actual_persona, kind, key, text, source_turn_id,
                 source_created_ms, version, now),
            )
        return ident, version

    async def put(self, robot, persona, kind, key, text, *, source_turn_id=None,
                  source_created_ms=None, expected_version=None):
        if (not isinstance(key, str) or not 1 <= len(key) <= 64 or
                not isinstance(text, str) or not 1 <= len(text.strip()) <= 300):
            raise ValueError("invalid memory key or text")
        if kind not in ("semantic", "persona", "episodes"):
            raise ValueError("invalid memory kind")
        if is_meta_memory(key, text):
            raise ValueError("extraction summary is not a memory fact")
        if source_created_ms is None:
            source_created_ms = time.time_ns() // 1_000_000
        async with self._locks[robot]:
            result = await self._canonical_write(
                robot, self._put, robot, persona, kind, key, text.strip(), source_turn_id,
                source_created_ms, expected_version,
            )
        if result:
            ident, version = result
            # Indexing never holds the correction/forget lock. Version-gated write.
            try:
                vector = await self.model.embed(text.strip())
                await asyncio.to_thread(self._index, robot, ident, version, vector)
            except Exception as exc:
                log.warning("memory embedding failed robot=%s: %s", robot, type(exc).__name__)
                self.errors[robot] = "embedding_failed"
                await asyncio.to_thread(self._mark_index_failed, robot, ident, version)
        return result

    def _index(self, robot, ident, version, vector):
        with self._connect() as db:
            db.execute(
                """UPDATE long_term_memories SET embedding=?,index_state='ready'
                   WHERE robot_id=? AND id=? AND version=? AND status='active'""",
                (sqlite_vec.serialize_float32(vector), robot, ident, version),
            )

    def _mark_index_failed(self, robot, ident, version):
        with self._connect() as db:
            db.execute(
                """UPDATE long_term_memories SET index_state='failed'
                   WHERE robot_id=? AND id=? AND version=? AND status='active'""",
                (robot, ident, version),
            )

    def _change(self, robot, persona, ident, version, text, source_turn_id):
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                """SELECT id,kind,persona_id,version,status FROM long_term_memories
                   WHERE id=? AND robot_id=? AND (kind='semantic' OR persona_id=?)""",
                (ident, robot, persona),
            ).fetchone()
            if row is None:
                raise KeyError(ident)
            if row["version"] != version or row["status"] != "active":
                raise ValueError("memory version conflict")
            now = time.time_ns() // 1_000_000
            # Removing the vector in the same transaction makes stale search impossible.
            db.execute(
                """UPDATE long_term_memories SET text=?,embedding=NULL,
                   index_state=?,status=?,version=version+1,
                   source_turn_id=?,source_created_ms=?,updated_ms=?
                   WHERE id=?""",
                (text or "", "pending" if text else "deleted",
                 "active" if text else "deleted", source_turn_id, now, now, ident),
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
        if text:
            try:
                vector = await asyncio.wait_for(
                    self.model.embed(text.strip()), MEMORY_REINDEX_DEADLINE_SEC,
                )
                await asyncio.to_thread(self._index, robot, ident, next_version, vector)
            except Exception as exc:
                self.errors[robot] = "embedding_failed"
                log.warning("memory reindex failed robot=%s: %s", robot, type(exc).__name__)
                await asyncio.to_thread(self._mark_index_failed, robot, ident, next_version)
        return {"id": ident, "version": next_version,
                "status": "active" if text else "deleted",
                "scope": "long_term_memory_only",
                "conversation_history_deleted": False}

    def _search(self, robot, persona, query, vector):
        with self._connect() as db:
            if vector is not None:
                ready = db.execute(
                    """SELECT id,kind,persona_id,fact_key,text,source_turn_id,version,
                       status,index_state,vec_distance_cosine(embedding, ?) AS distance
                       FROM long_term_memories
                       WHERE robot_id=? AND (kind='semantic' OR persona_id=?)
                       AND status='active' AND embedding IS NOT NULL
                       ORDER BY distance LIMIT ?""",
                    (sqlite_vec.serialize_float32(vector), robot, persona, MAX_CANDIDATES),
                ).fetchall()
                # Failed/pending embedding must not hide a freshly corrected
                # canonical fact. These are only eligible via lexical evidence.
                waiting = db.execute(
                    """SELECT id,kind,persona_id,fact_key,text,source_turn_id,version,
                       status,index_state,NULL AS distance FROM long_term_memories
                       WHERE robot_id=? AND (kind='semantic' OR persona_id=?)
                       AND status='active' AND embedding IS NULL
                       ORDER BY updated_ms DESC LIMIT ?""",
                    (robot, persona, MAX_CANDIDATES),
                ).fetchall()
                rows = (*ready, *waiting)
            else:
                rows = db.execute(
                    """SELECT id,kind,persona_id,fact_key,text,source_turn_id,version,
                       status,index_state FROM long_term_memories
                       WHERE robot_id=? AND (kind='semantic' OR persona_id=?)
                       AND status='active' ORDER BY updated_ms DESC LIMIT 256""",
                    (robot, persona),
                ).fetchall()
            result = [dict(row) for row in rows]
            temporal_sources = {row["source_turn_id"] for row in result
                                if row["source_turn_id"]}
            source_texts = {}
            if temporal_sources:
                placeholders = ",".join("?" for _ in temporal_sources)
                source_texts = dict(db.execute(
                    "SELECT turn_id,user_text FROM conversation_turns "
                    f"WHERE robot_id=? AND turn_source='user' AND turn_id IN ({placeholders})",
                    (robot, *temporal_sources),
                ).fetchall())
        for row in result:
            if row["source_turn_id"] in temporal_sources:
                row["source_user_text"] = source_texts.get(row["source_turn_id"])
        return result

    async def recall(self, robot, persona, query):
        memory_namespace(robot, persona, "persona")
        if not isinstance(query, str) or not 1 <= len(query.strip()) <= 300:
            raise ValueError("invalid memory query")
        started = time.monotonic()
        vector = None
        # Only an explicit memory-tool call waits; plain audio dialogue does not.
        try:
            vector = await asyncio.wait_for(
                self.model.embed(query), RECALL_DEADLINE_SEC - 0.1,
            )
        except Exception:
            pass
        remaining = RECALL_DEADLINE_SEC - (time.monotonic() - started)
        try:
            if remaining <= 0:
                raise asyncio.TimeoutError
            rows = await asyncio.wait_for(
                asyncio.to_thread(self._search, robot, persona, query, vector), remaining,
            )
        except asyncio.TimeoutError:
            return {"status": "unavailable", "evidence": [], "reason": "deadline"}
        seen = set()
        evidence = []
        used = 0
        for row in rows:
            content = row["text"]
            if (is_meta_memory(row["fact_key"], content) or
                    not _grounded_temporal_memory(row) or
                    not _grounded_extraction(row)):
                continue
            row.pop("source_user_text", None)
            if vector is not None and row["distance"] is not None and row["distance"] > 0.4:
                continue
            if content in seen:
                continue
            # Lexical-only fallback is intentionally conservative; don't inject
            # random old memories when the cloud query times out.
            if (vector is None or row.get("distance") is None) and not any(
                query[pos:pos + 2] in (content + row["fact_key"])
                for pos in range(len(query) - 1) if query[pos:pos + 2].strip()
            ):
                continue
            size = len(content.encode("utf-8"))
            if used + size > EVIDENCE_BYTES:
                continue
            evidence.append(row)
            seen.add(content)
            used += size
            if len(evidence) >= MAX_EVIDENCE:
                break
        return {"status": "ok", "evidence": evidence,
                "chars_unit": "utf8_bytes_not_provider_tokens",
                "deadline_ms": int(RECALL_DEADLINE_SEC * 1000)}

    def _next_turn(self, robot):
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
                return None
            row = db.execute(
                """SELECT id,turn_id,robot_id,chat_id,persona_id,user_text,turn_source,status,created_ms,updated_ms
                   FROM conversation_turns WHERE robot_id=? AND id>?
                   ORDER BY id LIMIT 1""",
                (robot, progress["turn_row_id"]),
            ).fetchone()
        return dict(row) if row else None

    def _nearby_dialogue(self, row):
        """Two same-Chat completed turns, only for disambiguating current text."""
        with self._connect() as db:
            rows = db.execute(
                """SELECT user_text,assistant_generated_text FROM conversation_turns
                   WHERE robot_id=? AND persona_id=? AND chat_id=? AND id<?
                   AND turn_source='user' AND status='completed'
                   ORDER BY id DESC LIMIT 2""",
                (row["robot_id"], row["persona_id"], row["chat_id"], row["id"]),
            ).fetchall()
        return [{"user": item["user_text"][:120],
                 "assistant": (item["assistant_generated_text"] or "")[:180]}
                for item in reversed(rows)]

    def _advance(self, robot, row_id):
        with self._connect() as db:
            db.execute("UPDATE memory_progress SET turn_row_id=? WHERE robot_id=? AND turn_row_id<?",
                       (row_id, robot, row_id))

    async def _worker(self, robot):
        while self.running:
            try:
                row = await asyncio.to_thread(self._next_turn, robot)
                if row is None:
                    await asyncio.sleep(1)
                    continue
                if row["turn_source"] == "user" and (
                    row["status"] == "running" or
                    row["status"] == "completed" and not row["user_text"].strip()) and (
                    time.time_ns() // 1_000_000 - row["updated_ms"] < 300_000
                ):
                    await asyncio.sleep(1)
                    continue
                if (row["turn_source"] == "user" and row["status"] == "completed"
                        and row["user_text"].strip()):
                    # Do not infer user facts from assistant-generated messages.
                    all_existing = await self.chat_list(
                        robot, row["persona_id"], row["chat_id"], True,
                    )
                    all_existing += await self.list(robot, row["persona_id"], True)
                    # Deleted facts are not offered to the model, but still
                    # prevent a newly invented key from undoing a user forget.
                    candidates = [item for item in all_existing if item["status"] == "active"
                                  and not is_meta_memory(item["fact_key"], item["text"])]
                    if isinstance(self.model, CloudMemoryModel):
                        facts = await self.model.extract(
                            row["user_text"], candidates,
                            context=await asyncio.to_thread(self._nearby_dialogue, row),
                        )
                    else:
                        facts = await self.model.extract(row["user_text"], candidates)
                    if _PLAN_CAR.search(row["user_text"]) and not _PLAN_CAR_CANCEL.search(row["user_text"]):
                        facts = [{"kind": "episodes", "key": _PLAN_KEY,
                                  "text": "用户最近准备去看车，进展尚未确认。"}, *facts][:2]
                    if _PLAN_CAR_CANCEL.search(row["user_text"]):
                        for old in candidates:
                            if old["kind"] == "episodes" and old["fact_key"] == _PLAN_KEY:
                                await self.change(
                                    robot, row["persona_id"], old["id"], old["version"],
                                    "用户此前的看车计划已结束，不能再作为待完成计划提起。",
                                    source_turn_id=row["turn_id"],
                                )
                    for fact in facts:
                        prepared = prepare_extracted_fact(fact, row["user_text"])
                        if prepared is None:
                            continue
                        kind, key, text = prepared
                        if same_extracted_fact(all_existing, kind, key, text):
                            continue
                        if kind == "chat":
                            saved = await self.chat_put(
                                robot, row["persona_id"], row["chat_id"], key, text,
                                source_turn_id=row["turn_id"],
                                source_created_ms=row["created_ms"],
                            )
                        else:
                            saved = await self.put(
                                robot, row["persona_id"], kind, key, text,
                                source_turn_id=row["turn_id"],
                                source_created_ms=row["created_ms"],
                            )
                        if saved is not None:
                            all_existing.append({
                                "kind": kind, "fact_key": key,
                                "text": text, "status": "active",
                            })
                await asyncio.to_thread(self._advance, robot, row["id"])
                self.errors.pop(robot, None)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.errors[robot] = type(exc).__name__
                log.exception("memory worker failed robot=%s", robot)
                await asyncio.sleep(5)

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
        await self.model.close()

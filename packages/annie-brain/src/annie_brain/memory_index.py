"""Local recall over long-term facts: Chinese bigrams, BM25 and a forgetting curve.

Ported from aureth-labs/dsh-aurabot-qq, src/features/memory.js (MIT License).
The index is rebuilt from canonical SQLite rows and never calls a model, so it
is cheap enough to run on every user turn.
"""

import math
import re
from datetime import datetime, timedelta, timezone

BEIJING = timezone(timedelta(hours=8))
DAY_MS = 86_400_000
K1, B = 1.4, 0.7
MIN_SCORE = 0.35
HINT_LIMIT = 4
# A hint nobody asked for needs two shared terms: one shared character is chance
# (星 in 星期 and 星际). The recall_memory tool still matches on one.
HINT_MIN_TERMS = 2
HINT_CHARS = 300
# “嗯嗯”“然后呢” find nothing on their own: the previous sentence joins the search.
SHORT_QUERY_CHARS = 12
PREVIOUS_CHARS = 300
PREVIOUS_MIN_SCORE = 0.5
PREVIOUS_WEIGHT = 0.6
FADE_IMPORTANCE = 0.6
FADE_RETENTION = 0.05

_STOP = frozenset("的了是我你他她它们在有和就不也都很吗吧呢啊呀哦嗯这那个一上下来去说要会能着过")
# A pair holding a particle (养了, 的车) or opening with a pronoun (你叫) spans two words;
# 吉他 and 酒吧 are words.
_GLUE = frozenset("的了是吗呢啊呀哦嗯一")
_PRONOUNS = frozenset("我你他她它")
_WORDS = re.compile(r"[a-z0-9_]{2,}|[\u4e00-\u9fff]+")
_SUBJECT = re.compile(r"^\s*用户")
_WEEKDAYS = "一二三四五六日"


def tokenize(text):
    """Latin and digit words whole; Chinese runs as overlapping character pairs."""
    out = []
    for word in _WORDS.findall(str(text or "").lower()):
        if word.isascii():
            out.append(word)
        elif len(word) == 1:
            if word not in _STOP:
                out.append(word)
        else:
            out.extend(word[i:i + 2] for i in range(len(word) - 1))
    return out


def terms(text):
    """What BM25 matches on: the tokens plus each character of a Chinese run.

    Pairs alone never find a one-character word inside a longer run, like 茶 in
    用户喜欢茶 or 猫 in 用户养了一只猫. Glue pairs are left out: 养了 would tie
    保养了 to 养了一只猫.
    """
    out = [token for token in tokenize(text)
           if token.isascii() or not (_GLUE & set(token) or token[0] in _PRONOUNS)]
    for word in _WORDS.findall(str(text or "").lower()):
        if len(word) > 1 and not word.isascii():
            out.extend(char for char in word if char not in _STOP)
    return out


def similarity(a, b):
    """Jaccard overlap of the token sets: 0.6 and above reads as the same fact.

    Facts open with 用户; counting it would make 用户喜欢茶 and 用户喜欢猫 one fact.
    """
    left = set(tokenize(_SUBJECT.sub("", str(a or ""))))
    right = set(tokenize(_SUBJECT.sub("", str(b or ""))))
    if not left or not right:
        return 0.0
    shared = len(left & right)
    return shared / (len(left) + len(right) - shared)


def importance_of(row):
    value = row.get("importance")
    return 0.5 if value is None else min(1.0, max(0.0, float(value)))


def retention(row, now_ms):
    """R = e^(-days/tau); tau grows with importance and the days it was recalled on."""
    last = max(row.get("source_created_ms") or 0, row.get("last_hit_ms") or 0)
    days = max(0.0, (now_ms - last) / DAY_MS)
    strength = 1 + min(10, row.get("hit_days") or 0)
    tau = 20 * strength * (0.5 + importance_of(row))
    return math.exp(-days / tau)


def faded(row, now_ms):
    """An unimportant automatic fact nobody brought up for weeks; hidden, never deleted."""
    return (row.get("origin") == "auto" and importance_of(row) < FADE_IMPORTANCE
            and retention(row, now_ms) < FADE_RETENTION)


def weight(row, now_ms):
    """How much a fact deserves a place when nothing in the talk points at it."""
    return importance_of(row) * (0.55 + 0.45 * retention(row, now_ms))


class MemoryIndex:
    """BM25 over the rows it was built from; rebuild it when the rows change."""

    def __init__(self, rows):
        self.docs = []
        self.df = {}
        for row in rows:
            words = terms(row["text"])
            counts = {}
            for word in words:
                counts[word] = counts.get(word, 0) + 1
            for word in counts:
                self.df[word] = self.df.get(word, 0) + 1
            self.docs.append((row, counts, len(words) or 1))
        self.average = sum(doc[2] for doc in self.docs) / (len(self.docs) or 1)

    def search(self, query, now_ms, *, limit=6, min_score=MIN_SCORE, min_terms=1):
        words = set(terms(query))
        need = min(min_terms, len(words))
        total = len(self.docs)
        found = []
        for row, counts, length in self.docs:
            score, why = 0.0, []
            for word in words:
                freq = counts.get(word)
                if not freq:
                    continue
                seen = self.df.get(word, 0)
                idf = math.log(1 + (total - seen + 0.5) / (seen + 0.5))
                part = idf * freq * (K1 + 1) / (freq + K1 * (1 - B + B * length / self.average))
                score += part
                why.append((part, word))
            if score <= 0:
                continue
            score *= (0.6 + 0.8 * importance_of(row)) * (0.55 + 0.45 * retention(row, now_ms))
            if score >= min_score and len(why) >= need:
                shown = [item for item in why if len(item[1]) > 1] or why
                found.append({"row": row, "score": score,
                              "why": [word for _part, word in sorted(shown, reverse=True)[:3]]})
        found.sort(key=lambda hit: -hit["score"])
        return found[:limit]


def recall(index, query, now_ms, *, previous="", limit=HINT_LIMIT):
    hits = index.search(query, now_ms, limit=limit, min_terms=HINT_MIN_TERMS)
    if len(query.strip()) < SHORT_QUERY_CHARS and previous.strip() and len(hits) < limit:
        have = {hit["row"]["id"] for hit in hits}
        for hit in index.search(previous[-PREVIOUS_CHARS:], now_ms, limit=limit,
                                min_score=PREVIOUS_MIN_SCORE, min_terms=HINT_MIN_TERMS):
            if len(hits) >= limit:
                break
            if hit["row"]["id"] not in have:
                hits.append({**hit, "score": hit["score"] * PREVIOUS_WEIGHT,
                             "why": [*hit["why"], "（上一句）"]})
    return hits


def when_text(ms, now_ms):
    """今天 / 昨天 / 3天前 / 09-17 / 去年03-02 / 2024-05-01, in Beijing days."""
    if not ms:
        return ""
    then = datetime.fromtimestamp(ms / 1000, BEIJING)
    now = datetime.fromtimestamp(now_ms / 1000, BEIJING)
    days = (now.date() - then.date()).days
    if days <= 0:
        return "今天"
    if days == 1:
        return "昨天"
    if days < 7:
        return f"{days}天前"
    if then.year == now.year:
        return then.strftime("%m-%d")
    if then.year == now.year - 1:
        return "去年" + then.strftime("%m-%d")
    return then.strftime("%Y-%m-%d")


def day_text(ms):
    """2026-11-03 周二, the date a conversation happened on."""
    day = datetime.fromtimestamp(ms / 1000, BEIJING)
    return day.strftime("%Y-%m-%d") + " 周" + _WEEKDAYS[day.weekday()]


def memory_line(row, now_ms):
    return f"[{when_text(row.get('source_created_ms'), now_ms)}] {row['text']}"


def recall_hint(hits, now_ms):
    if not hits:
        return ""
    body = "；".join(memory_line(hit["row"], now_ms) for hit in hits)
    if len(body) > HINT_CHARS:
        body = body[:HINT_CHARS] + "…"
    return ("\n【你记得的相关旧事（方括号是什么时候的事）】" + body
            + "\n聊到相关的就自然用上，别把老事当新事说，也别说“根据记忆”。"
            "这些是以前听来的，不一定是眼前这位说的，对不上就以对方现在说的为准。")

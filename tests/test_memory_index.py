"""Local recall index, consolidation replies and the recap, without a model or a database."""

import json
import math

import pytest
from annie_brain.memory_consolidate import consolidate_request, parse_ops
from annie_brain.memory_index import (
    DAY_MS,
    MemoryIndex,
    faded,
    recall,
    recall_hint,
    retention,
    similarity,
    tokenize,
    weight,
    when_text,
)
from annie_brain.recap import RECAP_CHARS, parse_recap, recap_hint, recap_request

NOW = 1_793_700_000_000  # 2026-11-03 18:00 Beijing, a Tuesday


def fact(ident, text, **extra):
    return {"id": ident, "text": text, "importance": 0.5, "source_created_ms": NOW, "hits": 0,
            "hit_days": 0, "last_hit_ms": None, "origin": "auto", **extra}


def test_tokenizer_pairs_chinese_and_keeps_latin_words_whole():
    assert tokenize("我喜欢吃辣") == ["我喜", "喜欢", "欢吃", "吃辣"]
    assert tokenize("iPhone 15 很好") == ["iphone", "15", "很好"]
    assert tokenize("的 茶 a 1") == ["茶"]
    assert tokenize(None) == []


def test_similarity_is_token_overlap():
    assert similarity("用户喜欢吃辣", "用户喜欢吃辣") == 1.0
    assert similarity("用户喜欢吃辣", "用户喜欢吃辣的") == pytest.approx(3 / 4)
    assert similarity("用户喜欢吃辣", "用户在杭州上班") == 0.0
    assert similarity("用户喜欢茶", "用户喜欢猫") == pytest.approx(1 / 3)
    assert similarity("用户喜欢吃辣", "用户喜欢吃甜") == pytest.approx(0.5)
    assert similarity("", "用户") == 0.0


def test_bm25_finds_shared_words_and_explains_why():
    index = MemoryIndex([
        fact("a", "用户喜欢吃辣，最爱火锅"),
        fact("b", "用户下周三要去面试"),
        fact("c", "用户养了一只猫叫团子"),
    ])
    hits = index.search("我想吃火锅", NOW)
    assert [hit["row"]["id"] for hit in hits] == ["a"]
    assert hits[0]["why"] == ["火锅"]
    assert index.search("今天天气不错", NOW) == []


def test_one_character_words_are_found_inside_longer_runs():
    index = MemoryIndex([fact("tea", "用户喜欢茶"), fact("cat", "用户养了一只猫叫团子")])
    assert [(hit["row"]["id"], hit["why"]) for hit in index.search("茶", NOW)] == [("tea", ["茶"])]
    assert [hit["row"]["id"] for hit in index.search("我的猫最近不爱吃饭", NOW)] == ["cat"]


def test_hints_need_two_shared_terms_the_tool_needs_one():
    index = MemoryIndex([fact("movie", "用户最喜欢的电影是《星际穿越》"), fact("tea", "用户喜欢喝茶")])
    assert recall(index, "今天星期几", NOW) == []
    assert [hit["row"]["id"] for hit in index.search("今天星期几", NOW)] == ["movie"]
    assert [hit["row"]["id"] for hit in recall(index, "茶", NOW)] == ["tea"]
    assert [hit["row"]["id"] for hit in recall(index, "我想喝点茶", NOW)] == ["tea"]


def test_particle_pairs_do_not_tie_unrelated_sentences():
    index = MemoryIndex([fact("cat", "用户养了一只叫年糕的橘猫"), fact("guitar", "用户在学吉他")])
    assert recall(index, "我的车该保养了", NOW) == []
    assert [hit["row"]["id"] for hit in recall(index, "吉他弦又断了", NOW)] == ["guitar"]
    assert [hit["row"]["id"] for hit in recall(index, "年糕最近胖了", NOW)] == ["cat"]


def test_importance_and_forgetting_order_equal_matches():
    def order(*rows):
        return [hit["row"]["id"] for hit in MemoryIndex(rows).search("这个周末", NOW, min_score=0)]

    assert order(fact("minor", "用户周末在家", importance=0.2),
                 fact("major", "用户周末去爬山", importance=0.9)) == ["major", "minor"]
    assert order(fact("stale", "用户周末加班", source_created_ms=NOW - 400 * DAY_MS),
                 fact("fresh", "用户周末在家")) == ["fresh", "stale"]


def test_forgetting_curve_slows_with_importance_and_recall_days():
    assert retention(fact("x", ""), NOW) == 1.0
    assert retention(fact("x", ""), NOW + 20 * DAY_MS) == pytest.approx(math.exp(-1))
    assert retention(fact("x", "", hit_days=3), NOW + 20 * DAY_MS) == pytest.approx(math.exp(-0.25))
    assert retention(fact("x", "", importance=1.0), NOW + 30 * DAY_MS) == pytest.approx(math.exp(-1))
    recalled = fact("x", "", source_created_ms=NOW - 100 * DAY_MS, last_hit_ms=NOW)
    assert retention(recalled, NOW) == 1.0
    assert weight(fact("x", "", importance=0.8), NOW) == pytest.approx(0.8)

    old = NOW - 100 * DAY_MS
    assert faded(fact("x", "", importance=0.3, source_created_ms=old), NOW)
    assert not faded(fact("x", "", importance=0.3, source_created_ms=old, origin="manual"), NOW)
    assert not faded(fact("x", "", importance=0.6, source_created_ms=old), NOW)
    assert not faded(fact("x", "", importance=0.3), NOW)


def test_short_replies_borrow_the_previous_sentence():
    index = MemoryIndex([fact("b", "用户下周三要去面试")])
    assert index.search("嗯嗯", NOW) == []
    hits = recall(index, "嗯嗯", NOW, previous="我下周三要去面试，有点紧张")
    assert [hit["row"]["id"] for hit in hits] == ["b"]
    assert hits[0]["why"][-1] == "（上一句）"
    direct = index.search("我下周三要去面试，有点紧张", NOW)[0]["score"]
    assert hits[0]["score"] == pytest.approx(direct * 0.6)
    long_query = "今天天气真不错我们出去走走吧"
    assert recall(index, long_query, NOW, previous="我下周三要去面试") == []


def test_when_text_counts_beijing_days():
    assert when_text(NOW - 3_600_000, NOW) == "今天"
    assert when_text(NOW - DAY_MS, NOW) == "昨天"
    assert when_text(NOW - 3 * DAY_MS, NOW) == "3天前"
    assert when_text(NOW - 30 * DAY_MS, NOW) == "10-04"
    assert when_text(NOW - 365 * DAY_MS, NOW) == "去年11-03"
    assert when_text(None, NOW) == ""


def test_recall_hint_dates_each_fact_and_stays_short():
    assert recall_hint([], NOW) == ""
    hint = recall_hint([{"row": fact("a", "用户喜欢吃辣", source_created_ms=NOW - DAY_MS)}], NOW)
    assert hint.startswith("\n【你记得的相关旧事")
    assert "[昨天] 用户喜欢吃辣" in hint
    many = recall_hint([{"row": fact(str(n), "用户" + "很" * 120)} for n in range(4)], NOW)
    assert "…" in many and len(many) < 500


def test_consolidation_request_hides_storage_ids_behind_aliases():
    related = [fact("row-a", "用户喜欢吃辣"), fact("row-b", "用户在杭州上班")]
    system, user, aliases = consolidate_request([(NOW, "我现在不吃辣了", "好的")], related, NOW)
    payload = json.loads(user)
    assert aliases == {"m1": "row-a", "m2": "row-b"}
    assert payload["对话日期"] == "2026-11-03 周二"
    assert payload["已经记得的事"][0] == {"编号": "m1", "什么时候": "今天", "内容": "用户喜欢吃辣"}
    assert payload["对话"] == [{"时间": "18:00", "用户": "我现在不吃辣了", "小安妮": "好的"}]
    assert "row-a" not in user and "ops" in system


def test_parse_ops_normalizes_the_model_reply():
    aliases = {"m1": "row-a"}
    reply = json.dumps({"ops": [
        {"op": "outdated", "id": "m1", "kind": "semantic", "text": "用户不再吃辣", "importance": 0.7},
        {"op": "update", "id": "m9", "kind": "persona", "text": "用户爱听相声", "importance": 2},
        {"op": "delete", "id": "#m1", "kind": "weird", "text": " 用户养猫 ", "importance": "x"},
        {"op": "add", "text": "  "},
        "garbage",
    ]}, ensure_ascii=False)
    assert parse_ops("好的：\n```json\n" + reply + "\n```", aliases) == [
        {"op": "outdated", "kind": "semantic", "text": "用户不再吃辣", "importance": 0.7,
         "target": "row-a"},
        {"op": "add", "kind": "persona", "text": "用户爱听相声", "importance": 1.0, "target": None},
        {"op": "add", "kind": "semantic", "text": "用户养猫", "importance": 0.5, "target": None},
    ]
    assert parse_ops('{"ops": []}', aliases) == []
    with pytest.raises(ValueError):
        parse_ops("{}", aliases)
    with pytest.raises(ValueError):
        parse_ops("没什么可记的", aliases)


def test_recap_round_trip():
    system, user = recap_request("", [("我下周去面试", "加油！"), ("谢谢", "")])
    assert "120字" in system
    assert json.loads(user) == {"之前的前情": "", "滑出的对话": [
        {"用户": "我下周去面试", "小安妮": "加油！"}, {"用户": "谢谢", "小安妮": ""}]}
    assert parse_recap('{"recap": "  聊了  面试 "}') == "聊了 面试"
    assert len(parse_recap(json.dumps({"recap": "长" * 400}))) == RECAP_CHARS
    with pytest.raises(ValueError):
        parse_recap('{"summary": "x"}')
    assert recap_hint("") == ""
    assert recap_hint("聊了面试").startswith("\n【本次对话前情")

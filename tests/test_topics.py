import json
from datetime import datetime

from annie_brain.memory_index import BEIJING
from annie_brain.topics import TOPICS_MAX, is_bland, parse_topics, topics_hint, topics_request


def test_topics_request_carries_the_date_period_and_material():
    now_ms = int(datetime(2026, 11, 18, 15, 30, tzinfo=BEIJING).timestamp() * 1000)
    _, user = topics_request(now_ms, scene="桌上有一杯咖啡", episodes=["11月17日上午用户让她跳舞"],
                             used=["咖啡的冷知识"])
    payload = json.loads(user)
    assert payload["现在"] == "11月18日下午"
    assert payload["看到的画面"] == "桌上有一杯咖啡"
    assert payload["最近用过"] == ["咖啡的冷知识"]


def test_parse_topics_keeps_short_distinct_lines():
    reply = json.dumps({"topics": [" 咖啡豆其实是果核 ", "咖啡豆其实是果核", "", 3, "长" * 61,
                                   "立冬刚过", "你喜欢冬天吗", "暖气来了没", "多余的一条"]},
                       ensure_ascii=False)
    assert parse_topics(reply) == ["咖啡豆其实是果核", "立冬刚过", "你喜欢冬天吗", "暖气来了没"][:TOPICS_MAX]
    assert topics_hint([]) == ""
    assert "咖啡豆其实是果核" in topics_hint(["咖啡豆其实是果核"])


def test_bland_replies_give_nothing_to_build_on():
    for text in ("嗯", "嗯嗯。", "好的", "哈哈哈", "不知道", "OK"):
        assert is_bland(text), text
    for text in ("好的，那你跳个舞", "我不喜欢冬天", ""):
        assert not is_bland(text), text

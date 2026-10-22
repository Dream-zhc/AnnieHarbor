"""Stale "who is in front of me" exchanges are not replayed into a new Session.

Replays the 10/22 14:19 incident: the restored 43-hour-old history answered
"面前的人叫什么" with "康兴。" three times, and a typed "你好" with no face in
view was then greeted as “你好，康兴。”.
"""

from annie_brain.context import select_restorable_context

GAP_MS = 15 * 60_000
INCIDENT_MS = 1_792_649_976_812  # turn 621, typed “你好” → “你好，康兴。”
OLD_MS = INCIDENT_MS - 43 * 3_600_000


def turn(turn_id, user, reply, created_ms, *, input_mode="audio"):
    return {"turn_id": turn_id, "user_text": user, "assistant_generated_text": reply,
            "assistant_spoken_text": reply or None, "status": "completed",
            "interrupted": False, "cancelled": False, "turn_source": "user",
            "input_mode": input_mode, "created_ms": created_ms,
            "playback_evidence_status": "speaker_reported_drained" if reply else "unknown"}


INCIDENT_HISTORY = [
    turn("517", "小安妮站在你面前的这个人叫什么名？", "康兴。", OLD_MS),
    turn("518", "请记住我叫康兴", "康兴，你正对着我，离近一点，保持一会儿，我正在记你的脸。",
         OLD_MS + 30_000, input_mode="text"),
    turn("519", "小安妮，你面前的这个人叫什么名字啊？", "康兴。", OLD_MS + 60_000),
    turn("520", "小安妮站在你面前的这个人叫海超。", "张海超。", OLD_MS + 90_000),
    turn("521", "站在你面前的这个人叫什么名字？", "康兴。", OLD_MS + 100_000),
    turn("522", "小安妮，声音。", "", OLD_MS + 300_000),
    turn("523", "调到50。", "", OLD_MS + 302_000),
    turn("524", "小韩，你，你好，你好", "你好呀，不过我叫小安妮，不是小韩哦。", OLD_MS + 400_000),
]
NAMED_GREETING = turn("621", "你好", "你好，康兴。", INCIDENT_MS, input_mode="text")


def restore(rows, now_ms):
    return select_restorable_context(rows, speech_enabled=True, max_turns=8,
                                     now_ms=now_ms, stale_after_ms=GAP_MS)


def test_incident_history_no_longer_restores_any_stale_name():
    packet = restore(INCIDENT_HISTORY, INCIDENT_MS - 13_000)
    assert [item["turn_id"] for item in packet["turns"]] == ["522", "523", "524"]
    assert packet["omitted_stale_identity_turns"] == 5
    assert not any("康兴" in item["user_text"] + item["assistant_text"]
                   or "海超" in item["user_text"] + item["assistant_text"]
                   for item in packet["turns"])


def test_greeting_by_name_is_dropped_once_it_is_older_than_the_gap():
    rows = [*INCIDENT_HISTORY, NAMED_GREETING]
    fresh = restore(rows, INCIDENT_MS + 60_000)
    assert fresh["turns"][-1]["assistant_text"] == "你好，康兴。"
    later = restore(rows, INCIDENT_MS + GAP_MS + 60_000)
    assert [item["turn_id"] for item in later["turns"]] == ["522", "523", "524"]
    assert later["omitted_stale_identity_turns"] == 6


def test_fresh_identity_exchange_within_the_gap_is_kept():
    rows = [turn("1", "记住我叫康兴", "已经记住了，康兴。", INCIDENT_MS, input_mode="text"),
            turn("2", "你面前的人叫什么名字", "康兴。", INCIDENT_MS + 10_000)]
    packet = restore(rows, INCIDENT_MS + 120_000)
    assert [item["turn_id"] for item in packet["turns"]] == ["1", "2"]
    assert packet["omitted_stale_identity_turns"] == 0


def test_without_a_clock_nothing_is_filtered():
    packet = select_restorable_context(INCIDENT_HISTORY, speech_enabled=True, max_turns=8)
    assert len(packet["turns"]) == 8
    assert packet["omitted_stale_identity_turns"] == 0


def test_stale_identity_variants_are_dropped():
    rows = [turn(str(index), user, reply, OLD_MS + index) for index, (user, reply) in enumerate([
        ("我是谁", "你是康兴。"),
        ("你认识我吗", "认识，你是海超。"),
        ("他叫什么", "他叫张海超。"),
        ("我叫小明", "好的，小明。"),
        ("我的名字是小明", "记住了。"),
        ("嗨", "晚上好呀，海超！"),
        ("技术啊。", "我记住你了，张海超。"),  # real turn 511, refilled once 512–521 were dropped
        ("嗯", "你是康兴！"),
    ])]
    packet = restore(rows, INCIDENT_MS)
    assert packet["turns"] == []
    assert packet["omitted_stale_identity_turns"] == 8


def test_stale_ordinary_turns_are_still_restored():
    rows = [turn(str(index), user, reply, OLD_MS + index) for index, (user, reply) in enumerate([
        ("介绍一下张海超", "张海超是我们的工程师。"),
        ("你还记得我上次说的吗", "你说准备去看车。"),
        ("我叫你别说了", "好的，我不说了。"),
        ("你好", "你好，我在呢！"),
        ("今天天气怎么样", "晴天，二十度。"),
        ("小安妮，你是谁", "你是说我吗？我是小安妮。"),
        ("介绍一下自己。", "我是小安妮，一个陪你聊天解闷的机器人。"),
    ])]
    packet = restore(rows, INCIDENT_MS)
    assert len(packet["turns"]) == 7
    assert packet["omitted_stale_identity_turns"] == 0

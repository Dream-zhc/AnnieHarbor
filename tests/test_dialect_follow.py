"""Dialect follow by more words and by the Windows FireRedLID sidecar (11-20), behind a switch."""

import asyncio

from annie_runtime.language import ReplyLanguageState, dialect_marker

from tests.test_live_issues_1020 import live
from tests.test_wake_call_recovery import kinds


def state(on=True):
    language = ReplyLanguageState()
    language.dialect_follow_more = on
    return language


def test_more_dialect_words_only_with_the_switch():
    assert dialect_marker("这事咋整啊") is None
    assert dialect_marker("这事咋整啊", extra=True)[:2] == ("zh", "dongbei")
    assert dialect_marker("侬今朝吃过伐", extra=True)[:2] == ("zh", "shanghai")
    assert dialect_marker("巴适得很")[:2] == ("zh", "sichuan")  # the old four stay on
    off = state(on=False)
    off._hear_dialect("这事咋整啊", None)
    assert off.asr_dialect is None


def test_two_sentences_in_a_row_of_a_speakable_group_are_followed():
    language = state()
    assert not language.hear_lid("zh yue", .99)  # one sentence is not enough
    assert language.hear_lid("zh yue", .98)
    assert (language.asr_dialect["language"], language.asr_dialect["dialect"]) == ("yue", "cantonese")
    assert language.asr_dialect["source"] == "acoustic_lid"
    assert "听口音判断" in language.instruction_hint()
    assert not language.hear_lid("zh mandarin", 1.0)
    assert language.hear_lid("zh mandarin", 1.0) and language.asr_dialect is None  # back


def test_north_low_confidence_switch_off_words_and_requests_are_not_overridden():
    language = state()
    for _ in range(3):
        assert not language.hear_lid("zh north", .99)  # 东北? 陕西? 河南? not from sound alone
    assert not language.hear_lid("zh xinan", .5) and not language.hear_lid("zh xinan", .6)
    assert not state(on=False).hear_lid("zh xinan", .99)
    words = state()
    words._hear_dialect("这个嫽得很", None)  # 陕西 by her words
    words.hear_lid("zh xinan", .99)
    assert not words.hear_lid("zh xinan", .99) and words.asr_dialect["dialect"] == "shaanxi"
    asked = state()
    asked.explicit = {"language": "zh", "dialect": "mandarin", "scope": "chat"}
    asked.hear_lid("zh yue", .99)
    assert not asked.hear_lid("zh yue", .99)


def test_the_sidecar_result_for_this_room_reaches_the_robot(tmp_path):
    async def body(rt, client):
        message = {"type": "language_id", "room": "roomv2", "robot_id": "a", "label": "zh xinan",
                   "confidence": .995, "name": "西南官话"}
        rt._room_sam_event("a", {**message, "room": "roomv3"})  # robot B's Room: ignored
        rt._room_sam_event("a", message)
        rt._room_sam_event("a", message)
        assert client.language.asr_dialect["dialect"] == "sichuan"
        assert kinds(client, "language.lid_followed")[-1]["following"] == "zh/sichuan"

    live(tmp_path, body, dialect_follow_enabled=True)


def test_off_by_default_the_result_is_only_shown(tmp_path):
    async def body(rt, client):
        message = {"type": "language_id", "room": "roomv2", "label": "zh yue", "confidence": .99}
        rt._room_sam_event("a", message)
        rt._room_sam_event("a", message)
        await asyncio.sleep(0)
        assert client.language.asr_dialect is None and not kinds(client, "language.lid_followed")

    live(tmp_path, body)

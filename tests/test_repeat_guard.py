"""Sentence-level repeats are dropped before speech; the opening never waits."""

import asyncio

import pytest
from annie_runtime.repeat_guard import RepeatGuard, asked_again

from tests.test_phase7_runtime import ready, runtime

EARLIER = "你个瘪犊子玩意儿，脑瓜子让驴踢了吧！整天就知道瞎咧咧，赶紧滚犊子吧你。"
KEPT = "哎呀妈呀，你还真敢让我骂啊！瞅你那熊样，还敢点菜让我用东北话骂！"
# 11-05 16:16 in shape: new opening, then the tail of the reply before it, word for word.
REPLY = KEPT + "脑瓜子让驴踢了吧！整天就知道瞎咧咧，赶紧滚犊子吧你。"


def stream(guard, text):
    return "".join(guard.feed(char) for char in text) + guard.feed("", final=True)


@pytest.mark.parametrize("chunked", [True, False])
def test_the_repeated_tail_of_her_last_reply_is_dropped(chunked):
    guard = RepeatGuard([EARLIER])
    said = stream(guard, REPLY) if chunked else guard.feed(REPLY, final=True)
    assert said == KEPT
    assert len(guard.dropped) == 2


def test_the_opening_streams_unheld_and_is_never_dropped():
    guard = RepeatGuard([EARLIER])
    assert guard.feed("整天就知道") == "整天就知道"  # no wait for the sentence end
    rest = "瞎咧咧，赶紧滚犊子吧你。这回换个新鲜的骂法给你听听。"
    assert guard.feed(rest, final=True) == rest
    # A short first sentence is not the opening yet: the first full one still passes.
    assert stream(RepeatGuard([EARLIER]), "哟！整天就知道瞎咧咧，赶紧滚犊子吧你。") == \
        "哟！整天就知道瞎咧咧，赶紧滚犊子吧你。"


def test_short_sentences_new_words_and_no_history_pass():
    guard = RepeatGuard(["妈的，滚犊子吧你！"])
    assert stream(guard, "你听好了，我今天心情特别好。滚犊子吧你！") == \
        "你听好了，我今天心情特别好。滚犊子吧你！"
    assert stream(RepeatGuard(), REPLY) == REPLY


def test_a_sentence_said_earlier_in_the_same_reply_is_dropped():
    guard = RepeatGuard(["以前随口说过的一句话。"])
    said = stream(guard, "今天天气真不错啊朋友。我们出去走走吧好不好。今天天气真不错啊朋友。")
    assert said == "今天天气真不错啊朋友。我们出去走走吧好不好。"


def test_asking_the_same_thing_again_allows_the_same_answer():
    assert asked_again("番茄炒蛋怎么做来着", ["怎么做番茄炒蛋"])
    assert not asked_again("用东北话骂我两句", ["骂我"])
    assert not asked_again("", ["骂我"])


def test_live_reply_drops_her_repeated_tail_unless_asked_to_repeat(tmp_path):
    async def exercise():
        rt = runtime(tmp_path)
        await ready(rt)
        client = rt.realtime["a"]
        try:
            client._recent_replies = lambda limit=3: [EARLIER]
            client._create_turn("用东北话骂我两句")
            said = "".join(client._stage_filter(char) for char in REPLY) + client._stage_filter("", final=True)
            assert said == KEPT
            assert len([event for event in client.events
                        if event["kind"] == "reply.sentence_repeat_dropped"]) == 2
            client._finish("completed")
            client._create_turn("你再说一遍")
            said = "".join(client._stage_filter(char) for char in REPLY) + client._stage_filter("", final=True)
            assert said == REPLY
        finally:
            await rt.close()
    asyncio.run(exercise())

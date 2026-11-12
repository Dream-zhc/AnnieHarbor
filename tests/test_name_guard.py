"""11-04 evening: she named someone the vision service no longer saw, and told the same
Trump joke twice. Absent names stay out of her replies; a reply that says a recent line
again turns the next one to something new."""

import asyncio
from types import SimpleNamespace

from annie_brain.decision_policy import ABSENT_PERSON_QUESTION
from annie_runtime.name_guard import PAST_PERSON, STANDIN, NameGuard

from tests.test_face_registration import FakeRegistry, run_live


def guarded(names, verdict, *chunks):
    guard = NameGuard(names, lambda name: verdict)
    return "".join(guard.feed(chunk) for chunk in chunks) + guard.feed("", final=True), guard


def test_an_absent_name_loses_its_call_or_becomes_a_stand_in():
    assert guarded(["小明"], False, "小明，你好呀")[0] == "你好呀"
    assert guarded(["小明"], False, "小明你好")[0] == "你好"
    assert guarded(["小明"], False, "我刚看到", "小明出门了")[0] == f"我刚看到{STANDIN}出门了"
    text, guard = guarded(["小明"], False, "嗨，小", "明在吗")
    assert text == f"嗨，{STANDIN}在吗" and guard.withheld == ["小明"]
    assert guarded(["小明"], True, "小明，你好")[0] == "小明，你好"


def test_a_name_waits_for_its_verdict_and_is_withheld_without_one():
    guard = NameGuard(["小明"], lambda name: None)
    assert guard.feed("好的，小明，") == "好的，"
    assert guard.feed("下次见", final=True) == "下次见"
    assert NameGuard(["妈妈", "小明", "A"], lambda name: False).names == ["小明"]
    assert NameGuard().feed("小明，你好") == "小明，你好"


def test_asking_about_someone_who_left_is_told_apart_from_mentioning_them():
    for text in ("刚才那个人是谁", "之前来的那个朋友叫什么", "你还记得他吗", "他去哪了"):
        assert PAST_PERSON.search(text), text
    for text in ("他认识我就是", "你刚才说什么来着", "今天天气怎么样"):
        assert not PAST_PERSON.search(text), text


class FakeDecision:
    model = "fake-d3"

    def __init__(self, **choices):
        self.choices, self.asked = choices, []

    async def ask(self, state, questions):
        self.asked.append((state, questions))
        return {key: {"choice": self.choices.get(key), "confidence": .9} for key in questions}


def published(client):
    events, publish = [], client.publish

    def record(kind, **fields):
        events.append((kind, fields))
        return publish(kind, **fields)

    client.publish = record
    return events


async def reply(client, text, *chunks):
    """She answers a typed line in these chunks: what she ended up saying."""
    client._create_turn(text)
    turn, generation, response = client.turn_id, client.robot.generation, "r-" + client.turn_id
    await asyncio.sleep(.01)
    await client.handle({"type": "response.created", "response": {"id": response}}, generation)
    for chunk in chunks:
        await client.handle({"type": "response.text.delta", "response_id": response, "delta": chunk},
                            generation)
    await client.handle({"type": "response.text.done", "response_id": response,
                         "text": "".join(chunks)}, generation)
    await client.handle({"type": "response.done", "response": {"id": response, "status": "completed"}},
                        generation)
    await asyncio.sleep(.02)
    return client.robot.turn_cache[turn]["assistant_generated_text"]


def instructions(client):
    return client._combined_instructions(client._fresh_visual())


def test_she_does_not_call_someone_by_name_once_they_are_out_of_view(tmp_path):
    async def body(rt, client):
        events = published(client)
        client._refresh_known_people()
        await asyncio.sleep(.05)
        assert await reply(client, "他认识我就是", "小明，", "他刚才还说想你了。") == "他刚才还说想你了。"
        assert [fields["count"] for kind, fields in events if kind == "reply.name_withheld"] == [1]
        assert "【称呼】" in instructions(client)
        # Asked who that was, she may say it; the note was meant for that one reply.
        assert await reply(client, "刚才那个人是谁", "是小明呀，他刚走。") == "是小明呀，他刚走。"
        assert "【称呼】" not in instructions(client)

    run_live(tmp_path, FakeRegistry(["小明"]), body)


def test_the_decision_model_tells_whether_the_user_asks_about_someone_absent(tmp_path):
    async def body(rt, client):
        client._refresh_known_people()
        await asyncio.sleep(.05)
        client.decision = FakeDecision(absent="absent")
        assert await reply(client, "他俩谁先到的", "小明先到的。") == "小明先到的。"
        assert client.decision.asked[0][1] == {"absent": ABSENT_PERSON_QUESTION}
        client.decision.choices["absent"] = "present"
        assert await reply(client, "他俩谁先到的呀", "小明先到的。") == f"{STANDIN}先到的。"

    run_live(tmp_path, FakeRegistry(["小明"]), body)


def test_a_reply_that_says_a_recent_line_again_turns_the_next_one(tmp_path):
    weather = "今天外面风好大，记得多穿点衣服再出门哦。"

    async def body(rt, client):
        events = published(client)
        client.robot.context_cache.extend([
            {"turn_id": "t0", "user_text": "外面冷吗", "assistant_context_text": weather},
            {"turn_id": "t1", "user_text": "特朗普呢",
             "assistant_context_text": "特朗普那个发推狂人，关税今天加明天减，股市跟坐过山车一样。"}])
        # A held reply nearly word for word is withdrawn before it is heard.
        client.transcript = "外面冷吗"
        assert client._held_repeat([
            {"type": "response.text.delta", "delta": weather.replace("点", "件")},
            {"type": "response.done", "response": {"status": "completed"}}])
        client.transcript = ""
        await reply(client, "我要出门了", weather.replace("点", "件").replace("。", "！"))
        assert [fields["model"] for kind, fields in events if kind == "reply.repeated"] == ["rule"]
        assert "【别复读】" in instructions(client)
        # A looser likeness is the decision model's call.
        client.decision = FakeDecision(repeat="repeat")
        await reply(client, "特朗普最近咋样", "特朗普？天天发推，关税加了又减，股市被他玩成过山车了。")
        assert [fields["model"] for kind, fields in events if kind == "reply.repeated"] == ["rule", "fake-d3"]
        assert "新回复" in client.decision.asked[-1][0]
        # Unlike replies are not asked about, and the note is gone after the next one.
        asked = len(client.decision.asked)
        await reply(client, "说点别的", "好呀，那我们聊聊你家的猫。")
        assert len(client.decision.asked) == asked
        assert "【别复读】" not in instructions(client)
        # Asked to say it again, she may.
        await reply(client, "你再说一遍", "好呀，那我们聊聊你家的猫。")
        assert len([kind for kind, _fields in events if kind == "reply.repeated"]) == 2

    run_live(tmp_path, FakeRegistry(), body)


def test_the_voice_she_recognises_may_be_named_but_other_enrolled_voices_may_not(tmp_path):
    async def body(rt, client):
        voice = {"status": "matched", "name": "小明", "score": .8}
        book = SimpleNamespace(catalog=lambda: {"speakers": [
            {"name": "小明", "stale": False}, {"name": "小红", "stale": False}]})
        client.speakers = SimpleNamespace(public=lambda: voice, instruction_hint=lambda: "", service=book,
                                          reset=lambda: None)
        assert await reply(client, "听出来是我了吗", "小明，", "一听就是你。") == "小明，一听就是你。"
        assert await reply(client, "我们出去玩吧", "好呀，叫上小红一起？") == f"好呀，叫上{STANDIN}一起？"
        voice.update(status="unknown", name=None)
        assert await reply(client, "听出来了吗", "小明，", "一听就是你。") == "一听就是你。"

    run_live(tmp_path, FakeRegistry(), body)

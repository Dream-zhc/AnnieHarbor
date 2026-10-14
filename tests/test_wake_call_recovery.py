"""The robot's name is a call: shown as 小安妮, and a dropped call is still answered."""

import asyncio

import pytest
from annie_core import RobotConfig
from annie_runtime import HarborRuntime
from annie_runtime import realtime as realtime_module
from annie_runtime.presence import canonical_wake_call
from annie_runtime.wakeword import WakeAdmission

from tests.test_decision_model import EchoSocket


@pytest.mark.parametrize(("text", "kws", "expected", "raw"), [
    ("小爱你，今天天气怎么样", True, "小安妮，今天天气怎么样", "小爱你"),
    ("小爱你今天天气怎么样", True, "小安妮今天天气怎么样", "小爱你"),  # KWS heard the name
    ("小阿妮，讲个笑话", True, "小安妮，讲个笑话", "小阿妮"),
    ("小爱，你今天天气怎么样？", True, "小安妮，今天天气怎么样？", "小爱，你"),  # live ASR split
    ("小爱，今天天气怎么样", True, "小安妮，今天天气怎么样", "小爱"),
    ("小爱，你今天天气怎么样？", False, "小爱，你今天天气怎么样？", None),
    ("小安你好", True, "小安你好", None),  # may be 小安妮 + 你好
    ("小爱同学", True, "小爱同学", None),
    ("小安妮，你好", True, "小安妮，你好", None),
    ("小爱你，今天天气怎么样", False, "小安妮，今天天气怎么样", "小爱你"),
    ("小爱你今天天气怎么样", False, "小爱你今天天气怎么样", None),  # no pause, no KWS
    ("小爱，你吃了吗", False, "小爱，你吃了吗", None),
    ("我小爱你", False, "我小爱你", None),
])
def test_only_a_leading_call_is_rewritten(text, kws, expected, raw):
    assert canonical_wake_call(text, kws_confirmed=kws) == (expected, raw)


class Silent:
    async def detected(self, _pcm):
        return False


def runtime(tmp_path):
    return HarborRuntime([RobotConfig("a", room_id="roomv2")], tmp_path / "w.db",
                         realtime_key="fake", realtime_socket_factory=EchoSocket)


async def ready(rt):
    await rt.start()
    for _ in range(150):
        if rt.realtime["a"].ready:
            return rt.realtime["a"]
        await asyncio.sleep(.01)
    raise AssertionError("not ready")


@pytest.fixture(autouse=True)
def quick_settle(monkeypatch):
    monkeypatch.setattr(realtime_module, "RECOVERY_SETTLE_SECONDS", 0.05)


async def settled(client):
    """Wait out the quiet period a dropped call is held for."""
    for _ in range(100):
        if not client._decision_tasks:
            return
        await asyncio.sleep(.01)
    raise AssertionError("recovery never settled")


async def say(client, item, *, draft=None, final=None, invalid=False, start=True):
    g = client.robot.generation
    if start:
        await client.handle({"type": "input_audio_buffer.speech_started", "item_id": item}, g)
    if draft is not None:
        await client.handle({"type": "conversation.item.input_audio_transcription.delta",
                             "item_id": item, "text": "", "stash": draft}, g)
    await client.handle({"type": "input_audio_buffer.speech_stopped", "item_id": item,
                         **({"reason": "turn_invalid"} if invalid else {})}, g)
    if final is not None:
        await client.handle({"type": "conversation.item.input_audio_transcription.completed",
                             "item_id": item, "transcript": final}, g)


def woken(client):
    client.wake = WakeAdmission(Silent())
    client.wake.presence.wake_detected()  # local KWS just heard 小安妮
    return client.wake.presence


def sent(client, kind):
    return [e for e in client.socket.outbound if e["type"] == kind]


def kinds(client, kind):
    return [e for e in client.events if e["kind"] == kind]


def test_one_shot_wake_shows_and_stores_the_real_name(tmp_path):
    async def run():
        rt = runtime(tmp_path)
        client = await ready(rt)
        try:
            woken(client)
            g = client.robot.generation
            await client.handle({"type": "input_audio_buffer.speech_started", "item_id": "v1"}, g)
            await client.handle({"type": "conversation.item.input_audio_transcription.delta",
                                 "item_id": "v1", "text": "小爱", "stash": "你今天"}, g)
            preview = kinds(client, "transcript.delta")[-1]
            assert (preview["text"], preview["stash"]) == ("小安", "妮今天")
            await client.handle({"type": "input_audio_buffer.speech_stopped", "item_id": "v1"}, g)
            await client.handle({"type": "conversation.item.input_audio_transcription.completed",
                                 "item_id": "v1", "transcript": "小爱你今天天气怎么样"}, g)
            assert kinds(client, "transcript.done")[-1]["text"] == "小安妮今天天气怎么样"
            assert rt.robots["a"].turn_cache[client.turn_id]["user_text"] == "小安妮今天天气怎么样"
            normalized = kinds(client, "wakeword.call_normalized")[-1]
            assert normalized["raw"] == "小爱你" and normalized["kws_confirmed"] is True
        finally:
            await rt.close()
    asyncio.run(run())


def test_typed_text_is_never_rewritten(tmp_path):
    async def run():
        rt = runtime(tmp_path)
        client = await ready(rt)
        try:
            accepted = await rt.realtime_text("a", "小爱你，今天天气怎么样")
            assert rt.robots["a"].turn_cache[accepted["turn_id"]]["user_text"] == "小爱你，今天天气怎么样"
            assert not kinds(client, "wakeword.call_normalized")
        finally:
            await rt.close()
    asyncio.run(run())


def test_a_call_dropped_by_smart_turn_is_answered_from_its_final_asr(tmp_path):
    async def run():
        rt = runtime(tmp_path)
        client = await ready(rt)
        try:
            presence = woken(client)
            g = client.robot.generation
            await client.handle({"type": "input_audio_buffer.speech_started", "item_id": "w1"}, g)
            await client.handle({"type": "conversation.item.input_audio_transcription.delta",
                                 "item_id": "w1", "text": "", "stash": "小爱你，我们刚才"}, g)
            await client.handle({"type": "input_audio_buffer.speech_stopped", "item_id": "w1",
                                 "reason": "turn_invalid"}, g)
            assert presence.state == "WAKE_WAIT"  # kept open for the final ASR
            before = len(client.socket.outbound)
            await client.handle({"type": "conversation.item.input_audio_transcription.completed",
                                 "item_id": "w1", "transcript": "小爱你，我们刚才说周末要去干嘛？"}, g)
            assert not sent(client, "response.create")  # held for a quiet moment first
            await settled(client)
            new = client.socket.outbound[before:]
            assert [e["type"] for e in new][-2:] == ["conversation.item.create", "response.create"]
            assert new[-2]["item"]["content"][0]["text"] == "小安妮，我们刚才说周末要去干嘛？"
            assert presence.state == "ACTIVE"
            turn = client.turn_id
            assert kinds(client, "turn.recovered")[-1]["turn_id"] == turn
            assert client.snapshot()["invalid_recovered"] == 1
            for event in (
                {"type": "response.created", "response": {"id": "r1"}},
                {"type": "response.text.delta", "response_id": "r1", "delta": "你们刚才说去爬山。"},
                {"type": "response.done", "response": {"id": "r1", "status": "completed",
                                                       "output": []}},
            ):
                await client.handle(event, g)
            done = kinds(client, "turn.done")[-1]
            assert done["turn_id"] == turn and done["text"] == "你们刚才说去爬山。"
        finally:
            await rt.close()
    asyncio.run(run())


def test_without_wake_a_named_call_is_recovered_and_an_unnamed_one_is_not(tmp_path):
    async def run():
        rt = runtime(tmp_path)
        client = await ready(rt)
        try:
            g = client.robot.generation
            await client.handle({"type": "input_audio_buffer.speech_started", "item_id": "n1"}, g)
            await client.handle({"type": "input_audio_buffer.speech_stopped", "item_id": "n1",
                                 "reason": "turn_invalid"}, g)
            await client.handle({"type": "conversation.item.input_audio_transcription.completed",
                                 "item_id": "n1", "transcript": "我们去吃饭吧"}, g)
            assert not sent(client, "response.create") and not kinds(client, "turn.recovered")
            client._finish("completed")

            client._create_turn("讲个长故事")  # an older reply still running
            old = client.turn_id
            await client.handle({"type": "input_audio_buffer.speech_started", "item_id": "n2"}, g)
            await client.handle({"type": "conversation.item.input_audio_transcription.delta",
                                 "item_id": "n2", "text": "小安妮，香山", "stash": "远吗"}, g)
            await client.handle({"type": "input_audio_buffer.speech_stopped", "item_id": "n2",
                                 "reason": "turn_invalid"}, g)
            await client.handle({"type": "conversation.item.input_audio_transcription.completed",
                                 "item_id": "n2", "transcript": "小安妮，香山离这儿远吗？"}, g)
            await settled(client)
            assert rt.robots["a"].turn_cache[old]["status"] == "cancelled"
            assert client.turn_id != old
            assert sent(client, "response.create")
            assert kinds(client, "turn.recovered")
        finally:
            await rt.close()
    asyncio.run(run())


def test_dropped_items_are_never_deleted_and_a_dropped_goodbye_still_sleeps(tmp_path):
    async def run():
        rt = runtime(tmp_path)
        client = await ready(rt)
        try:
            presence = woken(client)
            g = client.robot.generation
            await client.handle({"type": "input_audio_buffer.speech_started", "item_id": "d1"}, g)
            await client.handle({"type": "input_audio_buffer.speech_stopped", "item_id": "d1",
                                 "reason": "turn_invalid"}, g)
            await client.handle({"type": "conversation.item.input_audio_transcription.completed",
                                 "item_id": "d1", "transcript": "小爱你。"}, g)
            # Qwen kept no item: a delete would come back as an error event.
            assert not [e for e in sent(client, "conversation.item.delete") if e["item_id"] == "d1"]
            assert not sent(client, "response.create")

            presence.state = "FOLLOWUP"
            presence.deadline = presence._window_deadline = presence.clock() + 8
            await client.handle({"type": "input_audio_buffer.speech_started", "item_id": "d2"}, g)
            await client.handle({"type": "conversation.item.input_audio_transcription.delta",
                                 "item_id": "d2", "text": "小安妮，退下", "stash": ""}, g)
            await client.handle({"type": "input_audio_buffer.speech_stopped", "item_id": "d2",
                                 "reason": "turn_invalid"}, g)
            await client.handle({"type": "conversation.item.input_audio_transcription.completed",
                                 "item_id": "d2", "transcript": "小安妮，退下。"}, g)
            assert presence.state == "SLEEP"
            assert kinds(client, "wakeword.sleep_command")
            assert not sent(client, "response.create")
        finally:
            await rt.close()
    asyncio.run(run())


def test_a_call_cut_at_a_pause_is_joined_with_what_follows(tmp_path):
    async def run():
        rt = runtime(tmp_path)
        client = await ready(rt)
        try:
            woken(client)
            # Live: smart_turn dropped "小安妮，我们" while the user was still talking.
            await say(client, "p1", draft="小安妮，我们", final="小安妮，我们。", invalid=True)
            await say(client, "p2", draft="刚才说周末", final="刚才说周末要去干嘛？", invalid=True)
            assert kinds(client, "turn.recovery_deferred")
            await settled(client)
            creates = [e for e in sent(client, "conversation.item.create")
                       if e["item"]["type"] == "message"]
            assert [e["item"]["content"][0]["text"] for e in creates] == [
                "小安妮，我们刚才说周末要去干嘛？"]
            assert len(sent(client, "response.create")) == 1
            assert client.wake.state == "ACTIVE"
        finally:
            await rt.close()
    asyncio.run(run())


def test_when_the_rest_is_answered_normally_nothing_is_resent(tmp_path):
    async def run():
        rt = runtime(tmp_path)
        client = await ready(rt)
        try:
            woken(client)
            await say(client, "q1", draft="小安妮，我们", final="小安妮，我们。", invalid=True)
            await say(client, "q2", draft="刚才说周末", final="刚才说周末要去干嘛？")
            await settled(client)
            assert not sent(client, "response.create")  # Qwen answers q2 itself
            assert not kinds(client, "turn.recovered")
            assert client._invalid_recovery is None
            assert kinds(client, "transcript.done")[-1]["text"] == "刚才说周末要去干嘛？"
        finally:
            await rt.close()
    asyncio.run(run())

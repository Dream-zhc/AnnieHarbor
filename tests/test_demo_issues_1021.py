"""Fixes from the 2026-10-21 customer demo (docs/运行日志/2026-10-21, 14:19-14:21).

Her own voice came back as user turns (“颐和园天坛” right after she said
“故宫、颐和园、天坛”), every visitor was greeted as “海超” from an old memory
while the camera showed nothing, and the big screen needs playback, ignored
speech and decisions.
"""

import asyncio
import time
from types import SimpleNamespace

from annie_brain.memory import is_meta_memory
from annie_core import RobotConfig
from annie_runtime import HarborRuntime
from annie_runtime.display import build_display_message
from annie_runtime.realtime import IDENTITY_RULES, visual_instructions

from tests.test_decision_model import EchoSocket


def live(tmp_path, body):
    async def run():
        rt = HarborRuntime([RobotConfig("a", room_id="roomv2")], tmp_path / "rt.db",
                           realtime_key="fake", realtime_socket_factory=EchoSocket)
        await rt.start()
        client = rt.realtime["a"]
        try:
            for _ in range(150):
                if client.ready:
                    break
                await asyncio.sleep(.01)
            await body(client)
        finally:
            client.speech = None
            await rt.close()
    asyncio.run(run())


class Playing:
    """A speaker that is playing (or just stopped) her reply."""

    def __init__(self, active=True):
        self.active = active
        self.turn_id = "t1"
        self.control_state = "held_confirmed"
        self._last_receipt_t = time.monotonic()
        self.controls = []

    def output_active(self):
        return self.active

    def request_playback_control(self, action, **kwargs):
        self.controls.append(action)


def test_her_own_words_heard_back_are_echo_not_a_new_turn(tmp_path):
    async def body(client):
        client._last_reply_text = "故宫、颐和园、天坛，这三个是必去的。"
        client._last_reply_t = time.monotonic()
        client.speech = Playing()
        assert client._is_self_echo("颐和园天坛。")
        # A real question mixed with her echo stays a question.
        assert not client._is_self_echo("北京有什么景点故宫颐和园")
        assert not client._is_self_echo("小安妮，停一下")  # a call by name
        assert not client._is_self_echo("那长城呢？")
        client._last_reply_text = "北京美食？烤鸭、炸酱面、豆汁儿，还有那铜锅涮肉。"
        assert client._is_self_echo("北京美食考一")  # ASR mishears 烤鸭
        client.speech = Playing(active=False)
        client.speech._last_receipt_t = time.monotonic() - 5
        assert not client._is_self_echo("北京美食考一")  # long after she stopped: a person
        client.speech = Playing()
        await client._drop_self_echo("item-9", "颐和园天坛")
        assert client.speech.controls == ["resume"] and client.echo_dropped == 1
        ignored = [e for e in client.events if e["kind"] == "transcript.ignored"]
        assert ignored[-1]["reason"] == "self_echo"

    live(tmp_path, body)


def test_without_a_camera_she_still_does_not_name_anyone():
    assert visual_instructions("base", None).endswith(IDENTITY_RULES)
    assert "不能用记忆或以前对话里的名字称呼对方" in IDENTITY_RULES
    assert IDENTITY_RULES in visual_instructions("base", "【眼前画面】你正看着一位朋友。")


def test_a_user_name_is_not_a_robot_wide_memory():
    assert is_meta_memory("user_name_zhanghaichao", "用户名叫海超。")
    assert is_meta_memory("k", "用户的名字是张三。")
    assert not is_meta_memory("pet_name", "用户的猫叫咪咪。")
    assert not is_meta_memory("travel", "用户叫车去机场。")


def test_the_big_screen_gets_playback_ignored_speech_and_decisions():
    def show(kind, **event):
        message = build_display_message(kind, {"ts_ms": 1, **event}, "annie-a", "roomv2")
        return message and message["data"]

    assert show("tts.playback.started", turn_id="t1", source="speaker") == {
        "sender": "brain", "robot_id": "annie-a", "room_id": "roomv2",
        "event": "playback.state", "turn_id": "t1", "state": "playing", "source": "speaker"}
    assert show("tts.playback.held", conversation_id="t1", source="speaker")["state"] == "held"
    assert show("tts.playback.drained", turn_id="t1", source="speaker")["state"] == "drained"
    echo = show("transcript.ignored", text="颐和园天坛", item_id="i1", reason="self_echo")
    assert (echo["event"], echo["reason_label"]) == ("speech.ignored", "机器人自己的回声")
    side = show("transcript.ambient", text="他没接视觉吧", reason="not_addressed")
    assert side["reason_label"] == "不是对机器人说的"
    assert show("ambient.heard", text="我们下午开会", source="asleep")["reason"] == "asleep"
    assert show("ambient.heard", text="x", source="not_addressed") is None  # sent once above
    assert show("transcript.dropped", text="嗯那个", reason="turn_invalid")["event"] == "speech.ignored"
    decided = show("addressee.decided", item_id="i2", verdict="not_robot", choice="other",
                   confidence=.85, judge="llm", model="qwen-flash", latency_ms=290,
                   text="这个速度变快了")
    assert decided["decision"] == "addressee" and decided["confidence"] == .85
    assert decided["model"] == "qwen-flash"
    assert decided["summary"] == "不是对机器人说的，不回复"
    face = show("action.auto_decided", turn_id="t1", choice="smirk", confidence=.48,
                script=None, source="decision_model", model="d3-NVFP4", latency_ms=686)
    assert face["decision"] == "expression" and face["summary"] == "不做表情"
    assert face["model"] == "d3-NVFP4"
    assert show("tts.playback.progress", turn_id="t1") is None  # no progress spam


def test_playback_states_keep_only_the_newest_on_the_queue():
    from annie_runtime.display import DisplayOutbox

    sent = []

    class Publisher:
        async def publish(self, message):
            sent.append(message["data"]["event"] + ":" + str(message["data"].get("state")))

    async def run():
        outbox = DisplayOutbox(Publisher(), "annie-a", "roomv2", lambda: {"enabled": False})
        for kind in ("tts.playback.started", "tts.playback.held", "tts.playback.resumed"):
            outbox.observe(kind, {"turn_id": "t1", "source": "speaker", "ts_ms": 1})
        await asyncio.sleep(.05)
        await outbox.close()

    asyncio.run(run())
    assert sent[-1] == "playback.state:playing" and len(sent) <= 2

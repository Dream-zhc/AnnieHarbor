"""学我说话 (user 11-20): only after consent, from the user's own speech, deleted right after."""

import asyncio
import io
import json
import wave

from annie_adapters import RoomServerConfig
from annie_adapters.realtime import session_update
from annie_core import RobotConfig
from annie_runtime import HarborRuntime

from tests.test_decision_model import EchoSocket
from tests.test_phase4b import FakeTts
from tests.test_vision_ack import reply, updates
from tests.test_wake_call_recovery import kinds, say, sent, woken

CLONE = "qwen-audio-3.1-tts-plus-mimic1-0123456789abcdef0123456789abcdef"


class Provider:
    def __init__(self):
        self.cloned, self.deleted = [], []

    async def clone(self, filename, audio, prefix, language):
        with wave.open(io.BytesIO(audio)) as clip:
            self.cloned.append((clip.getframerate(), clip.getnframes() / clip.getframerate()))
        return CLONE

    async def delete_remote(self, voice_id):
        self.deleted.append(voice_id)


class Publisher:
    def __init__(self, *_args, **_kwargs):
        self.packets = []

    async def publish(self, packet):
        self.packets.append(packet)

    async def close(self):
        pass


def test_the_tool_exists_only_when_switched_on():
    names = lambda **kw: [t["function"]["name"] for t in session_update("x", 8, **kw)["session"]["tools"]]
    assert "mimic_user_voice" in names(voice_mimic=True)
    assert "mimic_user_voice" not in names()


def test_learn_my_voice_needs_enough_speech_then_says_one_line_and_deletes_it(tmp_path):
    FakeTts.instances.clear()
    channels = {name: "reb_" + name for name in
                ("main", "tts", "mic", "visualdata", "vision", "sam", "voice")}
    root = "ws://127.0.0.1:1/robot"
    room = RoomServerConfig(base_url=root + "/roomv2", room_root=root, channels=channels,
                            default_urls={n: root + "/roomv2/" + v for n, v in channels.items()})

    def output(client):
        item = sent(client, "conversation.item.create")[-1]["item"]
        return json.loads(item["output"])

    async def tool(client, call_id):
        await client._tool_call({"type": "response.function_call_arguments.done", "call_id": call_id,
                                 "name": "mimic_user_voice",
                                 "arguments": json.dumps({"text": "你好呀我是你"})})
        return output(client)

    async def run():
        rt = HarborRuntime([RobotConfig("a", room_id="roomv2")], tmp_path / "rt.db", room,
                           realtime_key="fake", realtime_socket_factory=EchoSocket,
                           tts_voice="fake-voice", tts_socket_factory=FakeTts,
                           room_tts_factory=Publisher, voice_mimic_enabled=True)
        await rt.start()
        client = rt.realtime["a"]
        try:
            for _ in range(150):
                if client.ready:
                    break
                await asyncio.sleep(.01)
            assert client._mimic_available()
            assert any("【学用户说话】" in text for text in updates(client))
            client.voices.provider = provider = Provider()
            woken(client)
            await say(client, "u1", final="可以，你学吧")
            for _ in range(100):  # 2 s of her voice so far: not enough
                client._keep_user_voice(bytes(640))
            assert (await tool(client, "c1"))["status"] == "need_more_audio"
            for _ in range(300):
                client._keep_user_voice(bytes(640))
            assert (await tool(client, "c2"))["status"] == "started"
            await reply(client, "r1", "让我琢磨琢磨你的嗓音。")
            if client.turn_id is not None:  # the tool's follow-up response of the same turn
                await reply(client, "r1b", "稍等哦。")
            for _ in range(300):
                if kinds(client, "voice_mimic.spoken"):
                    break
                await asyncio.sleep(.02)
            assert kinds(client, "voice_mimic.spoken")[-1]["cloned"] is True
            await reply(client, "r2", "你好呀我是你")
            await asyncio.wait_for(client._mimic_task, 10)
            assert provider.cloned == [(16000, 8.0)] and provider.deleted == [CLONE]
            voices = [m["payload"]["parameters"]["voice"] for tts in FakeTts.instances
                      for m in tts.sent if m["header"]["action"] == "run-task"]
            assert CLONE in voices and voices[-1] == CLONE and voices[0] != CLONE
            woken(client)
            await say(client, "u2", final="再学一次")
            assert (await tool(client, "c3"))["status"] == "cooldown"
        finally:
            await rt.close()

    asyncio.run(run())


def test_her_own_voice_and_another_chat_never_mix_into_the_sample(tmp_path):
    from tests.test_live_issues_1020 import live

    async def body(rt, client):
        client.voice_mimic_enabled = True
        for _ in range(2000):  # 40 s
            client._keep_user_voice(bytes(640))
        assert client._user_voice_bytes == 30 * 32000  # keeps the last 30 s
        client.robot.chat_id = "another-chat"
        client._keep_user_voice(bytes(640))
        assert client._user_voice_bytes == 640

    live(tmp_path, body)

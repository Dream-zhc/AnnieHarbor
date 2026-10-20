"""Voice volume control of the board speaker, and the cleanups from the 10/16 and 10/20 chats."""

import array
import asyncio
import re

import pytest
from annie_brain.persona import PREFERENCE_REQUEST
from annie_runtime.audio_devices import RoomAudioDevices
from annie_runtime.idle import SpokenTopics
from annie_runtime.speaker_volume import VOLUME_MIN, volume_target
from annie_runtime.speech_expression import STAGE_HOLD_LIMIT, StageDirections

from tests.test_room_microphone import FakeRoom, speaker


def test_board_volume_scales_samples_and_is_kept_across_restarts(tmp_path):
    playback = speaker.Playback(24000, prebuffer_ms=0)
    playback.set_volume(50)
    playback.push_audio("c", 1, 1, 0, array.array("h", [10000] * 480), stream_final=True)
    samples, _ = playback.pull(480)
    assert max(samples) == 2500  # 50 -> a quarter of the amplitude
    with pytest.raises(ValueError):
        playback.set_volume(101)
    message = {"type": "speaker.volume.set",
               "data": {"sender": "brain", "volume": 30, "request_id": "r1"}}
    assert speaker.parse_volume(message) == (30, "r1")
    assert speaker.parse_volume({"type": "speaker.volume.set",
                                 "data": {"sender": "speaker", "volume": 30}}) is None
    with pytest.raises(ValueError):
        speaker.parse_volume({"type": "speaker.volume.set",
                              "data": {"sender": "brain", "volume": "loud", "request_id": "r"}})
    store = speaker.VolumeStore(str(tmp_path / ".speaker-volume"))
    assert store.load(100) == 100
    store.save(40)
    assert store.load(100) == 40
    assert speaker.parse_args([]).volume == 100


def test_volume_steps_stay_audible():
    assert volume_target(100, change="down") == 80
    assert volume_target(20, change="down") == VOLUME_MIN
    assert volume_target(90, change="up") == 100
    assert volume_target(60, level=0) == VOLUME_MIN
    with pytest.raises(ValueError):
        volume_target(60, change="up", level=50)
    devices = RoomAudioDevices()
    devices.report({"type": "speaker.presence", "data": {
        "sender": "browser_test", "instance_id": "browser-speaker-1", "volume": 70}})
    assert devices.speaker_volume() is None  # the web audition is not the robot's speaker
    devices.report({"type": "speaker.presence", "data": {
        "sender": "speaker", "instance_id": "speaker-1", "volume": 70}})
    assert devices.speaker_volume() == 70
    devices.report({"type": "speaker.volume", "data": {
        "sender": "speaker", "instance_id": "speaker-1", "volume": 50, "request_id": "r9"}})
    assert devices.speaker_volume() == 50 and devices.volume_acks["r9"] == 50


def test_saying_louder_or_quieter_turns_the_board_speaker_end_to_end(tmp_path):
    from annie_adapters import RoomServerConfig
    from annie_core import RobotConfig
    from annie_runtime import HarborRuntime
    from websockets.asyncio.server import serve

    from tests.test_decision_model import EchoSocket

    async def run():
        room = FakeRoom()
        server = await serve(room.handler, "127.0.0.1", 0)
        root = f"ws://127.0.0.1:{server.sockets[0].getsockname()[1]}/robot"
        channels = {name: "reb_" + name for name in
                    ("main", "tts", "mic", "visualdata", "vision", "sam", "voice")}
        config = RoomServerConfig(base_url=root + "/roomv2", room_root=root, channels=channels,
                                  default_urls={n: f"{root}/roomv2/{c}" for n, c in channels.items()},
                                  reconnect_min_sec=.01, reconnect_max_sec=.02,
                                  connect_timeout_sec=.5)
        rt = HarborRuntime([RobotConfig("a", room_id="roomv2", listen_enabled=True)],
                           tmp_path / "rt.db", config, realtime_key="fake",
                           realtime_socket_factory=EchoSocket)
        await rt.start()
        client = rt.realtime["a"]
        playback = speaker.Playback(48000)
        store = speaker.VolumeStore(str(tmp_path / ".speaker-volume"))
        identity = speaker.mic_module().device_identity("speaker", "板载喇叭")
        import queue
        board = asyncio.create_task(speaker.serve(
            f"{root}/roomv2/reb_tts", f"{root}/roomv2/reb_voice", "roomv2", playback,
            queue.Queue(), speaker_identity=identity, volume_store=store))
        try:
            async with asyncio.timeout(4):
                while client.speaker_volume.current() is None:
                    await asyncio.sleep(.02)
            assert client.speaker_volume.current() == 100

            async def say(text, **args):
                if client.turn_id:
                    client._finish("completed")
                client._create_turn(text)
                return await client._set_speaker_volume(args)

            quieter = await say("你小声点", change="down")
            assert quieter == {"status": "set", "volume": 80, "previous": 100, "via": "speaker"}
            assert playback.volume == 80 and store.load(100) == 80
            half = await say("音量调到一半", level=50)
            assert half["volume"] == 50 and playback.volume == 50
            assert (await say("声音调到最大", level=100))["volume"] == 100
            assert (await say("再大声点", change="up"))["status"] == "at_limit"
            with pytest.raises(ValueError):
                await say("这个音箱是什么牌子", change="down")  # not a request
            changed = [e for e in client.events if e["kind"] == "speaker.volume.changed"]
            assert [(e["previous"], e["volume"]) for e in changed] == [(100, 80), (80, 50), (50, 100)]
        finally:
            board.cancel()
            await asyncio.gather(board, return_exceptions=True)
            await rt.close()
            server.close()
            await server.wait_closed()

    asyncio.run(run())


def test_without_a_board_speaker_volume_falls_back_to_the_voice_style(tmp_path):
    from annie_core import RobotConfig
    from annie_runtime import HarborRuntime

    from tests.test_decision_model import EchoSocket

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
            client._create_turn("你说话小声点")
            assert (await client._set_speaker_volume({"change": "down"})) == {
                "status": "set", "via": "voice_style", "value": "quieter"}
            assert (await client._set_speaker_volume({"change": "down"}))["status"] == "at_limit"
        finally:
            await rt.close()

    asyncio.run(run())


def test_bracketed_stage_directions_are_never_spoken():
    def clean(*parts):
        stage = StageDirections()
        return "".join(stage.feed(part) for part in parts) + stage.finish(), stage.dropped

    assert clean("【眨", "眼】") == ("", ["眨眼"])
    assert clean("好嘞【眨眼", "】～") == ("好嘞～", ["眨眼"])
    assert clean("眨眼】") == ("眨眼", [""])  # a stray bracket goes, the words stay
    long = "【" + "这是一段普通的话" * 4
    assert clean(long)[0] == long[1:] and len(long) > STAGE_HOLD_LIMIT
    assert clean("没关闭【的半句")[0] == "没关闭的半句"


def test_a_topic_word_alone_does_not_change_the_speech_rate():
    rate = PREFERENCE_REQUEST["speech_rate"]
    for said in ("说话的语速。", "语速", "这个速度变快了"):
        assert not re.search(rate, said), said
    for said in ("说慢一点", "语速快一点", "你说话太快了", "恢复正常语速", "放慢点说"):
        assert re.search(rate, said), said


def test_proactive_lines_are_not_repeated_after_the_user_talks():
    clock = [0.0]
    spoken = SpokenTopics(["a"], clock=lambda: clock[0])
    spoken.add("a", "今天天气挺舒服，适合出去走走")
    assert spoken.repeated("a", "今天天气感觉挺舒服的，适合出来走走")
    assert not spoken.repeated("a", "桌上那杯咖啡还冒着热气")
    assert spoken.latest("a").startswith("今天天气")
    clock[0] += 3 * 3600
    assert not spoken.repeated("a", "今天天气感觉挺舒服的，适合出来走走")


def test_what_the_user_calls_annie_is_not_remembered_as_their_name():
    from annie_brain.memory import is_meta_memory, prepare_extracted_fact

    # From the 10/14 chat: "那行，小bug，我把这勾勾改一改就行了。"
    for text in ("用户称呼当前AI角色为“小bug”。", "用户称呼当前AI角色为“小安妮”。",
                 "用户叫助手小bug。", "用户管机器人叫小bug。"):
        assert is_meta_memory("user_call_ai_xiaobug", text), text
        assert prepare_extracted_fact({"kind": "persona", "key": "user_call_ai",
                                       "text": text}, "那行，小bug，我改一改") is None
    assert not is_meta_memory("user_name", "用户叫张三。")


def test_change_your_personality_is_an_explicit_request():
    from pathlib import Path

    from annie_brain.persona import load_personas, resolve_persona_style_request

    profiles = load_personas(Path(__file__).resolve().parents[1] / "config/personas.toml")
    social = {"persona_style": {"value": "annie-weary", "source": "user_setting"}}
    for said in ("小安妮，换一个你的性格。", "换个性格", "给你换换风格"):
        style, options = resolve_persona_style_request("annie", profiles, social, "", said)
        assert style is None and "活泼俏皮" in options, said
    style, _ = resolve_persona_style_request("annie", profiles, social, "活泼俏皮",
                                             "小安妮，换一个你的性格。")
    assert style == "annie-lively"
    with pytest.raises(ValueError):
        resolve_persona_style_request("annie", profiles, social, "活泼俏皮", "你的性格挺好")


def test_systemctl_stop_sends_the_goodbye_like_ctrl_c():
    import os
    import signal

    said = []

    async def board():
        try:
            await asyncio.sleep(30)
        finally:
            said.append("goodbye")  # serve() sends its stop reports here

    async def run():
        task = asyncio.create_task(speaker.until_stopped(board()))
        await asyncio.sleep(.05)  # the SIGTERM handler is in place
        os.kill(os.getpid(), signal.SIGTERM)
        return await asyncio.wait_for(task, 2)

    assert asyncio.run(run()) is None and said == ["goodbye"]

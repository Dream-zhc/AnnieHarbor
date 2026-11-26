"""Laughs and sighs as real TTS sounds (user 11-20: “都挺自然的，尽量多用”)."""

import asyncio

from annie_runtime import realtime as realtime_module
from annie_runtime.speech_expression import (
    EMOTION_TAGS,
    SOUND_TAGS,
    SoundMarks,
    sounds_as_tags,
    without_sounds,
)

from tests.test_companion_speech import speech
from tests.test_phase4b import FakeTts


def marked(*parts, limit=3):
    marks = SoundMarks(limit=limit)
    return "".join(marks.feed(part) for part in parts) + marks.finish(), marks.used


def test_documented_sound_tags_become_marks_even_split_across_deltas():
    text, used = marked("[laugh", "ing]你这问题太逗了，[Giggles]我差点没忍住")
    assert used == ["laughing", "giggles"]
    assert without_sounds(text) == "你这问题太逗了，我差点没忍住"
    assert sounds_as_tags(text) == "[laughing]你这问题太逗了，[giggles]我差点没忍住"
    assert set(SOUND_TAGS) == {"laughing", "giggles", "snorts", "sighing", "gasp",
                               "clears throat", "cough"}


def test_theatrical_tags_are_dropped_other_brackets_stay_and_caps_hold():
    text, used = marked("[like dracula]好呀[不是标签]，[sighing][gasp][snorts][laughing]走吧[sneezes]")
    assert used == ["sighing", "gasp", "snorts"]  # the fourth sound is over the limit
    assert without_sounds(text) == "好呀[不是标签]，走吧[sneezes]"  # undocumented: plain text
    text, used = marked("[curious]你拿的啥？[excited]快给我看！[sad]不给就算了。")
    assert used == ["curious", "excited"]  # at most two emotion tags a reply
    assert sounds_as_tags(text) == "[curious]你拿的啥？[excited]快给我看！不给就算了。"
    assert marked("[clears", " throat]嗯", "[")[0] == SoundMarks().feed("[clears throat]") + "嗯["


def test_the_tts_hears_the_tag_but_the_aligner_and_history_only_the_words():
    async def run():
        FakeTts.instances.clear()
        output = speech(first_chars=2, min_chars=4, max_chars=30, first_wait_ms=5)
        try:
            identity = output.robot.session_state()
            text, _ = marked("[laughing]你这问题太逗了。[sighing]好吧。")
            await output.feed("turn", identity, text)
            await output.finish("turn")
            await asyncio.wait_for(output.worker, 2)
            sent = [m["payload"]["input"]["text"] for tts in FakeTts.instances for m in tts.sent
                    if m["header"]["action"] == "continue-task"]
            assert "".join(sent) == "[laughing]你这问题太逗了。[sighing]好吧。"
            assert output.submitted_text == output.generated_text == "你这问题太逗了。好吧。"
        finally:
            await output.close()
    asyncio.run(run())


def test_the_prompt_lists_only_the_sounds_the_runtime_passes_through():
    for tag in ("laughing", "giggles", "snorts", "sighing", "gasp", "clears throat"):
        assert f"[{tag}]" in realtime_module.SOUNDS_HINT
    assert "[cough]" not in realtime_module.SOUNDS_HINT
    for tag in EMOTION_TAGS:
        assert f"[{tag}]" in realtime_module.SOUNDS_HINT
    for tag in ("like dracula", "shouting", "crying", "trembling", "panicked", "asmr"):
        assert f"[{tag}]" not in realtime_module.SOUNDS_HINT


def test_serious_safety_valve_hard_drops_laughs_and_playful_emotions():
    marks = SoundMarks(serious=True)
    out = marks.feed(
        "[laughing][giggles][snorts][sarcastic][mischievously][scornful]"
        "[empathetic]别太难过，[serious]我陪着你。[sad]慢慢说。"
    ) + marks.finish()
    assert marks.used == ["empathetic", "serious"]  # cap=2, laughs/sarcasm hard-dropped
    assert without_sounds(out) == "别太难过，我陪着你。慢慢说。"
    assert sounds_as_tags(out) == "[empathetic]别太难过，[serious]我陪着你。慢慢说。"

    # Switching mid-turn via set_serious immediately blocks subsequent laughs.
    live_marks = SoundMarks()
    first = live_marks.feed("[laughing]前一句。")
    live_marks.set_serious(True)
    second = live_marks.feed("[giggles][sarcastic][empathetic]后一句。") + live_marks.finish()
    assert sounds_as_tags(first) == "[laughing]前一句。"
    assert sounds_as_tags(second) == "[empathetic]后一句。"


def test_a_laugh_in_her_reply_reaches_the_tts_but_never_the_reply_text(tmp_path):
    from annie_adapters import RoomServerConfig
    from annie_core import RobotConfig
    from annie_runtime import HarborRuntime

    from tests.test_decision_model import EchoSocket
    from tests.test_vision_ack import reply, updates
    from tests.test_wake_call_recovery import kinds, say, woken

    class Publisher:
        def __init__(self, *_args, **_kwargs):
            self.packets = []

        async def publish(self, packet):
            self.packets.append(packet)

        async def close(self):
            pass

    FakeTts.instances.clear()
    channels = {name: "reb_" + name for name in
                ("main", "tts", "mic", "visualdata", "vision", "sam", "voice")}
    root = "ws://127.0.0.1:1/robot"
    room = RoomServerConfig(base_url=root + "/roomv2", room_root=root, channels=channels,
                            default_urls={name: root + "/roomv2/" + value
                                          for name, value in channels.items()})

    async def run():
        rt = HarborRuntime([RobotConfig("a", room_id="roomv2")], tmp_path / "rt.db", room,
                           realtime_key="fake", realtime_socket_factory=EchoSocket,
                           tts_voice="fake-voice", tts_socket_factory=FakeTts,
                           room_tts_factory=Publisher, tts_sounds_enabled=True)
        await rt.start()
        client = rt.realtime["a"]
        try:
            for _ in range(150):
                if client.ready:
                    break
                await asyncio.sleep(.01)
            assert client.sounds_enabled and client.speech is not None
            assert any(realtime_module.SOUNDS_HINT in text for text in updates(client))
            woken(client)
            await say(client, "u1", final="给我讲个笑话")
            await reply(client, "r1", "[laughing]好嘞，[shouting][giggles]听好了。")
            done = kinds(client, "turn.done")[-1]
            assert done["text"] == "好嘞，听好了。" and done["sounds"] == ["laughing", "giggles"]
            texts = ""
            for _ in range(200):
                texts = "".join(m["payload"]["input"]["text"] for tts in FakeTts.instances
                                for m in tts.sent if m["header"]["action"] == "continue-task")
                if "听好了" in texts:
                    break
                await asyncio.sleep(.01)
            assert texts == "[laughing]好嘞，[giggles]听好了。"
            assert client.speech.generated_text == "好嘞，听好了。"
        finally:
            await rt.close()

    asyncio.run(run())

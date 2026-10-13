"""Turn-frozen speech style and raw duplex TTS parameter contracts."""

import asyncio
from dataclasses import FrozenInstanceError
from functools import wraps

import pytest
from annie_adapters.tts import command
from annie_core.speech_style import SpeechStyle, choose_speech_style
from annie_runtime.speech import SpeechOutput

from tests.test_phase4b import FakePublisher, FakeRobot, FakeTts


def async_test(function):
    @wraps(function)
    def run():
        return asyncio.run(function())
    return run


def test_style_defaults_are_immutable_and_neutral():
    style = SpeechStyle()
    assert (style.language, style.dialect, style.emotion) == ("zh", "unknown", "neutral")
    assert (style.rate, style.pitch, style.volume, style.source) == (1., 1., 50, "runtime_default")
    with pytest.raises(FrozenInstanceError):
        style.rate = 1.1


@pytest.mark.parametrize("kwargs", [
    {"language": "arbitrary"}, {"language": True}, {"dialect": []},
    {"emotion": "angry"}, {"rate": True}, {"rate": "1.0"},
    {"rate": float("nan")}, {"pitch": float("inf")}, {"pitch": .5},
    {"rate": 1.13}, {"rate": 10 ** 1000}, {"rate": .89}, {"volume": True}, {"volume": 50.5},
    {"volume": 61}, {"instruction": []}, {"instruction": "a" * 513},
    {"source": ""}, {"source": {}},
])
def test_style_rejects_invalid_types_and_nonconservative_values(kwargs):
    with pytest.raises(ValueError):
        SpeechStyle(**kwargs)


@pytest.mark.parametrize("language", ["zh", "yue", "en", "ja", "ko", "fr", "de", "es", "ar"])
def test_language_whitelist(language):
    assert choose_speech_style(language=language).language == language


@pytest.mark.parametrize("dialect,label", [
    ("mandarin", "普通话"), ("sichuan", "四川话"), ("henan", "河南话"),
    ("shaanxi", "陕西话"), ("cantonese", "粤语"),
])
def test_dialect_instruction_is_fixed_and_not_spoken_text(dialect, label):
    style = choose_speech_style(dialect=dialect, emotion="playful")
    assert label in style.instruction
    params = command("run-task", "task", voice="keep", style=style)["payload"]["parameters"]
    assert params["instruction"] == style.instruction
    assert params["voice"] == "keep" and params["sample_rate"] == 24000
    assert params["rate"] == style.rate and params["pitch"] == style.pitch
    assert "speech_rate" not in params and "pitch_rate" not in params
    assert command("continue-task", "task", text="你好")["payload"] == {"input": {"text": "你好"}}


@pytest.mark.parametrize("input_emotion", ["angry", "sad", "fearful"])
def test_distress_is_gently_received_not_mirrored(input_emotion):
    style = choose_speech_style(emotion="sarcastic", input_emotion=input_emotion)
    assert style.emotion in {"warm", "calm"}
    assert "嘲讽" not in style.instruction and "暴怒" not in style.instruction
    assert style.rate >= .9 and style.pitch >= .95 and style.volume == 50


def test_serious_context_suppresses_sarcasm():
    assert choose_speech_style(emotion="sarcastic", serious=True).emotion == "serious"


def test_unknown_intent_falls_back_without_guessed_expression():
    assert choose_speech_style(emotion="unverified").emotion == "neutral"


def test_default_wire_remains_byte_equivalent_parameters():
    assert command("run-task", "task", voice="keep")["payload"]["parameters"] == {
        "text_type": "PlainText", "voice": "keep", "format": "pcm",
        "sample_rate": 24000, "enable_ssml": False,
    }


@pytest.mark.parametrize("kwargs", [
    {"rate": True}, {"rate": float("nan")}, {"pitch": float("-inf")},
    {"rate": 2.1}, {"pitch": .49}, {"volume": 101}, {"volume": 50.5},
    {"instruction": 42}, {"style": {}}, {"style": SpeechStyle(), "rate": 1.1},
])
def test_adapter_validates_only_known_parameters(kwargs):
    with pytest.raises(ValueError):
        command("run-task", "task", **kwargs)


def test_adapter_raw_official_bounds_and_non_run_rejection():
    params = command("run-task", "task", instruction="自然", rate=.5, pitch=2., volume=100)["payload"]["parameters"]
    assert (params["rate"], params["pitch"], params["volume"]) == (.5, 2., 100)
    with pytest.raises(ValueError):
        command("continue-task", "task", text="你好", rate=1.1)


@async_test
async def test_style_freezes_once_before_run_task_and_does_not_leak_after_cancel():
    robot, publisher, events, calls = FakeRobot(), FakePublisher(), [], []
    current = [choose_speech_style(dialect="henan", emotion="playful")]

    def provider(turn_id, identity):
        calls.append((turn_id, dict(identity)))
        identity["generation"] = -1  # callback must not mutate output identity
        return current[0]

    output = SpeechOutput(robot, publisher, url="wss://example.test/tts", key="fake",
                          voice="keep", socket_factory=FakeTts, first_chars=2,
                          style_provider=provider, event=lambda kind, **data: events.append((kind, data)))
    try:
        identity = robot.session_state()
        await output.feed("one", identity, "你好")
        for _ in range(100):
            if any(kind == "tts.run_task" for kind, _ in events):
                break
            await asyncio.sleep(.001)
        current[0] = choose_speech_style(dialect="sichuan", emotion="serious")
        await output.feed("one", identity, "今天好吗。")
        await output.finish("one")
        await asyncio.wait_for(output.worker, 1)
        assert output.error is None
        assert [turn for turn, _ in calls] == ["one"]
        assert output.snapshot()["speech_style"]["dialect"] == "henan"
        assert output.snapshot()["run_to_first_pcm_ms"] >= 0
        kinds = [kind for kind, _ in events]
        assert kinds.index("tts.style.locked") < kinds.index("tts.run_task") < kinds.index("tts.first_audio")
        task = FakeTts.instances[-1]
        run = [packet for packet in task.sent if packet["header"]["action"] == "run-task"]
        assert len(run) == 1 and "河南话" in run[0]["payload"]["parameters"]["instruction"]
        assert "四川话" not in str(task.sent)
        assert "instruction" not in str([p for p in task.sent if p["header"]["action"] == "continue-task"])
        await output.cancel("interrupted")
        assert output.snapshot()["speech_style"] is None
        await output.feed("two", identity, "再见。")
        await output.finish("two")
        await asyncio.wait_for(output.worker, 1)
        assert [turn for turn, _ in calls] == ["one", "two"]
        assert output.snapshot()["speech_style"]["dialect"] == "sichuan"
    finally:
        await output.close()


@async_test
async def test_absent_style_provider_preserves_original_run_task():
    robot = FakeRobot()
    output = SpeechOutput(robot, FakePublisher(), url="wss://example.test/tts", key="fake",
                          voice="keep", socket_factory=FakeTts)
    try:
        await output.feed("one", robot.session_state(), "你好。")
        await output.finish("one")
        await asyncio.wait_for(output.worker, 1)
        assert output.snapshot()["speech_style"] is None
        assert FakeTts.instances[-1].sent[0] == command("run-task", FakeTts.instances[-1].sent[0]["header"]["task_id"], voice="keep")
    finally:
        await output.close()


@pytest.mark.parametrize("emotion", ["neutral", "warm", "playful", "sarcastic", "curious", "serious", "sad", "excited", "calm"])
def test_every_expression_stays_conservative(emotion):
    style = choose_speech_style(emotion=emotion)
    assert style.emotion == emotion
    assert .9 <= style.rate <= 1.12 and .95 <= style.pitch <= 1.05
    assert style.volume == 50


def test_cantonese_uses_documented_hint_not_unknown_yue_wire_value():
    style = choose_speech_style(language="yue")
    params = command("run-task", "task", style=style)["payload"]["parameters"]
    assert params["language_hints"] == ["zh"]
    assert "粤语" in params["instruction"]
    english = choose_speech_style(language="en", dialect="henan")
    assert english.dialect == "unknown"
    assert command("run-task", "task", style=english)["payload"]["parameters"]["language_hints"] == ["en"]


@async_test
async def test_generation_change_during_open_does_not_lock_or_send_stale_style():
    opened, release, calls = asyncio.Event(), asyncio.Event(), []

    class SlowOpen(FakeTts):
        async def open(self):
            opened.set()
            await release.wait()
            return self

    robot = FakeRobot()
    output = SpeechOutput(robot, FakePublisher(), url="wss://example.test/tts", key="fake",
                          voice="keep", socket_factory=SlowOpen,
                          style_provider=lambda *args: calls.append(args) or SpeechStyle())
    try:
        await output.feed("stale", robot.session_state(), "你好。")
        await asyncio.wait_for(opened.wait(), 1)
        robot.generation += 1
        release.set()
        await asyncio.wait_for(output.worker, 1)
        assert calls == []
        assert output.snapshot()["speech_style"] is None
        assert SlowOpen.instances[-1].sent == []
        assert SlowOpen.instances[-1].closed
    finally:
        await output.close()


@async_test
async def test_bad_provider_value_is_diagnosed_without_sending_cloud_parameters():
    events = []
    robot = FakeRobot()
    output = SpeechOutput(robot, FakePublisher(), url="wss://example.test/tts", key="fake",
                          voice="keep", socket_factory=FakeTts, style_provider=lambda *_: {"rate": 7},
                          event=lambda kind, **data: events.append((kind, data)))
    try:
        await output.feed("bad", robot.session_state(), "你好。")
        await output.finish("bad")
        await asyncio.wait_for(output.worker, 1)
        assert "SpeechStyle or None" in output.error
        assert FakeTts.instances[-1].sent == []
        assert output.snapshot()["speech_style"] is None
        assert any(kind == "tts.error" for kind, _ in events)
    finally:
        await output.close()


@pytest.mark.parametrize("language", ["hi", "tr", "uk"])
def test_asr_language_not_supported_as_tts_hint_still_builds_valid_task(language):
    style = choose_speech_style(language=language, source="asr_language")
    assert style.language == language
    params = command("run-task", "task", style=style)["payload"]["parameters"]
    assert "language_hints" not in params
    assert params["rate"] == 1. and params["voice"] == ""


@pytest.mark.parametrize("language,dialect", [
    ("en", "henan"), ("ja", "mandarin"), ("uk", "cantonese"),
    ("yue", "sichuan"), ("yue", "mandarin"),
])
def test_style_constructor_rejects_language_dialect_mismatch(language, dialect):
    with pytest.raises(ValueError, match="dialect"):
        SpeechStyle(language=language, dialect=dialect)


@pytest.mark.parametrize("source", ["asr_language", "chat_preference", "robot_preference",
                                   "llm_inferred", "llm_early", "explicit_user"])
def test_runtime_source_labels_are_preserved_not_enumerated(source):
    assert choose_speech_style(source=source).source == source


@async_test
async def test_unsupported_hint_diagnoses_and_keeps_speech_pcm_running():
    robot, publisher, events = FakeRobot(), FakePublisher(), []
    style = choose_speech_style(language="hi", source="asr_language")
    output = SpeechOutput(robot, publisher, url="wss://example.test/tts", key="fake",
                          voice="keep", socket_factory=FakeTts,
                          style_provider=lambda *_: style,
                          event=lambda kind, **data: events.append((kind, data)))
    try:
        await output.feed("hindi", robot.session_state(), "नमस्ते।")
        await output.finish("hindi")
        await asyncio.wait_for(output.worker, 1)
        assert output.error is None and output.samples > 0
        assert output.snapshot()["speech_style"]["language"] == "hi"
        assert any(kind == "tts.language_hint.unsupported" and data["language"] == "hi"
                   for kind, data in events)
        assert "language_hints" not in FakeTts.instances[-1].sent[0]["payload"]["parameters"]
    finally:
        await output.close()

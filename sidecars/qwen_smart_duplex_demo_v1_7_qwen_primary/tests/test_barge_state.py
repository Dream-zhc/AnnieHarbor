import asyncio
import json

from config import Settings
from session import VoiceSession


class FakeBrowser:
    def __init__(self):
        self.events = []
        self.bytes = []

    async def send_text(self, raw):
        self.events.append(json.loads(raw))

    async def send_bytes(self, data):
        self.bytes.append(data)


class FakeTTS:
    def __init__(self):
        self.cancel_count = 0

    async def cancel(self):
        self.cancel_count += 1


class FakeSensor:
    def __init__(self):
        self.injected = []

    async def inject_assistant_text(self, text):
        self.injected.append(text)


def run(coro):
    return asyncio.run(coro)


def test_hold_is_silent_and_reversible():
    browser = FakeBrowser()
    session = VoiceSession(browser, Settings())
    session.ai_speaking = True

    async def scenario():
        await session.begin_interrupt_candidate("local")
        assert session.interrupt_state == "holding"
        assert any(e["type"] == "playback.hold" for e in browser.events)
        assert not any(e["type"] == "playback.duck" for e in browser.events)
        await session.reject_interrupt("turn_invalid")
        assert any(e["type"] == "playback.resume" for e in browser.events)

    run(scenario())


def test_confirm_clears_before_cloud_cancel_and_invalidates_generation():
    browser = FakeBrowser()
    session = VoiceSession(browser, Settings())
    session.tts = FakeTTS()
    session.sensor = FakeSensor()
    session.ai_speaking = True
    session.generation = 4
    session.turn_users[4] = "上一个问题"

    async def scenario():
        await session.begin_interrupt_candidate("smart_turn")
        await session.confirm_interrupt("stable_asr_prefix")
        assert session.generation == 5
        assert session.tts.cancel_count == 1
        clear_index = next(i for i, e in enumerate(browser.events) if e["type"] == "playback.clear")
        trace_index = next(i for i, e in enumerate(browser.events) if e["type"] == "trace" and "确认打断" in e["message"])
        assert clear_index < trace_index
        assert session.sensor.injected == ["[上一轮回答被用户打断，用户没有听完。]"]

    run(scenario())


def test_tts_finished_does_not_mark_browser_playback_done():
    browser = FakeBrowser()
    session = VoiceSession(browser, Settings())
    session.ai_speaking = True

    async def scenario():
        await session.on_tts_event({"type": "tts.task-finished", "generation": 2})
        assert session.ai_speaking is True
        assert any(e["type"] == "playback.source_finished" for e in browser.events)

    run(scenario())


def test_hybrid_cloud_speech_start_hard_cuts_without_waiting_for_asr():
    browser = FakeBrowser()
    session = VoiceSession(browser, Settings())
    session.tts = FakeTTS()
    session.sensor = FakeSensor()
    session.ai_speaking = True
    session.generation = 7
    session.turn_users[7] = "旧问题"
    session.barge_mode = "hybrid"

    async def scenario():
        await session.on_sensor_event(
            {
                "type": "input_audio_buffer.speech_started",
                "item_id": "item_new",
            }
        )
        assert session.interrupt_state == "confirmed"
        assert session.generation == 8
        assert session.tts.cancel_count == 1
        clear = next(e for e in browser.events if e["type"] == "playback.clear")
        assert clear["reason"] == "cloud_speech_started"
        assert not any(e["type"] == "playback.resume" for e in browser.events)

    run(scenario())


def test_semantic_mode_cloud_speech_start_only_holds():
    browser = FakeBrowser()
    session = VoiceSession(browser, Settings())
    session.ai_speaking = True
    session.generation = 3
    session.barge_mode = "semantic"

    async def scenario():
        await session.on_sensor_event(
            {
                "type": "input_audio_buffer.speech_started",
                "item_id": "item_semantic",
            }
        )
        assert session.interrupt_state == "holding"
        assert any(e["type"] == "playback.hold" for e in browser.events)
        assert not any(e["type"] == "playback.clear" for e in browser.events)

    run(scenario())


def test_local_release_rejects_speculative_hold_before_fixed_timeout():
    browser = FakeBrowser()
    session = VoiceSession(browser, Settings())
    session.ai_speaking = True

    async def scenario():
        await session.begin_interrupt_candidate("local")
        await session.on_browser_event('{"type":"local.barge_release"}')
        assert session.interrupt_state == "idle"
        resume = next(e for e in browser.events if e["type"] == "playback.resume")
        assert resume["reason"] == "local_release"

    run(scenario())


def test_meaningful_ambient_is_promoted_instead_of_swallowed():
    browser = FakeBrowser()
    session = VoiceSession(browser, Settings())
    started = []

    async def fake_start(text):
        started.append(text)

    session.start_external_turn = fake_start

    async def scenario():
        await session.on_sensor_event(
            {
                "type": "conversation.item.ambient_audio_transcription.completed",
                "transcript": "还没吧。",
            }
        )
        assert started == ["还没吧。"]
        assert any(e["type"] == "asr.ambient_promoted" for e in browser.events)

    run(scenario())


def test_short_backchannel_ambient_remains_ignored_while_ai_speaks():
    browser = FakeBrowser()
    session = VoiceSession(browser, Settings())
    session.ai_speaking = True
    session.interrupt_state = "holding"
    started = []

    async def fake_start(text):
        started.append(text)

    session.start_external_turn = fake_start

    async def scenario():
        await session.on_sensor_event(
            {
                "type": "conversation.item.ambient_audio_transcription.completed",
                "transcript": "嗯。",
            }
        )
        assert started == []
        assert any(e["type"] == "asr.ambient_final" for e in browser.events)
        assert any(e["type"] == "playback.resume" for e in browser.events)

    run(scenario())


def test_qwen_mode_cloud_speech_start_holds_and_preserves_audio():
    browser = FakeBrowser()
    session = VoiceSession(browser, Settings())
    session.tts = FakeTTS()
    session.ai_speaking = True
    session.generation = 9
    session.barge_mode = "qwen"

    async def scenario():
        await session.on_sensor_event(
            {
                "type": "input_audio_buffer.speech_started",
                "item_id": "item_qwen",
            }
        )
        assert session.interrupt_state == "holding"
        hold = next(e for e in browser.events if e["type"] == "playback.hold")
        assert hold["source"] == "smart_turn"
        assert hold["preserve_audio"] is True
        assert session.tts.cancel_count == 0
        assert not any(e["type"] == "playback.clear" for e in browser.events)

        await session.on_sensor_event(
            {
                "type": "input_audio_buffer.speech_stopped",
                "item_id": "item_qwen",
                "reason": "turn_invalid",
            }
        )
        assert session.interrupt_state == "idle"
        assert any(e["type"] == "playback.resume" for e in browser.events)
        assert session.tts.cancel_count == 0

    run(scenario())


def test_qwen_mode_ignores_local_noise_candidate():
    browser = FakeBrowser()
    session = VoiceSession(browser, Settings())
    session.ai_speaking = True
    session.barge_mode = "qwen"

    async def scenario():
        await session.on_browser_event(
            '{"type":"local.barge_candidate","dbfs":-40,"snr":60}'
        )
        assert session.interrupt_state == "idle"
        assert not any(
            e["type"] in {"playback.hold", "playback.clear", "playback.duck"}
            for e in browser.events
        )

    run(scenario())


def test_qwen_mode_valid_cloud_prefix_confirms_after_provisional_hold():
    browser = FakeBrowser()
    session = VoiceSession(browser, Settings())
    session.tts = FakeTTS()
    session.sensor = FakeSensor()
    session.ai_speaking = True
    session.generation = 5
    session.turn_users[5] = "旧问题"
    session.barge_mode = "qwen"

    async def scenario():
        await session.on_sensor_event(
            {"type": "input_audio_buffer.speech_started", "item_id": "new"}
        )
        await session.on_sensor_event(
            {
                "type": "conversation.item.input_audio_transcription.delta",
                "text": "不对，我",
                "stash": "",
            }
        )
        assert session.interrupt_state == "confirmed"
        assert session.tts.cancel_count == 1
        assert any(
            e["type"] == "playback.clear" and e["reason"] == "stable_asr_prefix"
            for e in browser.events
        )

    run(scenario())

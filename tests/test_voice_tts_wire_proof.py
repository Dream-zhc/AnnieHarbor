"""Chosen voice really reaches Qwen's TTS run-task, without raw IDs in events."""

import asyncio
from hashlib import sha256

from annie_runtime.speech import SpeechOutput
from tests.test_phase4b import FakePublisher, FakeRobot, FakeTts


def test_selected_voice_is_locked_in_wire_and_fingerprinted_in_diagnostics():
    async def exercise():
        events = []
        selected = "qwen-audio-3.0-tts-flash-annie-new-clone"
        output = SpeechOutput(
            FakeRobot(), FakePublisher(), url="wss://fixture/tts", key="secret",
            voice="original", voice_provider=lambda _: selected,
            socket_factory=FakeTts,
        )
        output.event = lambda kind, **data: events.append((kind, data))
        try:
            await output.feed("new-voice-turn", output.robot.session_state(), "你好，新音色。")
            await output.finish("new-voice-turn")
            await asyncio.wait_for(output.worker, 2)
            wire = FakeTts.instances[-1].sent[0]["payload"]["parameters"]["voice"]
            assert wire == selected
            event = next(data for kind, data in events if kind == "tts.run_task")
            assert event["voice_fingerprint"] == sha256(selected.encode()).hexdigest()[:12]
            assert selected not in str(events)
            assert "secret" not in str(events)
        finally:
            await output.close()
    asyncio.run(exercise())

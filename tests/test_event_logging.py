"""11-20: the runtime log missed barge-in timing, what she was heard saying, dropped
speech, sensor decisions, motions, TTS style and wake-state changes."""

import asyncio
import logging

from annie_runtime.presence import DialoguePresence

from tests.test_wake_call_recovery import ready, runtime


def lines(caplog, prefix):
    return [record.getMessage() for record in caplog.records
            if record.getMessage().startswith(prefix)]


def test_every_wake_state_change_is_logged(caplog):
    presence = DialoguePresence()
    presence.label = "annie-a"
    with caplog.at_level(logging.INFO, logger="annie_runtime.presence"):
        presence.state = "WAKE_WAIT"
        presence.state = "WAKE_WAIT"  # no change, no line
        presence.close()
    assert lines(caplog, "wake_state ") == [
        "wake_state robot=annie-a from=SLEEP to=WAKE_WAIT expirations=0",
        "wake_state robot=annie-a from=WAKE_WAIT to=SLEEP expirations=0",
    ]


def test_published_events_the_log_used_to_miss(tmp_path, caplog):
    async def run():
        rt = runtime(tmp_path)
        client = await ready(rt)
        try:
            with caplog.at_level(logging.INFO, logger="annie_runtime.realtime"):
                client.publish("transcript.dropped", text="就差一点，你要早再早个一个月" * 3,
                               item_id="u1", reason="turn_invalid")
                client.publish("event.policy", event_kind="sensor.pressure", event_id="e1",
                               decision="ignore", status="dropped", reason="habituated",
                               transport="room")
                client.publish("action.state", turn_id="t-123456789", call_id="listen-abc",
                               action_kind="action", action_value="listen_tilt",
                               status="send_unknown")
                client.publish("action.state", turn_id="t-123456789", call_id="listen-abc",
                               action_kind="action", action_value="listen_tilt",
                               status="device_finished")
                client.publish("tts.playback.held", turn_id="t-123456789",
                               conversation_id="t-123456789", played_sample_count=48000,
                               source="speaker")
                client.publish("tts.run_task", turn_id="t-123456789", segment_id=2,
                               parameters={"instruction": "轻快地笑着说", "rate": 1.0, "volume": 50},
                               speech_style={"emotion": "happy"}, expression_source="model")
                client.publish("turn.done", turn_id="t-123456789", text="好嘞，我先退下啦。",
                               status="completed", first_text_ms=312.5, cancel_source=None)
                client.publish("acoustic.observed", language="zh", emotion="happy",
                               utterance_id="x", observed_monotonic_ms=1, application="next_turn")
            logged = lines(caplog, "event ")
            assert len(logged) == 7  # the send_unknown motion step stays quiet
            dropped, policy, action, held, run_task, done, heard = logged
            assert "emotion='happy'" in heard and "language='zh'" in heard
            assert "kind=transcript.dropped" in dropped and "reason='turn_invalid'" in dropped
            assert "就差一点，你要早再早个一个月就差一点" in dropped
            assert "event='sensor.pressure'" in policy and "reason='habituated'" in policy
            assert "script='listen_tilt'" in action and "status='device_finished'" in action
            assert "played_ms=2000" in held and "source='speaker'" in held
            assert "emotion='happy'" in run_task and "instruction='轻快地笑着说'" in run_task
            assert "status='completed'" in done and "chars=9" in done and "first_text_ms=312.5" in done
        finally:
            await rt.close()
    asyncio.run(run())

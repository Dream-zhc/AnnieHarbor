"""Companion speech latency parameters and bounded slow-output regressions."""

import asyncio
import shutil
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import pytest
from annie_runtime.speech import SpeechOutput

from tests.test_phase4b import FakePublisher, FakeRobot, FakeTts


def speech(*, publisher=None, socket_factory=FakeTts, **kwargs):
    robot = FakeRobot()
    return SpeechOutput(
        robot, publisher or FakePublisher(), url="wss://example.test/tts", key="fake",
        voice="test", socket_factory=socket_factory, **kwargs,
    )


def test_bootstrap_passes_provider_sentence_lengths(tmp_path):
    from annie_runtime import __main__ as bootstrap

    root = Path(__file__).resolve().parents[1]
    shutil.copytree(root / "config", tmp_path / "config")
    runtime = Mock()
    runtime.preflight.side_effect = ValueError("stop before network")
    with patch.object(bootstrap, "load_local_env"), \
         patch.object(bootstrap, "HarborRuntime", return_value=runtime) as factory, \
         patch("sys.argv", ["annie_runtime", "--root", str(tmp_path), "--check"]), \
         patch.dict("os.environ", {}, clear=True):
        with pytest.raises(SystemExit) as result:
            bootstrap.main()
    assert result.value.code == 2
    assert factory.call_args.kwargs["tts_first_chars"] == 2
    assert factory.call_args.kwargs["tts_min_chars"] == 4
    assert factory.call_args.kwargs["tts_max_chars"] == 30


@pytest.mark.parametrize("kwargs", [
    {"first_chars": 0}, {"first_chars": True}, {"max_chars": 0},
    {"min_chars": 0}, {"min_chars": 31, "max_chars": 30},
    {"first_chars": 31, "max_chars": 30}, {"min_chars": 1.5},
])
def test_sentence_lengths_require_positive_bounded_integers(kwargs):
    with pytest.raises(ValueError, match="chars"):
        speech(**kwargs)


def test_default_sentence_threshold_preserves_existing_behavior():
    output = speech()
    assert output.min_chars == output.first_chars == 10
    assert output.max_chars == 48


class CompanionSpeechTests(unittest.IsolatedAsyncioTestCase):
    async def test_short_opening_then_configured_sentence_threshold(self):
        output = speech(first_chars=2, min_chars=4, max_chars=30, first_wait_ms=5000)
        try:
            identity = output.robot.session_state()
            await output.feed("turn", identity, "你好")
            assert output.queue.get_nowait() == "你好"
            output.queue.task_done()
            await output.feed("turn", identity, "好。")
            assert output.queue.empty()
            await output.feed("turn", identity, "嗯。")
            assert output.queue.get_nowait() == "好。嗯。"
            output.queue.task_done()
        finally:
            await output.close()


    async def test_short_sentence_still_flushes_without_later_delta(self):
        output = speech(first_chars=2, min_chars=4, max_chars=30, first_wait_ms=1)
        try:
            identity = output.robot.session_state()
            await output.feed("turn", identity, "你好")
            await output.feed("turn", identity, "好。")
            for _ in range(100):
                if output.submitted_text == "你好好。":
                    break
                await asyncio.sleep(.001)
            assert output.submitted_text == "你好好。"
            await output.finish("turn")
            await asyncio.wait_for(output.worker, 1)
            assert output.error is None
        finally:
            await output.close()


    async def test_overflow_cancels_slow_output_without_blocking_feed_or_new_turn(self):
        send_entered, release_send = asyncio.Event(), asyncio.Event()
        stop_entered, release_stop = asyncio.Event(), asyncio.Event()

        class SlowTts(FakeTts):
            async def send(self, message):
                if message["header"]["action"] == "continue-task":
                    send_entered.set()
                    await release_send.wait()
                await super().send(message)

        class SlowStopPublisher(FakePublisher):
            async def publish(self, message):
                if message["data"].get("reason") == "tts_text_limit":
                    stop_entered.set()
                    await release_stop.wait()
                await super().publish(message)

        events = []
        output = speech(publisher=SlowStopPublisher(), socket_factory=SlowTts,
                        first_chars=2, max_chars=30, first_wait_ms=5000,
                        event=lambda name, **data: events.append((name, data)))
        identity = output.robot.session_state()
        try:
            await output.feed("turn", identity, "你好")
            await asyncio.wait_for(send_entered.wait(), 1)
            await output.feed("turn", identity, "啊" * 7998)
            assert output.pending
            assert output.queue.qsize() == 8
            await asyncio.wait_for(output.feed("turn", identity, "多"), .1)
            assert output.failed_turn == "turn"
            assert output.error == "tts_text_limit: exceeded 8000 chars"
            assert output.pending == ""
            assert output.queue.empty()
            assert output.cancelled
            await asyncio.wait_for(stop_entered.wait(), 1)
            await asyncio.wait_for(output.feed("turn", identity, "不能继续合成"), .1)
            assert len(SlowTts.instances[-1].sent) == 1  # only run-task, blocked text cancelled
            assert any(name == "tts.error" for name, _ in events)
            release_stop.set()
            release_send.set()
            await output.feed("next", identity, "正常下一轮。")
            await output.finish("next")
            await asyncio.wait_for(output.worker, 1)
            assert output.failed_turn is None
            assert output.error is None
            assert output.submitted_text == "正常下一轮。"
            assert output.publisher.messages[-1]["data"]["stream_final"]
        finally:
            release_stop.set()
            release_send.set()
            await output.close()


    async def test_limit_counts_submitted_text_not_only_pending_queue(self):
        output = speech(first_chars=2, max_chars=40, first_wait_ms=5000)
        try:
            identity = output.robot.session_state()
            await output.feed("turn", identity, "啊" * 8000)
            await asyncio.wait_for(output.queue.join(), 1)
            assert len(output.submitted_text) == 8000
            assert output.pending == ""
            await output.feed("turn", identity, "多")
            assert output.failed_turn == "turn"
            assert output.error == "tts_text_limit: exceeded 8000 chars"
        finally:
            await output.close()

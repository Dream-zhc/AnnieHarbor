"""TTS failure recovery: real orchestration with injected transports, no hardware claims."""

import asyncio
import time
from unittest.mock import patch

from annie_runtime.reminders import ReminderService
from annie_runtime.speech import SpeechOutput

from tests.test_phase4b import FakePublisher, FakeRobot, FakeTts, receipt
from tests.test_phase8_reminders import build, ready


class FailedAfterAudio(FakeTts):
    async def send(self, message):
        if message['header']['action'] == 'finish-task':
            task = message['header']['task_id']
            await self.received.put(b'\x01\x00' * 480)
            # Let the publication task consume real PCM before failure.
            await asyncio.sleep(.02)
            await self.received.put({'header': {
                'event': 'task-failed', 'task_id': task, 'error_code': 'provider_stream_failed',
            }})
        else:
            await super().send(message)


def output_for(robot=None, publisher=None, factory=FailedAfterAudio):
    events = []
    output = SpeechOutput(
        robot or FakeRobot(), publisher or FakePublisher(),
        url='wss://example.test/api-ws/v1/inference', key='fake', voice='keep',
        socket_factory=factory, first_chars=2,
        event=lambda kind, **fields: events.append((kind, fields)),
    )
    return output, events


async def fail(output):
    await output.feed('broken', output.robot.session_state(), '讲故事。')
    await output.finish('broken')
    await asyncio.wait_for(output.worker, 1)
    assert 'provider_stream_failed' in output.error
    assert output.samples == 480


def test_failed_stream_releases_wait_after_stop_grace_without_claiming_spoken():
    async def run():
        output, events = output_for()
        try:
            await fail(output)
            assert output.local_output_stopped_at is not None
            assert ReminderService._speech_blocks(output)
            output.local_output_stopped_at -= 3
            assert not ReminderService._speech_blocks(output)
            assert not output.synthesized_complete
            assert output.playback_evidence == 'unknown'
            assert any(k == 'tts.output_stopped' for k, _ in events)
            assert not output.robot.context_cache
            # Late model deltas must not resurrect the failed task.
            await output.feed('broken', output.robot.session_state(), '迟到的文字。')
            assert output.queue.empty() and not output.pending
            output.socket_factory = FakeTts
            await output.feed('next', output.robot.session_state(), '新一轮。')
            await output.finish('next')
            await asyncio.wait_for(output.worker, 1)
            assert output.error is None and output.synthesized_complete
            assert output.local_output_stopped_at is None
        finally:
            await output.close()
    asyncio.run(run())


def test_speaker_progress_blocks_failed_stream_even_after_browser_stopped():
    async def run():
        output, _ = output_for()
        try:
            await fail(output)
            epoch = output.stream_epoch
            assert output.on_playback_receipt(receipt('started', 'broken', epoch, 0, source='speaker'))
            assert output.on_playback_receipt(receipt('stopped', 'broken', epoch, 480, seq=2))
            output.local_output_stopped_at -= 3
            assert ReminderService._speech_blocks(output)
            assert output.on_playback_receipt(receipt('stopped', 'broken', epoch, 480,
                                                     seq=2, source='speaker'))
            assert not ReminderService._speech_blocks(output)
            assert not output.robot.context_cache
        finally:
            await output.close()
    asyncio.run(run())


def test_failed_stream_does_not_hold_due_reminder_until_realtime_reconnect(tmp_path):
    async def run():
        rt = build(tmp_path)
        await ready(rt)
        client = rt.realtime['a']
        output, _ = output_for(rt.robots['a'])
        original = client.speech
        client.speech = output
        try:
            row = await rt.reminder_create('a', '闹钟响了', 60000, 'failure-regression')
            socket = client.socket
            await fail(output)
            output.local_output_stopped_at -= 3
            result = await rt.reminders.dispatch('a', now_ms=row['due_ms'])
            assert result['status'] == 'submitted_unknown'
            assert client.socket is socket and client.ready
            assert sum(p['type'] == 'response.create' for p in socket.outbound) == 1
        finally:
            client.speech = original
            await output.close()
            await rt.close()
    asyncio.run(run())


def test_failed_stop_retries_boundedly_and_keeps_unknown_if_both_sends_fail():
    class BrokenStop(FakePublisher):
        def __init__(self):
            super().__init__()
            self.stop_attempts = 0

        async def publish(self, message):
            if message['data'].get('stop'):
                self.stop_attempts += 1
                await asyncio.Event().wait()
            await super().publish(message)

    async def run():
        publisher = BrokenStop()
        output, events = output_for(publisher=publisher)
        try:
            with patch('annie_runtime.speech.FAILED_STOP_TIMEOUT_SECONDS', .01), \
                    patch('annie_runtime.speech.FAILED_STOP_RETRY_SECONDS', .001):
                await fail(output)
            assert publisher.stop_attempts == 2
            assert output.local_output_stopped_at is None
            assert ReminderService._speech_blocks(output)
            assert any(k == 'tts.recovery_failed' for k, _ in events)
            assert 'provider_stream_failed' in output.error
        finally:
            # Avoid the normal cancel's real one-second deadline in this fake.
            output.publisher = FakePublisher()
            await output.close()
    asyncio.run(run())


def test_failed_stop_first_send_recovers_on_second_without_replaying_pcm():
    class FlakyStop(FakePublisher):
        def __init__(self):
            super().__init__()
            self.stop_attempts = 0

        async def publish(self, message):
            if message['data'].get('stop'):
                self.stop_attempts += 1
                if self.stop_attempts == 1:
                    raise ConnectionError('room link lost')
            await super().publish(message)

    async def run():
        publisher = FlakyStop()
        output, _ = output_for(publisher=publisher)
        try:
            await fail(output)
            assert publisher.stop_attempts == 2
            assert output.local_output_stopped_at is not None
            assert len([p for p in publisher.messages if p['data'].get('audio')]) == 1
            assert output.playback_evidence == 'unknown'
        finally:
            await output.close()
    asyncio.run(run())


def test_transient_tts_connect_retries_once_before_submitting_any_text():
    class FlakyConnect(FakeTts):
        attempts = 0

        async def open(self):
            type(self).attempts += 1
            if self.attempts == 1:
                raise ConnectionError('temporary connect failure')
            return await super().open()

    async def run():
        FlakyConnect.attempts = 0
        output, events = output_for(factory=FlakyConnect)
        try:
            await output.feed('connect', output.robot.session_state(), '你好。')
            await output.finish('connect')
            await asyncio.wait_for(output.worker, 2)
            assert output.error is None
            assert FlakyConnect.attempts == 2
            assert output.submitted_text == '你好。'
            assert any(k == 'tts.connect_retry' for k, _ in events)
        finally:
            await output.close()
    asyncio.run(run())


def test_failed_turn_rejects_late_hold_and_late_mouth_scripts():
    async def run():
        output, _ = output_for()
        try:
            await fail(output)
            assert not output.request_playback_control('resume', reason='turn_invalid')
            assert not output.on_mouth_script({
                'type': 'mouth_script', 'conversation_id': 'broken',
                'stream_epoch': output.stream_epoch, 'start_sample': 0, 'end_sample': 400,
                'timing_source': 'qwen3_forced_aligner_online',
            })
        finally:
            await output.close()
    asyncio.run(run())


def test_permanent_connect_failure_is_bounded_and_does_not_submit_text():
    class DeadConnect(FakeTts):
        attempts = 0

        async def open(self):
            type(self).attempts += 1
            raise ConnectionError('provider unavailable')

    async def run():
        DeadConnect.attempts = 0
        output, events = output_for(factory=DeadConnect)
        try:
            await output.feed('dead', output.robot.session_state(), '你好。')
            await output.finish('dead')
            await asyncio.wait_for(output.worker, 2)
            assert DeadConnect.attempts == 2
            assert output.submitted_text == '' and output.samples == 0
            error = next(data for kind, data in events if kind == 'tts.error')
            assert error['error_stage'] == 'provider_connect'
            assert 'provider unavailable' in error['error']
        finally:
            await output.close()
    asyncio.run(run())


def test_cancellation_during_connect_retry_never_resurrects_failed_audio():
    class DeadConnect(FakeTts):
        async def open(self):
            raise ConnectionError('provider unavailable')

    async def run():
        output, events = output_for(factory=DeadConnect)
        try:
            await output.feed('cancel-retry', output.robot.session_state(), '你好。')
            for _ in range(100):
                if any(kind == 'tts.connect_retry' for kind, _ in events):
                    break
                await asyncio.sleep(.001)
            assert any(kind == 'tts.connect_retry' for kind, _ in events)
            await asyncio.wait_for(output.cancel('valid_smart_turn'), .5)
            count = len(events)
            await asyncio.sleep(.3)
            assert len(events) == count
            assert output.worker is None and output.turn_id is None
            assert output.samples == 0
        finally:
            await output.close()
    asyncio.run(run())


def test_text_limit_cleanup_and_close_complete_without_cyclic_wait():
    # Isolate the known cancellation cycle so a regression cannot hang pytest itself.
    import subprocess
    import sys

    script = (
        'import unittest; '
        'from tests.test_companion_speech import CompanionSpeechTests; '
        'suite=unittest.TestSuite([CompanionSpeechTests('
        '"test_limit_counts_submitted_text_not_only_pending_queue")]); '
        'result=unittest.TextTestRunner().run(suite); '
        'raise SystemExit(not result.wasSuccessful())'
    )
    result = subprocess.run([sys.executable, '-c', script], capture_output=True, text=True, timeout=3)
    assert result.returncode == 0, result.stderr


def test_cancel_during_failed_stop_retry_cannot_stop_replacement_turn():
    class PausedStop(FakePublisher):
        def __init__(self):
            super().__init__()
            self.entered = asyncio.Event()

        async def publish(self, message):
            if message['data'].get('reason') == 'tts_error':
                self.entered.set()
                await asyncio.Event().wait()
            await super().publish(message)

    async def run():
        publisher = PausedStop()
        output, _ = output_for(publisher=publisher)
        identity = output.robot.session_state()
        try:
            await output.feed('broken', identity, '讲故事。')
            await output.finish('broken')
            await asyncio.wait_for(publisher.entered.wait(), 1)
            await asyncio.wait_for(output.cancel('valid_smart_turn'), .5)
            output.socket_factory = FakeTts
            await output.feed('new', identity, '新一轮。')
            await output.finish('new')
            await asyncio.wait_for(output.worker, 1)
            await asyncio.sleep(.15)
            packets = [p['data'] for p in publisher.messages]
            index = next(i for i, p in enumerate(packets) if p['conversation_id'] == 'new')
            assert all(p['conversation_id'] == 'new' for p in packets[index:])
            assert output.turn_id == 'new' and output.synthesized_complete
            assert output.local_output_stopped_at is None
        finally:
            await output.close()
    asyncio.run(run())


def test_cancel_returns_when_provider_close_ignores_cancellation():
    """A stuck TTS close must not pin the receive loop on the open speech flag."""

    class StickyClose(FakeTts):
        async def close(self):
            task = asyncio.current_task()
            if task is not None and task.cancelling():
                task.uncancel()
            await asyncio.sleep(30)

    async def run():
        output, _events = output_for(factory=StickyClose)
        identity = output.robot.session_state()
        try:
            await output.feed('story', identity, '讲个故事。')
            for _ in range(100):
                if output.worker is not None and output.socket is not None:
                    break
                await asyncio.sleep(.01)
            assert output.worker is not None and output.socket is not None
            started = time.monotonic()
            await asyncio.wait_for(output.cancel('valid_smart_turn'), 2)
            assert time.monotonic() - started < 1.7
            assert output.turn_id is None and output.worker is None
            assert output.queue.empty()
            output.socket_factory = FakeTts
            await output.feed('next', identity, '下一句。')
            await output.finish('next')
            await asyncio.wait_for(output.worker, 1)
            assert output.turn_id == 'next' and output.synthesized_complete
        finally:
            output.socket = None
            output._mixed_sockets.clear()
            await output.close()

    asyncio.run(run())


def test_due_worker_recovers_automatically_without_manual_dispatch(tmp_path):
    async def run():
        rt = build(tmp_path)
        await ready(rt)
        client = rt.realtime['a']
        output, _ = output_for(rt.robots['a'])
        output.event = client.publish
        original = client.speech
        client.speech = output
        try:
            with patch('annie_runtime.reminders.RETRY_SECONDS', .05), \
                    patch('annie_runtime.reminders.UNCONFIRMED_PLAYBACK_GRACE_SECONDS', .02):
                row = await rt.reminder_create('a', '闹钟响了', 1000, 'automatic-recovery')
                socket = client.socket
                await fail(output)

                async def delivered():
                    while True:
                        saved = await asyncio.to_thread(rt.reminders.store.get, 'a', row['task_id'])
                        if saved['status'] == 'submitted_unknown':
                            return
                        await asyncio.sleep(.01)

                await asyncio.wait_for(delivered(), 1.5)
                assert client.socket is socket and client.ready
                assert sum(p['type'] == 'response.create' for p in socket.outbound) == 1
        finally:
            client.speech = original
            await output.close()
            await rt.close()
    asyncio.run(run())

"""Stage B opt-in Room TTS pipeline: first duplex then bounded one-shot tasks."""

import asyncio

import pytest
from annie_core.speech_style import choose_speech_style
from annie_runtime.realtime import RealtimeRobot
from annie_runtime.speech import SpeechOutput, _tts_failure, sentence_style
from annie_runtime.speech_expression import ExpressionStream

from tests.test_phase4b import FakePublisher, FakeRobot, FakeTts


def make_output(robot=None, *, factory=FakeTts, events=None):
    robot = robot or FakeRobot()
    publisher = FakePublisher()
    events = events if events is not None else []
    output = SpeechOutput(
        robot, publisher, url="wss://example.test/api-ws/v1/inference",
        key="fake", voice="keep", socket_factory=factory, first_chars=2,
        style_provider=lambda _turn, _identity: choose_speech_style(emotion="neutral"),
        mixed_enabled=True,
        event=lambda kind, **fields: events.append((kind, fields)),
    )
    return output, publisher, events


async def send_marked(output, turn, identity, raw, size=1):
    parser = ExpressionStream()
    for offset in range(0, len(raw), size):
        speech = parser.feed(raw[offset:offset + size])
        events = parser.take_events()
        if events:
            await output.expression_events(turn, identity, events)
        if speech:
            await output.feed(turn, identity, speech)
    tail = parser.finish()
    if tail:
        await output.feed(turn, identity, tail)
    await output.finish(turn)
    await asyncio.wait_for(output.worker, 3)
    return parser


@pytest.mark.parametrize("size", [1, 5, 1024])
def test_mixed_single_room_monotonic_samples_and_real_run_task_style(size):
    async def run():
        output, publisher, events = make_output()
        identity = output.robot.session_state()
        seen = len(FakeTts.instances)
        try:
            parser = await send_marked(
                output, "turn-a", identity,
                "你好！[overall:playful][instruction:语气俏皮，尾音稍微上扬。]"
                "[mischievously]别小瞧我！[sentence_instruction:语气稳重，重音强调提醒。]"
                "[serious]不过安全第一。", size,
            )
            assert parser.issues == []
            assert output.error is None
            assert output.snapshot()["mixed_sentences_submitted"] == 2
            assert len(FakeTts.instances) - seen == 3
            sockets = FakeTts.instances[seen:]
            assert all(sock.closed for sock in sockets)
            assert [len([p for p in sock.sent if p['header']['action']=='run-task'])
                    for sock in sockets] == [1, 1, 1]
            assert "语气俏皮" in sockets[1].sent[0]['payload']['parameters']['instruction']
            assert "重音强调提醒" in sockets[2].sent[0]['payload']['parameters']['instruction']
            assert sockets[0].sent[0]['payload']['parameters']['enable_ssml'] is False
            assert all(sock.sent[0]['payload']['parameters']['enable_ssml'] is True
                       for sock in sockets[1:])
            assert all(sock.sent[0]['payload']['parameters']['seed'] == 42
                       for sock in sockets[1:])
            assert [p['header']['streaming'] for sock in sockets for p in sock.sent
                    if p['header']['action']=='run-task'] == ['duplex'] * 3
            later_inputs = [p['payload']['input']['text'] for sock in sockets[1:]
                            for p in sock.sent if p['header']['action']=='continue-task']
            assert later_inputs == ['[mischievously]别小瞧我！', '[serious]不过安全第一。']
            audio = [m['data'] for m in publisher.messages if m['type']=='tts' and m['data']['audio']]
            assert audio and len(audio) == 9
            assert audio[0]['start_sample'] == 0
            assert all(b['start_sample'] == a['end_sample'] for a, b in zip(audio, audio[1:]))
            assert audio[-1]['end_sample'] == 3000
            assert {p['conversation_id'] for p in audio} == {'turn-a'}
            assert len({p['stream_epoch'] for p in audio}) == 1
            assert audio[0]['text'] == '你好！'
            assert audio[-1]['text'] == '你好！别小瞧我！不过安全第一。'
            segments = [m['data'] for m in publisher.messages
                        if m['type']=='tts' and m['data'].get('final')
                        and not m['data'].get('stream_final')]
            assert [p['turn_id'] for p in segments] == ['turn-a-1', 'turn-a-2', 'turn-a-3']
            assert sum(m['data'].get('stream_final', False) for m in publisher.messages) == 1
            assert len([e for e in events if e[0]=='tts.run_task']) == 3
            assert output.submitted_text == '你好！别小瞧我！不过安全第一。'
        finally:
            await output.close()
    asyncio.run(run())


def test_no_punctuation_finishes_as_one_original_duplex_task():
    async def run():
        output, publisher, _ = make_output()
        seen = len(FakeTts.instances)
        try:
            await send_marked(output, 'single', output.robot.session_state(), '你好啊', 1)
            assert len(FakeTts.instances)-seen == 1
            assert output.snapshot()['mixed_sentences_submitted'] == 0
            assert any(m['data'].get('stream_final') for m in publisher.messages)
        finally:
            await output.close()
    asyncio.run(run())


def test_interrupt_stops_all_tasks_without_resurrecting_old_pcm():
    class SlowSecond(FakeTts):
        number = 0

        async def send(self, message):
            if message['header']['action'] == 'run-task':
                type(self).number += 1
            if type(self).number >= 2 and message['header']['action'] == 'finish-task':
                self.sent.append(message)
                return  # provider stalls after beginning the second sentence
            return await super().send(message)

    async def run():
        SlowSecond.number = 0
        output, publisher, _ = make_output(factory=SlowSecond)
        identity = output.robot.session_state()
        try:
            await output.feed('old', identity, '你好。后面还有。')
            await output.finish('old')
            for _ in range(300):
                if SlowSecond.number >= 2:
                    break
                await asyncio.sleep(.002)
            assert SlowSecond.number >= 2
            await asyncio.wait_for(output.cancel('valid_smart_turn'), 1)
            stop_index = len(publisher.messages)
            await asyncio.sleep(.02)
            assert all(m['data'].get('conversation_id') != 'old' for m in
                       publisher.messages[stop_index:] if m['type'] == 'tts')
            assert any(m['data'].get('stop') for m in publisher.messages)
            assert not output._mixed_sockets
            SlowSecond.number = 0
            await send_marked(output, 'new', identity, '新的一轮。')
            assert publisher.messages[-1]['data']['conversation_id'] == 'new'
            assert output.stream_epoch == 2
        finally:
            await output.close()
    asyncio.run(run())


def test_opening_sends_pcm_without_waiting_for_punctuation_or_metadata():
    class EarlyPCM(FakeTts):
        async def send(self, message):
            await super().send(message)
            if message['header']['action'] == 'continue-task':
                await self.received.put(bytes([1, 0]) * 480)

    async def run():
        output, publisher, _ = make_output(factory=EarlyPCM)
        identity = output.robot.session_state()
        try:
            await output.feed('immediate', identity, '你好')
            for _ in range(500):
                if output.first_publish_ms is not None:
                    break
                await asyncio.sleep(.002)
            assert output.first_publish_ms is not None
            assert not output._mixed_first_closed.is_set()
            assert output.snapshot()['mixed_sentences_submitted'] == 0
            assert publisher.messages[0]['data']['text'] == '你好'
            await output.finish('immediate')
            await asyncio.wait_for(output.worker, 2)
        finally:
            await output.close()
    asyncio.run(run())


def test_fail_before_any_later_pcm_retries_only_unsent_sentence():
    class FailSecond(FakeTts):
        number = 0

        async def send(self, message):
            if message['header']['action']=='run-task':
                type(self).number += 1
            if type(self).number==2 and message['header']['action']=='finish-task':
                self.sent.append(message)
                task = message['header']['task_id']
                await self.received.put({'header':{'event':'task-failed','task_id':task}})
                return
            return await super().send(message)

    async def run():
        FailSecond.number = 0
        output, publisher, events = make_output(factory=FailSecond)
        try:
            await send_marked(output,'retry',output.robot.session_state(),'您好。继续说。')
            assert output.error is None
            assert FailSecond.number == 3
            assert sum(1 for kind, _ in events if kind=='tts.sentence_fallback') == 1
            assert output.submitted_text == '您好。继续说。'
            assert publisher.messages[-1]['data'].get('stream_final') is True
        finally:
            await output.close()
    asyncio.run(run())


def test_two_robots_cannot_share_pcm_or_sample_epoch():
    async def run():
        a, pa, _ = make_output(FakeRobot('a', 'room-a'))
        b, pb, _ = make_output(FakeRobot('b', 'room-b'))
        try:
            await asyncio.gather(
                send_marked(a,'turn-a',a.robot.session_state(),'甲来！[serious]甲走。'),
                send_marked(b,'turn-b',b.robot.session_state(),'乙来！[curious]乙走。'),
            )
            assert {m['data']['conversation_id'] for m in pa.messages} == {'turn-a'}
            assert {m['data']['conversation_id'] for m in pb.messages} == {'turn-b'}
            assert a.samples == b.samples == 2000
            assert a.published_text == '甲来！甲走。'
            assert b.published_text == '乙来！乙走。'
        finally:
            await asyncio.gather(a.close(),b.close())
    asyncio.run(run())


def test_language_and_explicit_rate_cannot_be_overridden_by_markup():
    base = choose_speech_style(language='en', rate=.93, source='explicit_user')
    result, tag = sentence_style(base, {'language':'zh','pace':'faster',
                                        'instruction':'语气清晰，句尾稳重。','latest_tag':'serious'})
    assert result is base
    assert (result.language, result.rate) == ('en', .93)
    assert tag is None


def test_default_duplex_error_exposes_underlying_provider_failure_not_taskgroup():
    class FailedTts(FakeTts):
        async def send(self, message):
            if message['header']['action'] == 'finish-task':
                task = message['header']['task_id']
                await self.received.put({'header': {
                    'event': 'task-failed', 'task_id': task,
                    'error_code': 'provider_reject_text',
                }})
            else:
                await super().send(message)

    async def run():
        robot, publisher, events = FakeRobot(), FakePublisher(), []
        output = SpeechOutput(robot, publisher, url='wss://example.test/tts',
                              key='fake', voice='keep', socket_factory=FailedTts,
                              first_chars=2,
                              event=lambda kind, **data: events.append((kind, data)))
        try:
            await output.feed('broken', robot.session_state(), '这是需要合成的句子。')
            await output.finish('broken')
            await asyncio.wait_for(output.worker, 2)
            assert output.error is not None
            assert 'provider_receive' in output.error
            assert 'provider_reject_text' in output.error
            assert 'ExceptionGroup' not in output.error
            assert next(d for k,d in events if k == 'tts.error')['error_stage'] == 'provider_receive'
            assert any(m['data'].get('stop') for m in publisher.messages)
        finally:
            await output.close()
    asyncio.run(run())


def test_failure_summary_masks_access_tokens():
    nested = ExceptionGroup('generic', [RuntimeError('Bearer bearer-token-123')])
    message = _tts_failure(nested)
    assert 'bearer-token-123' not in message
    assert 'RuntimeError' in message


def test_multitask_evidence_not_completed_at_first_segment():
    identity = FakeRobot().session_state()
    record = {"identity": identity}

    def record_event(kind, **fields):
        RealtimeRobot._record_tts_evidence(record, {
            "kind": kind, "ts_ms": 100, **identity, **fields,
        })

    record_event('tts.mixed.started')
    record_event('tts.run_task', task_id='a', segment_id='turn-1',
                 parameters={'enable_ssml': False}, streaming='duplex')
    record_event('tts.task_started', task_id='a')
    record_event('tts.task_finished', task_id='a')
    record_event('tts.segment_done', segment_id='turn-1')
    assert record['tts']['state'] == 'started'
    record_event('tts.run_task', task_id='b', segment_id='turn-2',
                 parameters={'instruction': '轻快，句尾上扬', 'enable_ssml': True},
                 streaming='duplex')
    record_event('tts.text_submitted', task_id='b', segment_id='turn-2',
                 text='再见。', start_char=3, end_char=6)
    record_event('tts.task_finished', task_id='b')
    assert record['tts']['additional_tasks'][0]['state'] == 'completed'
    assert record['tts']['submitted_text'] == '再见。'
    assert record['tts']['state'] == 'started'
    record_event('tts.stream_finished')
    assert record['tts']['state'] == 'completed'


def test_long_punctuated_output_bounded_provider_tasks_and_no_text_loss():
    async def run():
        output, publisher, _ = make_output()
        try:
            text = '先听着！' + '多说一句。' * 40
            await send_marked(output, 'bounded', output.robot.session_state(), text, 35)
            assert output.error is None
            assert output.published_text == text
            assert output.submitted_text == text
            assert output.snapshot()['mixed_sentences_submitted'] == 17
            assert publisher.messages[-1]['data']['stream_final']
        finally:
            await output.close()
    asyncio.run(run())

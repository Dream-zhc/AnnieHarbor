"""User browser-listening report: requested delivery and selected-turn wire proof."""
import asyncio

import pytest
from annie_core import RobotConfig
from annie_core.speech_style import choose_speech_style
from annie_runtime import HarborRuntime
from annie_runtime.language import ReplyLanguageState, explicit_language_request, explicit_style_request
from annie_runtime.speech import SpeechOutput

from tests.test_phase4b import FakePublisher, FakeRobot, FakeTts
from tests.test_speech_expression_integration import FakeSocket, ready


@pytest.mark.parametrize('text,dialect', [
    ('能用陕西话讲一个故事吗', 'shaanxi'), ('讲个四川话', 'sichuan'),
    ('换成四川话', 'sichuan'), ('用河南话说一段', 'henan'),
])
def test_reported_dialect_requests_reach_policy(text, dialect):
    assert explicit_language_request(text)['dialect'] == dialect


@pytest.mark.parametrize('text', ['讲个情绪丰富的', '语气有感情一点', '讲得有抑扬顿挫'])
def test_expressive_request_is_delivery_not_a_fake_emotion_class(text):
    state = ReplyLanguageState()
    state.user_request(text)
    state.first_text('turn')
    style = state.style_for('turn')
    assert style.delivery == 'expressive'
    assert style.emotion == 'neutral'  # not every expressive sentence is happy
    assert style.source == 'explicit_user'
    assert '语义' in style.instruction and '起伏' in style.instruction
    assert (style.rate, style.pitch, style.volume) == (1., 1., 50)


@pytest.mark.parametrize('text,emotion', [('开心地说一段', 'excited'), ('用伤心的语气说', 'sad'),
                                        ('用嘲讽的语气说', 'sarcastic')])
def test_explicit_emotion_synonyms_are_not_lost(text, emotion):
    state = ReplyLanguageState()
    state.user_request(text)
    state.first_text('turn')
    assert state.style_for('turn').emotion == emotion


def test_expressive_delivery_does_not_override_serious_context_or_default():
    assert choose_speech_style().instruction == ''
    state = ReplyLanguageState()
    state.user_request('我很难过，需要求助，讲个情绪丰富的')
    state.first_text('turn')
    assert state.style_for('turn').emotion == 'serious'
    assert state.style_for('turn').delivery == 'natural'
    state.response_finished()
    state.first_text('next')
    assert state.style_for('next').delivery == 'contextual'


def test_contextual_delivery_is_the_runtime_default_without_an_asr_or_tool():
    state = ReplyLanguageState()
    state.first_text('greeting')
    style = state.style_for('greeting')
    assert style.delivery == 'contextual'
    assert style.source == 'runtime_context'
    assert '语义' in style.instruction
    assert style.emotion == 'neutral'
    assert (style.rate, style.pitch, style.volume) == (1., 1., 50)


def test_explicit_emotion_takes_precedence_over_optional_early_intent():
    state = ReplyLanguageState()
    state.user_request('用兴奋的语气说你好')
    state.first_text('turn')
    state.reply_intent(language='zh', emotion='sad')
    assert state.style_for('turn').emotion == 'excited'
    assert state.style_for('turn').source == 'explicit_user'


def test_input_sadness_is_reference_not_an_override_of_explicit_acting():
    state = ReplyLanguageState()
    state.observation = {'emotion': 'sad'}
    state.user_request('用兴奋的语气说你好')
    state.first_text('turn')
    assert state.style_for('turn').emotion == 'excited'


@pytest.mark.parametrize('utterance,expected', [
    ('用不开心的语气说一段', 'sad'), ('用不高兴的语气说你好', 'sad'),
    ('「讲个情绪丰富的」这句话是什么意思', None),
])
def test_negated_or_quoted_style_is_not_positive_excitement(utterance, expected):
    assert explicit_style_request(utterance).get('emotion') == expected
    if expected is None:
        assert explicit_style_request(utterance) == {}


def test_request_not_to_act_explicitly_selects_natural_delivery():
    assert explicit_style_request('不要表演，正常播报') == {'delivery': 'natural'}


def test_early_emotion_is_not_replayed_on_unrelated_next_turn():
    state = ReplyLanguageState()
    state.first_text('one')
    state.reply_intent(language='zh', emotion='sad')
    state.response_finished()
    state.confirm(state.revision)
    state.user_request('你好，今天天气真好')
    state.first_text('two')
    assert state.style_for('two').emotion == 'neutral'


def test_late_frozen_turn_emotion_is_consumed_by_only_the_next_turn():
    state = ReplyLanguageState()
    state.first_text('one')
    state.locked_turns.add('one')
    state.reply_intent(language='zh', emotion='sad')
    assert state.style_for('one').emotion == 'neutral'
    state.response_finished()
    state.confirm(state.revision)
    state.first_text('two')
    assert state.style_for('two').emotion == 'sad'
    state.response_finished()
    state.confirm(state.revision)
    state.first_text('three')
    assert state.style_for('three').emotion == 'neutral'


def test_runtime_default_reaches_actual_run_task_with_sidecar_off(tmp_path):
    async def run():
        runtime = HarborRuntime([RobotConfig('a', 'r')], tmp_path/'runtime.db',
                                realtime_key='fixture', realtime_socket_factory=FakeSocket)
        await runtime.start()
        c = runtime.realtime['a']
        output = SpeechOutput(c.robot, FakePublisher(), url='wss://example.test/tts',
                              key='fixture', voice='private', socket_factory=FakeTts,
                              style_provider=c.turn_speech_style, event=c.publish)
        c.speech = output
        try:
            await ready(runtime)
            for user, reply, dialect in [('你好', '你好呀，今天很高兴见到你！', 'unknown'),
                                          ('讲个四川话', '好嘛，我们慢慢摆一哈。', 'sichuan')]:
                result = await c.submit_text(user)
                turn = result['turn_id']
                await c.handle({'type': 'response.created', 'response': {'id': 'reply-'+turn}}, c.robot.generation)
                await c.handle({'type': 'response.text.delta', 'delta': reply}, c.robot.generation)
                await c.handle({'type': 'response.done', 'response': {
                    'id': 'reply-'+turn, 'status': 'completed', 'output': [],
                }}, c.robot.generation)
                await asyncio.wait_for(output.worker, 1)
                sent = [m for sock in FakeTts.instances for m in sock.sent
                        if m['header']['action'] == 'run-task'][-1]['payload']['parameters']
                proof = c._timeline_snapshot(turn)['tts']
                assert proof['speech_style']['delivery'] == 'contextual'
                assert proof['speech_style']['dialect'] == dialect
                assert proof['parameters']['instruction'] == sent['instruction']
                assert proof['submitted_text'] == reply
                assert not c.acoustic.snapshot()['enabled']
                assert proof['state'] == 'completed'
        finally:
            await output.close()
            await runtime.close()
    asyncio.run(run())


def test_sent_tts_events_contain_actual_text_and_sanitized_run_contract():
    async def run():
        events = []
        output = SpeechOutput(FakeRobot(), FakePublisher(), url='wss://example.test/tts',
                              key='secret', voice='private-voice', socket_factory=FakeTts,
                              style_provider=lambda *_: choose_speech_style(dialect='sichuan'))
        output.event = lambda kind, **data: events.append((kind, data))
        try:
            await output.feed('t', output.robot.session_state(), '我们慢慢摆一哈。')
            await output.finish('t')
            await asyncio.wait_for(output.worker, 1)
            sent = FakeTts.instances[-1].sent
            run_event = next(d for k, d in events if k == 'tts.run_task')
            assert run_event['parameters'] == {k: v for k, v in sent[0]['payload']['parameters'].items()
                                               if k != 'voice'}
            assert run_event['streaming'] == 'duplex'
            pieces = [d for k, d in events if k == 'tts.text_submitted']
            assert ''.join(d['text'] for d in pieces) == '我们慢慢摆一哈。'
            assert all(d['task_id'] == run_event['task_id'] for d in pieces)
            assert pieces[-1]['end_char'] == len('我们慢慢摆一哈。')
            assert 'private-voice' not in str(events) and 'secret' not in str(events)
        finally:
            await output.close()
    asyncio.run(run())


def test_actual_tts_contract_is_retained_per_turn_bounded_and_not_borrowed(tmp_path):
    async def run():
        runtime = HarborRuntime([RobotConfig('a', 'r')], tmp_path/'runtime.db',
                                realtime_key='fixture', realtime_socket_factory=FakeSocket)
        await runtime.start()
        try:
            await ready(runtime)
            c = runtime.realtime['a']
            c.publish('turn.started', turn_id='old', user_text='讲个四川话')
            c.publish('tts.run_task', turn_id='old', task_id='task-old', segment_id='old-1',
                      streaming='duplex', parameters={'voice': 'private', 'instruction': '四川话',
                                                       'rate': 1., 'format': 'pcm', 'sample_rate': 24000},
                      speech_style={'dialect': 'sichuan', 'emotion': 'neutral'})
            for i in range(70):
                c.publish('tts.text_submitted', turn_id='old', task_id='task-old', segment_id='old-1',
                          text='哈', chars=1, start_char=i, end_char=i+1)
            c.publish('turn.started', turn_id='new', user_text='另一轮')
            old = next(t for t in c.diagnostic_snapshot()['turn_timelines'] if t['turn_id'] == 'old')['tts']
            assert old['parameters']['instruction'] == '四川话'
            assert 'voice' not in old['parameters']
            assert old['submitted_text'] == '哈'*70
            assert len(old['segments']) == 64 and old['segments_omitted'] == 6
            assert old['segment_count'] == 70
            assert c._timeline_snapshot('new')['tts'] is None
            # A mismatched late callback cannot overwrite evidence in this identity.
            c.publish('tts.run_task', turn_id='old', generation=c.robot.generation-1,
                      task_id='wrong', parameters={'instruction': '错误'})
            assert c._timeline_snapshot('old')['tts']['task_id'] == 'task-old'
            # Snapshot consumers cannot mutate retained evidence.
            old['parameters']['instruction'] = 'consumer mutation'
            assert c._timeline_snapshot('old')['tts']['parameters']['instruction'] == '四川话'
        finally:
            await runtime.close()
    asyncio.run(run())


@pytest.mark.parametrize('utterance', [
    '别说得太兴奋，平静一点', '别兴奋地说',
    '不想用兴奋的语气说', '不能用兴奋的语气说',
])
def test_negative_emotion_commands_are_not_positive_acting_requests(utterance):
    assert explicit_style_request(utterance) == {}

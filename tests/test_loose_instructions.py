"""User decision 10-22: take instructions loosely but do not be led.

“我叫小明” alone registers a face, “用河南话说话” holds for the chat while
“用陕西话说一句话” is one reply, “别说河南话了” goes back, and whatever the phrase
rules cannot place goes to the decision model. Also the microphone gap at the
provider's routine 180 s idle close (missing words, same day).
"""

import asyncio
import json
import time

import pytest
from annie_adapters import VERIFIED_MIC_FORMAT
from annie_brain import persona
from annie_brain.decision_policy import face_request_question, language_request_question
from annie_core import RobotConfig
from annie_runtime import AudioInputGate, HarborRuntime
from annie_runtime.language import (
    ReplyLanguageState,
    explicit_language_request,
    explicit_language_stop,
    unplaced_language_mention,
)

from tests.test_decision_model import EchoSocket, FakeDecision
from tests.test_face_registration import FakeRegistry, say, see
from tests.test_visual_observations import ME, person

IDENTITY = {'robot_id': 'a', 'room_id': 'r', 'chat_id': 'c', 'persona_id': 'p',
            'generation': 1, 'session_epoch': 1}


@pytest.mark.parametrize(('text', 'expected'), [
    # The user's own three examples.
    ('用河南话说话', ('henan', 'chat')),
    ('用陕西话说一句话', ('shaanxi', 'turn')),
    ('以后都用粤语和我讲话', ('cantonese', 'robot')),
    # Only how to talk: the rest of this chat.
    ('小安妮，用河南话跟我说话吧', ('henan', 'chat')),
    ('说河南话', ('henan', 'chat')),
    ('跟我说陕西话', ('shaanxi', 'chat')),
    ('用四川话和我聊天', ('sichuan', 'chat')),
    ('你能不能说河南话', ('henan', 'chat')),
    ('换成粤语', ('cantonese', 'chat')),
    ('说回普通话', ('mandarin', 'chat')),
    # Something to say: this reply only.
    ('北京有什么好玩的，用粤语说', ('cantonese', 'turn')),
    ('用四川方言回答我', ('sichuan', 'turn')),
    ('四川话给我讲讲', ('sichuan', 'turn')),
    ('来段粤语', ('cantonese', 'turn')),
    ('这句换成普通话', ('mandarin', 'turn')),
    ('唱粤语', ('cantonese', 'turn')),
])
def test_how_long_a_language_request_holds(text, expected):
    parsed = explicit_language_request(text)
    assert (parsed['dialect'], parsed['scope']) == expected


def test_stop_phrases_and_what_the_decision_model_gets():
    assert explicit_language_stop('别说河南话了') == ('zh', 'henan')
    assert explicit_language_stop('不要用粤语了') == ('yue', 'cantonese')
    assert explicit_language_stop('别用方言了') == ('zh', '*')
    assert explicit_language_stop('你不用说普通话') is None
    assert explicit_language_stop('别翻译“说粤语”') is None
    # Unplaced mentions go to the decision model; settled ones never do.
    assert unplaced_language_mention('粤语聊天吧') == ('yue', 'cantonese')
    assert unplaced_language_mention('整点河南话') == ('zh', 'henan')
    for settled in ('用河南话说话', '你会说粤语吗', '别说河南话了', '粤语怎么说谢谢', '今天天气不错'):
        assert unplaced_language_mention(settled) is None
    question = language_request_question('粤语')
    assert set(question['criteria']) == {'this_reply', 'from_now', 'no'}
    assert set(face_request_question('小明')['criteria']) == {'yes', 'other', 'no'}


def test_stop_returns_to_mandarin_only_when_that_language_is_in_force():
    s = ReplyLanguageState()
    s.reset(IDENTITY)
    s.user_request('别说河南话了')  # nothing to stop
    assert s.explicit is None
    s.load_preferences({'chat': {'language': 'zh', 'dialect': 'henan'}, 'robot': None})
    assert s.selection()['dialect'] == 'henan'
    assert s.user_request('别说河南话了')
    assert (s.selection()['dialect'], s.selection()['source']) == ('mandarin', 'explicit_user')
    s.response_finished()
    s.load_preferences({'chat': None, 'robot': {'language': 'yue', 'dialect': 'cantonese'}})
    s.user_request('别用方言了')
    assert s.selection()['dialect'] == 'mandarin'


def test_chat_scoped_request_tells_the_model_it_is_saved_for_this_chat():
    s = ReplyLanguageState()
    s.reset(IDENTITY)
    s.user_request('用河南话说话')
    assert '这次聊天一直用河南话，已替你保存' in s.instruction_hint()
    s.response_finished()
    s.user_request('以后都用粤语和我讲话')
    assert '以后一直用粤语' in s.instruction_hint()


def test_persona_follows_requests_but_keeps_who_she_is():
    text, _ = persona.session_instructions('annie', {}, {})
    assert persona.USER_REQUESTS in text
    assert '拿不准按这次聊天都这样办' in text and '不再当小安妮' in text


# ---- runtime ------------------------------------------------------------------------
def live(tmp_path, body, *, decision=None, registry=None, socket_factory=EchoSocket):
    async def run():
        rt = HarborRuntime([RobotConfig('a', room_id='roomv2')], tmp_path / 'rt.db',
                           realtime_key='fake', realtime_socket_factory=socket_factory,
                           decision_client=decision,
                           face_registries={'a': registry} if registry else None)
        if registry:
            rt.faces.poll_seconds = .01
        await rt.start()
        client = rt.realtime['a']
        try:
            for _ in range(200):
                if client.ready:
                    break
                await asyncio.sleep(.01)
            await body(rt, client)
        finally:
            await rt.close()
    asyncio.run(run())


async def until(check, steps=200):
    for _ in range(steps):
        if check():
            return True
        await asyncio.sleep(.005)
    return False


def saved(rt, client):
    return rt.store.load_language_preferences('a', client.robot.chat_id)


def test_spoken_requests_save_and_stop_by_scope(tmp_path):
    async def body(rt, client):
        await client.submit_text('用河南话说话')
        assert await until(lambda: client.language.preferences['chat'])
        assert saved(rt, client)['chat']['dialect'] == 'henan'
        await client.cancel()
        await client.submit_text('用陕西话说一句话')  # one reply: the setting stays
        assert client.language.selection()['dialect'] == 'shaanxi'
        await asyncio.sleep(.02)
        assert saved(rt, client)['chat']['dialect'] == 'henan'
        await client.cancel()
        await client.submit_text('别说河南话了')
        assert client.language.selection()['dialect'] == 'mandarin'
        assert await until(lambda: not client.language.preferences['chat'])
        await client.cancel()
        # A robot-wide setting stays for other chats; this chat goes back to Mandarin.
        await client.submit_text('以后都用粤语和我讲话')
        assert await until(lambda: client.language.preferences['robot'])
        await client.cancel()
        await client.submit_text('别说粤语了')
        assert await until(lambda: client.language.preferences['chat'])
        prefs = saved(rt, client)
        assert (prefs['chat']['dialect'], prefs['robot']['dialect']) == ('mandarin', 'cantonese')
        await client.cancel()
        await client.submit_text('以后都别说粤语了')
        assert await until(lambda: not client.language.preferences['robot'])

    live(tmp_path, body)


def test_unplaced_language_request_goes_to_the_decision_model(tmp_path):
    decision = FakeDecision({'choice': 'from_now', 'confidence': .9},
                            {'choice': 'no', 'confidence': .8})

    async def body(rt, client):
        await client.submit_text('粤语聊天吧')
        assert await until(lambda: client.language.preferences['chat'])
        state, questions = decision.calls[0]
        assert state['当前这句话'] == '粤语聊天吧' and '粤语' in questions['intent']['instructions']
        assert saved(rt, client)['chat']['dialect'] == 'cantonese'
        await client.cancel()
        await client.submit_text('你觉得河南话好听吗')  # the model says no: nothing changes
        await until(lambda: len(decision.calls) == 2)
        await asyncio.sleep(.02)
        assert saved(rt, client)['chat']['dialect'] == 'cantonese'
        assert client.decision_stats['instruction_checks'] == 2

    live(tmp_path, body, decision=decision)


async def style(client, text, dialect, call_id):
    if client.turn_id:
        client._finish('completed')
    client._create_turn(text)
    await client._tool_call({'call_id': call_id, 'name': 'select_reply_style', 'arguments': json.dumps(
        {'language': 'zh', 'dialect': dialect, 'emotion': 'playful'})})
    return next(e for e in client.events if e['kind'] == 'tool.result' and e['call_id'] == call_id)


def test_the_models_own_dialect_choice_is_judged_not_dropped(tmp_path):
    decision = FakeDecision({'choice': 'this_reply', 'confidence': .7},
                            {'choice': 'no', 'confidence': .9})

    async def body(rt, client):
        # “整点河南话” matches no phrase rule; the decision model says “this reply”.
        accepted = await style(client, '整点河南话', 'henan', 's1')
        assert accepted['valid'] is True and client.language.intent['dialect'] == 'henan'
        rejected = await style(client, '河南话你觉得难学吗', 'henan', 's2')
        assert rejected['valid'] is False
        unnamed = await style(client, '今天好开心', 'sichuan', 's3')  # never named: no question
        assert unnamed['valid'] is False and len(decision.calls) == 2

    live(tmp_path, body, decision=decision)


def test_without_a_decision_model_a_named_dialect_is_trusted_for_one_reply(tmp_path):
    async def body(rt, client):
        accepted = await style(client, '整点河南话', 'henan', 's1')
        assert accepted['valid'] is True
        await asyncio.sleep(.02)
        assert saved(rt, client)['chat'] is None

    live(tmp_path, body)


def test_any_self_introduction_registers_and_the_rest_is_judged(tmp_path):
    registry = FakeRegistry()
    decision = FakeDecision({'choice': 'other', 'confidence': .8},
                            {'choice': 'yes', 'confidence': .9})

    async def body(rt, client):
        see(rt, [person('person_5')])
        intro = await say(client, '我是小明', 'f1', 'remember_face', {'name': '小明'})
        assert intro['status'] == 'started' and decision.calls == []
        assert await until(lambda: 'a' not in rt.faces.active)
        friend = await say(client, '我是张三的朋友', 'f2', 'remember_face', {'name': '张三'})
        assert friend['status'] == 'rejected'
        assert decision.calls[-1][0]['当前这句话'] == '我是张三的朋友'
        client._last_reply_text = '你叫什么名字呀？'
        answer = await say(client, '王五', 'f3', 'remember_face', {'name': '王五'})
        assert answer['status'] == 'started'
        assert decision.calls[-1][0]['机器人上一句'] == '你叫什么名字呀？'
        assert [call[1] for call in registry.calls] == ['小明', '王五']

    live(tmp_path, body, decision=decision, registry=registry)


def test_a_face_known_by_another_name_is_asked_about_once(tmp_path):
    registry = FakeRegistry(people=('张三',))

    async def body(rt, client):
        see(rt, [ME])  # 张三, looking at her
        first = await say(client, '我叫李四', 'k1', 'remember_face', {'name': '李四'})
        assert first['status'] == 'known_as_other' and first['known_as'] == '张三'
        assert registry.calls == []
        again = await say(client, '对，我就叫李四，记住我', 'k2', 'remember_face', {'name': '李四'})
        assert again['status'] == 'started' and registry.calls == [('register', '李四', 18)]

    live(tmp_path, body, registry=registry)


# ---- missing words: the provider's idle close ------------------------------------------
class IdleClosingSocket(EchoSocket):
    opened = []

    async def open(self):
        IdleClosingSocket.opened.append(time.monotonic())
        return self


def test_routine_idle_close_reconnects_at_once_and_keeps_queued_audio(tmp_path):
    IdleClosingSocket.opened = []

    async def body(rt, client):
        audio = client.robot.audio
        cleared = []
        original = audio._clear
        audio._clear = lambda: cleared.append(1) or original()
        closed_at = time.monotonic()
        await client.socket.inbound.put({'type': 'error', 'error': {
            'code': 'response_idle_timeout',
            'message': 'Your session was closed because no response was generated for 180 seconds.'}})
        assert await until(lambda: len(IdleClosingSocket.opened) == 2 and client.ready)
        # Before: a 1 s backoff on top of the handshake, and the queue emptied.
        assert IdleClosingSocket.opened[1] - closed_at < .5 and cleared == []
        await client.socket.inbound.put({'type': 'error', 'error': {'code': 'internal_error',
                                                                     'message': 'x'}})
        assert await until(lambda: cleared == [1])

    live(tmp_path, body, socket_factory=IdleClosingSocket)


def test_lost_input_frames_are_recorded_with_their_time():
    gate = AudioInputGate(RobotConfig('a', room_id='r', listen_enabled=True, audio_queue_capacity=2))
    gate.start_stream('mic', VERIFIED_MIC_FORMAT)
    began = time.monotonic()
    now = time.monotonic()
    for _ in range(3):
        gate.put('mic', bytes(640), sequence=None, received_at=now)
    assert gate.lost_since(began) == 1  # the oldest frame was pushed out
    with pytest.raises(Exception):
        gate.put('mic', bytes(640), sequence=None, received_at=now - 5)
    assert gate.lost_since(began) == 2
    gate.reset_source('mic')
    assert gate.lost_since(began) == 4 and gate.lost_since(time.monotonic() + 1) == 0

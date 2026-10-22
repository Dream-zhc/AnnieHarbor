"""Typed language evidence is not an acoustic observation or a saved preference."""

import pytest
from annie_runtime.language import ReplyLanguageState, explicit_language_request

IDENTITY = dict(robot_id='a', room_id='r', chat_id='c', persona_id='p', generation=1,
                session_epoch=1)


def state():
    value = ReplyLanguageState()
    value.reset(IDENTITY)
    return value


@pytest.mark.parametrize('text', [
    'Have an iced drink at a small shop.', 'What would you like to drink?',
    "I'm happy to see you!", 'hello', 'no', 'Thanks!',
])
def test_typed_english_updates_hint_and_confirmed_tts_selection(text):
    s = state()
    assert s.user_request(text, input_source='typed')
    assert s.selection()['language'] == 'en'
    assert s.selection()['source'] == 'typed_text'
    assert '目标语种=en' in s.instruction_hint()
    assert s.snapshot()['input'] is None
    assert s.snapshot()['typed_input']['evidence_level'] == 'typed_text_heuristic'
    s.confirm(s.revision)
    s.first_text('turn')
    assert s.style_for('turn').language == 'en'
    assert s.style_for('turn').source == 'typed_text'


def test_continuous_typed_english_remains_english_without_persisting():
    s = state()
    for i, text in enumerate(['Have an iced drink at a small shop.', 'no', 'hello']):
        s.user_request(text, input_source='typed')
        s.confirm(s.revision)
        s.first_text(str(i))
        assert s.style_for(str(i)).language == 'en'
        s.response_finished()
    assert s.preferences == {'robot': None, 'chat': None}
    s.user_request('还是喝热茶吧', input_source='typed')
    s.confirm(s.revision)
    s.first_text('zh')
    assert s.style_for('zh').language == 'zh'


@pytest.mark.parametrize('text', [
    'https://example.com/have/an/iced/drink', 'Paris', 'OpenAI',
    '`Have an iced drink at a small shop.`', '"Have an iced drink at a small shop."',
    'print("hello world")', 'const drink = "iced tea";',
    '请翻译 Have an iced drink at a small shop.', '这句话 hello 是什么意思',
    'SELECT * FROM drinks', 'Bonjour tout le monde',
])
def test_urls_code_names_quotes_and_other_language_are_not_english_evidence(text):
    s = state()
    s.user_request(text, input_source='typed')
    assert s.selection()['language'] == 'zh'
    assert s.snapshot()['typed_input'] is None


@pytest.mark.parametrize('scope', ['chat', 'robot'])
def test_saved_explicit_preferences_override_typed_observation(scope):
    s = state()
    s.load_preferences({scope: {'language': 'zh', 'dialect': 'sichuan'}})
    s.user_request('Have an iced drink at a small shop.', input_source='typed')
    assert s.selection()['source'] == scope + '_preference'
    assert s.selection()['dialect'] == 'sichuan'
    s.user_request('这句请用英语', input_source='typed')
    assert s.selection()['source'] == 'explicit_user'
    assert s.selection()['language'] == 'en'


def test_asr_transcript_never_masquerades_as_typed_language_evidence():
    s = state()
    s.user_request('Have an iced drink at a small shop.')
    assert s.snapshot()['typed_input'] is None
    assert s.snapshot()['asr_text_input']['source'] == 'asr_text_heuristic'
    assert s.selection()['language'] == 'en'
    assert s.selection()['source'] == 'asr_text_heuristic'
    s.user_request('今天天气怎么样')
    assert s.selection()['language'] == 'zh'
    assert s.snapshot()['asr_text_input']['evidence_level'] == 'text_heuristic'
    assert s.snapshot()['input'] is None


def test_unconfirmed_typed_language_keeps_known_target_without_claiming_prompt_application():
    s = state()
    s.user_request('hello', input_source='typed')
    s.first_text('turn')
    assert s.style_for('turn').language == 'en'
    assert s.application == 'unknown'
    assert s.snapshot()['llm_actual_text_language'] == 'unknown'


def test_asr_next_input_and_identity_reset_retire_typed_evidence():
    s = state()
    s.user_request('hello', input_source='typed')
    s.user_request('你好')
    assert s.snapshot()['typed_input'] is None
    s.user_request('hello', input_source='typed')
    s.reset({**IDENTITY, 'generation': 2})
    assert s.snapshot()['typed_input'] is None
    assert s.selection()['language'] == 'zh'


@pytest.mark.parametrize(('text', 'expected'), [
    ('Please speak English in this chat', ('en', 'chat')),
    ('Reply in English from now on', ('en', 'robot')),
    ('Speak Mandarin', ('zh', 'turn')),
    ('Use Cantonese in this conversation', ('yue', 'chat')),
    ('Translate “Please speak English”', None),
    ("Don't speak English", None),
    ('Can you speak English?', None),
])
def test_explicit_english_instruction_not_confused_with_questions(text, expected):
    choice = explicit_language_request(text)
    assert ((choice['language'], choice['scope']) if choice else None) == expected


def test_actual_assistant_text_is_heuristic_and_never_claims_audio_evidence():
    s = state()
    s.user_request('hello', input_source='typed')
    s.confirm(s.revision)
    s.first_text('turn')
    s.response_text_done('你好，我是小安妮。')
    snapshot = s.snapshot()
    assert snapshot['target']['language'] == 'en'
    assert snapshot['llm_actual_text_language'] == 'zh'
    assert snapshot['tts_audibly_verified_language'] == 'unknown'
    s.response_finished()
    s.response_started()
    assert s.snapshot()['llm_actual_text_language'] == 'unknown'


@pytest.mark.parametrize(('text', 'expected'), [
    ('你用正宗的陕西方言讲个', ('zh', 'shaanxi', 'turn')),
    ('请用地道的河南方言讲个故事', ('zh', 'henan', 'turn')),
    ('用四川方言回答我', ('zh', 'sichuan', 'turn')),
    ('以后用正宗的陕西方言说话', ('zh', 'shaanxi', 'robot')),
    ('这个会话用陕西方言', ('zh', 'shaanxi', 'chat')),
    ('不要用陕西方言说话', None),
    ('你知道陕西方言吗？', None),
    ('我去陕西旅游过', None),
    # Spoken order without 用 (10-22 live test).
    ('陕西话介绍一下张海超。', ('zh', 'shaanxi', 'turn')),
    ('陕西话介绍一下自己', ('zh', 'shaanxi', 'turn')),
    ('四川话给我讲个笑话', ('zh', 'sichuan', 'turn')),
    ('陕西话怎么说吃饭', None),
    ('陕西话里吃饭叫什么', None),
])
def test_explicit_dialect_requests_accept_natural_modifiers_without_location_guess(text, expected):
    parsed = explicit_language_request(text)
    assert ((parsed['language'], parsed['dialect'], parsed['scope']) if parsed else None) == expected


@pytest.mark.parametrize(('text', 'expected'), [
    ('我想听你用俄语讲个故事', ('ru', 'unknown')),
    ('来段粤语', ('yue', 'cantonese')),
    ('唱首粤语歌给我听', ('yue', 'cantonese')),
    ('说两句日语', ('ja', 'unknown')),
    ('切成英文', ('en', 'unknown')),
    ('能不能用粤语说', ('yue', 'cantonese')),
    ('要不要用英语聊聊', ('en', 'unknown')),
    ('俄语来一段', ('ru', 'unknown')),
    ('四川话给我讲讲', ('zh', 'sichuan')),
    ('用意大利语打个招呼', ('it', 'unknown')),
    ('讲个西班牙语笑话', ('es', 'unknown')),
    ('用纯正的西安话说说', ('zh', 'shaanxi')),
    ('香港话来一句', ('yue', 'cantonese')),
    ('用越南语介绍一下你自己', ('vi', 'unknown')),
    ('Tell me a story in Russian', ('ru', 'unknown')),
    ('原来英语这么难', None),
    ('你英语说得真好', None),
    ('聊聊英语学习', None),
    ('中文讲究对仗', None),
    ('别说俄语了', None),
    ('你会说俄语吗', None),
    ('俄语怎么说你好', None),
    ('用俄语怎么说谢谢', None),
    ('我在读英语专业', None),
    ('英语来自拉丁语吗', None),
    ('说白话一点', None),
    ('西语和葡语很像', None),
])
def test_loose_language_requests_without_ordinary_talk(text, expected):
    # User decision 10-22: any natural way of asking for a language counts.
    parsed = explicit_language_request(text)
    assert ((parsed['language'], parsed['dialect']) if parsed else None) == expected


def test_saved_language_outranks_chinese_input_and_model_guess():
    s = state()
    s.load_preferences({'chat': {'language': 'ru', 'dialect': 'unknown'}, 'robot': None})
    s.user_request('北京天气怎么样')  # Chinese ASR final
    s.reply_intent(language='zh', dialect='unknown', emotion='warm', transcript='北京天气怎么样')
    assert (s.selection()['language'], s.selection()['source']) == ('ru', 'chat_preference')
    assert s.selection()['emotion'] == 'warm'
    assert '本会话语言设置' in s.instruction_hint()
    assert '全程用俄语回答' in s.instruction_hint()  # a name, not just “ru”
    assert '整句是外语时仍用该语言回答' not in s.instruction_hint()
    s.first_text('t')
    assert s.style_for('t').language == 'ru'
    s.user_request('这句换成普通话')  # this turn's explicit order still wins
    assert s.selection()['dialect'] == 'mandarin'


@pytest.mark.parametrize(('text', 'expected'), [
    ('取消这个会话的俄语设置', 'chat'), ('恢复默认语言', 'any'),
    ('清除机器人的语言设置', 'robot'), ('取消提醒', None), ('不要清除语言设置', None),
])
def test_spoken_language_clear(text, expected):
    from annie_runtime.language import explicit_language_clear
    assert explicit_language_clear(text) == expected


def test_every_named_language_is_speakable():
    from annie_core.speech_style import LANGUAGES as SPEAKABLE
    from annie_runtime.language import NAMES, validate_language
    for language, dialect in NAMES.values():
        assert language in SPEAKABLE
        validate_language(language, dialect)


def test_explicit_dialect_request_needs_no_inference_evidence():
    language_state = state()
    language_state.reply_intent(language='zh', dialect='shaanxi', emotion='playful',
                                transcript='陕西话介绍一下自己。')
    assert language_state.intent['dialect'] == 'shaanxi'
    with pytest.raises(ValueError):
        language_state.reply_intent(language='zh', dialect='henan', emotion='neutral',
                                    transcript='陕西话介绍一下自己。')
    spoken = state()
    spoken.user_request('陕西话介绍一下张海超。')  # ASR final, not typed
    spoken.first_text('turn')
    assert spoken.style_for('turn').dialect == 'shaanxi'
    assert '陕西话' in spoken.style_for('turn').instruction


def test_dialect_request_reaches_tts_style_before_first_text():
    language_state = state()
    language_state.user_request('你用正宗的陕西方言讲个', input_source='typed')
    assert language_state.selection()['dialect'] == 'shaanxi'
    language_state.confirm(language_state.revision)
    language_state.first_text('turn')
    style = language_state.style_for('turn')
    assert style.dialect == 'shaanxi'
    assert '陕西话' in style.instruction

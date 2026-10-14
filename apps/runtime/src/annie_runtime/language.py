"""Bounded per-session language policy; observations never write persistent preferences."""

import re
import time
from collections import OrderedDict

LANGUAGES = frozenset('zh yue en ja de ko ru fr pt ar it es hi id th tr uk vi'.split())
DIALECTS = frozenset('unknown mandarin sichuan henan shaanxi cantonese'.split())
NAMES = {'普通话': ('zh', 'mandarin'), '中文': ('zh', 'unknown'),
         '四川话': ('zh', 'sichuan'), '河南话': ('zh', 'henan'),
         '陕西话': ('zh', 'shaanxi'), '四川方言': ('zh', 'sichuan'),
         '河南方言': ('zh', 'henan'), '陕西方言': ('zh', 'shaanxi'),
         '粤语': ('yue', 'cantonese'),
         '广东话': ('yue', 'cantonese'), '英语': ('en', 'unknown'),
         '英文': ('en', 'unknown'), '日语': ('ja', 'unknown'),
         '韩语': ('ko', 'unknown'), '法语': ('fr', 'unknown'),
         '德语': ('de', 'unknown'), '西班牙语': ('es', 'unknown')}
IDENTITY_KEYS = ('robot_id', 'room_id', 'chat_id', 'persona_id', 'generation', 'session_epoch')


def typed_text_language(text):
    """Conservative text evidence, not ASR or a general language classifier."""
    if not isinstance(text, str):
        return None
    text = text.strip()
    # Ignore quoted material, code, addresses and mixed-language explanations.
    # Internal English apostrophes (I'm / don't) are ordinary prose.
    prose = re.sub(r"(?<=[A-Za-z])['’](?=[A-Za-z])", '', text)
    if (not prose or re.search(r'[^A-Za-z0-9\s,.!?-]', prose)
            or re.search(r'\b(?:https?|www|SELECT|INSERT|UPDATE|DELETE|const|let|var|import|def)\b',
                         prose, re.IGNORECASE)):
        return None
    words = re.findall(r'[A-Za-z]+', prose.lower())
    short = {'hello', 'hi', 'hey', 'no', 'yes', 'okay', 'ok', 'thanks', 'thank you',
             'good morning', 'good afternoon', 'good evening', 'goodbye'}
    grammar = {'i', 'im', 'you', 'we', 'they', 'he', 'she', 'it', 'a', 'an', 'the',
               'is', 'are', 'was', 'have', 'has', 'would', 'can', 'do', 'dont',
               'what', 'where', 'when', 'how', 'why', 'to', 'at', 'of', 'with', 'my'}
    if (' '.join(words) in short or len(words) >= 3 and len(set(words) & grammar) >= 2):
        return 'en'
    return None


def prose_language_hint(text):
    """Script/grammar hint only; never present it as acoustic language proof."""
    detected = typed_text_language(text)
    if not detected and isinstance(text, str):
        han = len(re.findall(r'[\u4e00-\u9fff]', text))
        latin = len(re.findall(r'[A-Za-z]', text))
        if han >= 2 and han > latin:
            detected = 'zh'
    return detected


# Words only a dialect speaker says in conversation; one is enough. They are
# lexical evidence from the ASR text, labelled as such, never an acoustic
# label. Ambiguous ones are left out: 要得(到), 安逸, 晓得, 俺, 得劲, 额(=嗯).
DIALECT_MARKERS = (
    ('yue', 'cantonese', re.compile(
        r'[哋嘅咗嚟冇喺佢啱睇嘢]|唔(?:係|好|該|该|知|使|系)|乜嘢|點樣|係咪|系咪|傾偈'
        r'|(?<![一有])点样(?![子品本式])')),  # Qwen ASR writes Cantonese in simplified Mandarin
    ('zh', 'sichuan', re.compile(
        r'巴适|要得不|咋个|啥子|莫得|搞快|屋头|娃儿|瓜娃子|得行|撒子|耍嘛|好耍')),
    ('zh', 'henan', re.compile(r'中不中|弄啥嘞|弄啥咧|可中|恁(?!地)|夜儿个|美得很嘞')),
    ('zh', 'shaanxi', re.compile(r'嫽|咥|谝闲传|么麻达|美滴很|哈怂|额们|额滴')),
)
# A dialect heard by its words keeps being followed this long after the last one.
DIALECT_FOLLOW_SECONDS = 120


def dialect_marker(text):
    """(language, dialect, word) for the first dialect-only word, else None."""
    if not isinstance(text, str) or re.search(r'[“”‘’「」"\']|翻译|引用', text):
        return None
    for language, dialect, pattern in DIALECT_MARKERS:
        match = pattern.search(text)
        if match:
            return language, dialect, match.group(0)
    return None


def validate_language(language, dialect):
    if language not in LANGUAGES or dialect not in DIALECTS:
        raise ValueError('invalid language or dialect')
    if (dialect in {'mandarin', 'sichuan', 'henan', 'shaanxi'} and language != 'zh'
            or dialect == 'cantonese' and language != 'yue'):
        raise ValueError('language and dialect mismatch')


def explicit_language_request(text):
    """Conservative current-user command matcher, not a location/dialect classifier."""
    if not isinstance(text, str) or re.search(
        r'不要|别用|不用|不能|不想|会说.*[吗么?？]|翻译|引用|[“”‘’「」"\']', text,
    ):
        return None
    english = re.search(
        r'\b(?:please\s+)?(?:speak|talk|reply|respond|answer|switch\s+to|use)'
        r'\s+(?:to\s+me\s+)?(?:in\s+)?(english|chinese|mandarin|cantonese|japanese)\b',
        text, re.IGNORECASE,
    )
    if english and not re.search(r'\b(?:don.t|do\s+not|never|stop|translate|quote|what|how|can\s+you|could\s+you)\b',
                                 text, re.IGNORECASE):
        names = {'english': ('en', 'unknown'), 'chinese': ('zh', 'unknown'),
                 'mandarin': ('zh', 'mandarin'), 'cantonese': ('yue', 'cantonese'),
                 'japanese': ('ja', 'unknown')}
        language, dialect = names[english[1].lower()]
        scope = ('chat' if re.search(r'\b(?:this|current)\s+(?:chat|conversation)\b', text, re.IGNORECASE)
                 else 'robot' if re.search(r'\b(?:from\s+now\s+on|always|in\s+the\s+future)\b', text, re.IGNORECASE)
                 else 'turn')
        return dict(language=language, dialect=dialect, scope=scope)
    match = re.search(r'(?:用|说|讲|改成|换成|切换到)\s*'
                      r'(?:(?:正宗|地道|自然|标准)的)?\s*'
                      r'(?:个|一个|一段|两句)?\s*('
                      + '|'.join(NAMES) + ')', text)
    if not match:
        return None
    language, dialect = NAMES[match[1]]
    scope = ('chat' if re.search(r'(?i)chat|本会话|这个会话|当前会话', text)
             else 'robot' if re.search(r'以后|今后|一直|所有会话|全局|这台机器人', text)
             else 'turn')
    return dict(language=language, dialect=dialect, scope=scope)


def explicit_style_request(text):
    style = {}
    if re.search(r'引用|翻译|[“”‘’「」"\']', text):
        return style
    if re.fullmatch(r'(?:请)?(?:不要|不用)表演(?:[，, ]*(?:正常|平直|平淡)(?:地)?播报)?[。！! ]*', text):
        return {'delivery': 'natural'}
    if re.search(r'不要|别|不用|不能|不想', text):
        return style
    if re.search(r'(?:讲|说|语气|声音|表达).{0,12}(?:情绪丰富|有感情|抑扬顿挫)', text):
        style['delivery'] = 'expressive'
    elif re.search(r'(?:用|请|语气).{0,8}(?:平直|平淡|无感情|不要表演|正常播报)', text):
        style['delivery'] = 'natural'
    if re.search(r'(?:说|讲|语速|速度).{0,5}(?:慢|放慢)|慢一点', text):
        style['rate'] = .92
    elif re.search(r'(?:说|讲|语速|速度).{0,5}(?:快|加快)|快一点', text):
        style['rate'] = 1.10
    for token, emotion in [('温柔', 'warm'), ('安抚', 'calm'), ('平静', 'calm'),
                           ('严肃', 'serious'), ('调皮', 'playful'), ('俏皮', 'playful'),
                           ('兴奋', 'excited'), ('开心', 'excited'), ('高兴', 'excited'),
                           ('悲伤', 'sad'), ('伤心', 'sad'), ('嘲讽', 'sarcastic'),
                           ('反讽', 'sarcastic'), ('好奇', 'curious'),
                           ('不开心', 'sad'), ('不高兴', 'sad')]:
        guarded = r'(?<!不)' + token
        if re.search(r'(?:用|语气|说得|一点).{0,6}' + guarded + '|'
                     + guarded + r'.{0,6}(?:说|讲|语气)', text):
            style['emotion'] = emotion
    return style


class ReplyLanguageState:
    def __init__(self):
        self.preferences = {'robot': None, 'chat': None}
        self.identity = {}
        self.revision = 0
        self.confirmed_revision = -1
        self.styles = OrderedDict()
        self.reset({})

    def reset(self, identity):
        self.identity = dict(identity)
        self.observation = None
        self.typed_observation = None
        self.asr_text_observation = None
        self.asr_dialect = None
        self.candidate = None
        self.first_pending = False
        self.last_candidate = None
        self.explicit = None
        self.style_request = {}
        self.intent = None
        self._intent_emotion_pending = False
        self.response = None
        self.application = 'unknown'
        self.last_decision = None
        self.actual_text_language = 'unknown'
        self.styles.clear()
        self.locked_turns = set()
        self.active_turn_id = None
        self.serious_context = False
        self.last_activity = time.monotonic()
        self.confirmed_revision = -1
        self.seen_candidates = set()
        self._last_hint = self.instruction_hint()
        self.revision += 1

    def _refresh_revision(self):
        hint = self.instruction_hint()
        if hint != self._last_hint:
            self._last_hint = hint
            self.revision += 1

    def load_preferences(self, preferences):
        self.preferences = {key: preferences.get(key) for key in ('robot', 'chat')}
        self._refresh_revision()

    def default_selection(self):
        for scope in ('chat', 'robot'):
            row = self.preferences[scope]
            if row:
                return dict(language=row['language'], dialect=row['dialect'], source=f'{scope}_preference')
        return dict(language='zh', dialect='unknown', source='runtime_default')

    def selection(self):
        if self.explicit:
            return {**self.explicit, 'source': 'explicit_user'}
        if self.typed_observation:
            # Typed natural language follows the current user unless an explicit
            # saved preference already answers which language should be used.
            if any(self.preferences.values()):
                return self.default_selection()
            return dict(language=self.typed_observation['language'], dialect='unknown',
                        source='typed_text')
        if self.candidate and self.first_pending:
            return dict(self.candidate)
        if self.intent:
            return dict(self.intent)
        if self._dialect_followed() and not any(self.preferences.values()):
            # Saved preferences keep their one-reply first follow only.
            return dict(language=self.asr_dialect['language'], dialect=self.asr_dialect['dialect'],
                        source='asr_lexicon')
        if self.asr_text_observation and not any(self.preferences.values()) and not self.candidate:
            return dict(language=self.asr_text_observation['language'], dialect='unknown',
                        source='asr_text_heuristic')
        return self.default_selection()

    def observe(self, observation):
        now = time.monotonic()*1000
        if any(observation.get(k) != self.identity.get(k) for k in IDENTITY_KEYS):
            return False
        stamp = observation.get('observed_monotonic_ms')
        text = observation.get('text', '').strip()
        if (not isinstance(stamp, (float, int)) or not 0 <= now-stamp <= 3000
                or not text or re.fullmatch(r'[\s嗯啊哦呃唔,.，。!?！？]+', text)
                or observation.get('language') not in LANGUAGES):
            return False
        if time.monotonic()-self.last_activity > 180:
            self.candidate = self.intent = None
            self.last_candidate = None
            self.first_pending = False
            self.seen_candidates.clear()
        self.last_activity = time.monotonic()
        self.observation = {**observation, 'dialect': 'unknown', 'dialect_candidate': None,
                            'dialect_confidence': None, 'evidence_level': 'asr_language'}
        # An ASR zh label cannot identify a province. Only observed language is followed here.
        candidate = dict(language=observation['language'], dialect='unknown', source='asr_language')
        pair = (candidate['language'], candidate['dialect'])
        # A coarse zh result must not repeatedly erase an inferred province dialect.
        if not (self.candidate and self.candidate['language'] == candidate['language']
                and self.candidate['dialect'] != 'unknown'):
            if pair != self.last_candidate:
                self.candidate = candidate
                self.last_candidate = pair
                self.first_pending = pair not in self.seen_candidates
        self._refresh_revision()
        if self.response is not None:
            self.application = 'next_turn'
        return True

    def user_request(self, text, *, input_source='asr_transcript'):
        request = explicit_language_request(text)
        style = explicit_style_request(text)
        previous_hint = self.instruction_hint()
        language = typed_text_language(text) if input_source == 'typed' else None
        self.typed_observation = ({'language': language, 'dialect': 'unknown',
                                   'source': 'typed_text', 'evidence_level': 'typed_text_heuristic'}
                                  if language else None)
        if input_source == 'asr_transcript':
            hint = prose_language_hint(text)
            self.asr_text_observation = ({'language': hint, 'dialect': 'unknown',
                                          'source': 'asr_text_heuristic',
                                          'evidence_level': 'text_heuristic'} if hint else None)
            self._hear_dialect(text, request)
        else:
            self.asr_text_observation = None
        # Stable prefixes can be extended by negation/correction. Re-evaluate the
        # whole authoritative prefix/final instead of keeping an earlier command.
        self.explicit = request
        self.style_request = style
        # Distinguish a sad acting instruction from a person's reported distress.
        self.serious_context = bool(re.search(
            r'严肃|求助|去世|焦虑|痛苦|(?:我很|我好|我真的|我感到|我现在|我有点|我太|我特别).{0,6}'
            r'(?:难过|伤心|生气)|我(?:很)?难过', text))
        self._refresh_revision()
        return previous_hint != self.instruction_hint()

    def _hear_dialect(self, text, request):
        """Follow a dialect the user is speaking, by its words in the ASR text.

        Runs on stable partials, so the prompt update usually lands before
        smart_turn starts the reply. "说普通话" and other explicit requests win.
        """
        if request:
            self.asr_dialect = None
            return
        found = dialect_marker(text)
        if not found:
            return
        language, dialect, word = found
        self.asr_dialect = {'language': language, 'dialect': dialect, 'evidence': word,
                            'source': 'asr_lexicon', 'evidence_level': 'lexical_inference',
                            'at': time.monotonic()}
        pair = (language, dialect)
        if pair != self.last_candidate:
            self.candidate = dict(language=language, dialect=dialect, source='asr_lexicon')
            self.last_candidate = pair
            self.first_pending = pair not in self.seen_candidates

    def _dialect_followed(self):
        heard = self.asr_dialect
        return bool(heard and time.monotonic() - heard['at'] <= DIALECT_FOLLOW_SECONDS)

    def reply_intent(self, *, language, dialect='unknown', evidence='', emotion='neutral', reason='',
                     transcript=''):
        validate_language(language, dialect)
        if dialect not in {'unknown', 'mandarin', 'cantonese'}:
            # Evidence is a phrase the user really said: the sidecar's stable
            # text, or the main ASR final when the sidecar is off (default).
            stable = (self.observation or {}).get('text', '')
            if not evidence.strip() or (evidence not in stable and evidence not in transcript):
                raise ValueError('dialect inference requires a phrase from the current utterance')
            if self.observation:
                self.observation = {**self.observation, 'dialect_candidate': dialect,
                                    'evidence_level': 'lexical_inference',
                                    'dialect_source': 'llm_inferred',
                                    'dialect_evidence': evidence[:120]}
        from annie_core.speech_style import choose_speech_style
        choose_speech_style(language=language, dialect=dialect, emotion=emotion)
        self.intent = dict(language=language, dialect=dialect, emotion=emotion,
                           source='llm_inferred' if evidence else 'llm_early', reason=reason[:120])
        self._intent_emotion_pending = self.response is None or self.active_turn_id in self.locked_turns
        pair = (language, dialect)
        if evidence and pair != self.last_candidate:
            self.candidate = dict(self.intent)
            self.last_candidate = pair
            self.first_pending = pair not in self.seen_candidates
        # An early optional FC may set expression; it cannot retroactively alter response language.
        if self.response is not None and self.active_turn_id not in self.locked_turns:
            chosen_emotion = self.style_request.get('emotion', emotion)
            chosen_source = 'explicit_user' if self.style_request else 'llm_early'
            self.response['emotion'] = chosen_emotion
            self.response['source'] = chosen_source
            if self.active_turn_id in self.styles:
                self.styles[self.active_turn_id]['emotion'] = chosen_emotion
                self.styles[self.active_turn_id]['source'] = chosen_source
        elif self.response is not None:
            self.application = 'next_turn'
        self._refresh_revision()

    def instruction_hint(self):
        selected = self.selection()
        if (not self.observation and not self.typed_observation and not any(self.preferences.values())
                and not self.explicit and not self.style_request and not self.intent
                and not self.candidate and not self._dialect_followed()):
            return ('\n【语音表达】默认按当前用户实际交谈语种自然回应；英语对话用英语，中文对话用中文，'
                    '不要把基础人格的中文描述误当作输出语言约束。'
                    '这一轮整句是外语时就用该语言回答，不要改用中文点评。'
                    '根据语境与人格安排重音与标点节奏，'
                    '不写情绪标签/JSON/旁白；明确情绪要求优先。'
                    'select_reply_style可选提前提出具体语气，不必每轮调用，不等待工具再开口。')
        return ('\n【语音表达】目标语种=' + selected['language'] + '；方言=' + selected['dialect']
                + ('；当前用户用此语种交流，优先用对应语种自然回答；不因历史中文回复改回中文'
                   if selected['source'] in {'typed_text', 'asr_text_heuristic'} else '')
                + ('；用户正用这种方言说话（说了“' + self.asr_dialect['evidence']
                   + '”），用它地道的口语词回答，不要只换一两个词'
                   if selected['source'] == 'asr_lexicon' and self.asr_dialect else '')
                + ('；新候选首次跟随一句，之后按语境自然延续或恢复默认' if self.first_pending else '')
                + ('；最近观察=' + self.candidate['language'] + '/' + self.candidate['dialect']
                   + '，非首次可按语境继续或恢复保存默认，不强制每轮跟随' if self.candidate else '')
                + ('；本轮用户表达要求=' + str(self.style_request) if self.style_request else '')
                + '；自然使用对应口语词汇和标点，只输出可朗读文本，不写情绪标签/JSON/旁白。'
                + '引用、翻译、句中夹杂外语词时不机械跟随；这一轮整句是外语时仍用该语言回答。'
                + '输入情绪只供温和回应参考。'
                + '仅当前用户明确要求保存/清除Chat或机器人语言偏好时调用对应工具，成功才说已保存。'
                + '可选select_reply_style供有证据的方言推断/语境选择，不必每轮调用，不等待工具再开口。')

    def confirm(self, revision):
        if revision == self.revision:
            self.confirmed_revision = revision

    def response_started(self):
        if self.response is not None:  # FC continuation belongs to the same logical reply.
            return
        self.actual_text_language = 'unknown'
        selected = self.selection()
        confirmed = self.confirmed_revision == self.revision
        # A direct typed-language target is known locally even while a provider
        # prompt update is unconfirmed. This does not verify actual LLM wording.
        # Dialect words in this very utterance are local evidence too: the
        # voice follows now, the wording catches up once the prompt lands.
        if not confirmed and selected['source'] not in {'explicit_user', 'typed_text',
                                                        'asr_lexicon'}:
            selected = self.default_selection()
        self.response = {**selected, 'delivery': 'contextual', **self.style_request,
                         **({'source': 'explicit_user'} if self.style_request else {}),
                         'revision': self.revision, 'text_started': False,
                         'serious': self.serious_context,
                         'input_emotion': (self.observation or {}).get('emotion', 'neutral')}
        if 'emotion' in selected:
            self._intent_emotion_pending = False
        self.application = 'current' if confirmed else 'unknown'
        self.last_decision = dict(self.response)
        if ((confirmed or selected['source'] == 'asr_lexicon')
                and self.first_pending and not self.explicit):
            self.first_pending = False
            self.seen_candidates.add((selected["language"], selected["dialect"]))
            self._refresh_revision()

    def first_text(self, turn_id):
        self.response_started()
        self.response['text_started'] = True
        self.active_turn_id = turn_id
        if turn_id not in self.styles:
            self.styles[turn_id] = dict(self.response)
            while len(self.styles) > 8:
                retired, _ = self.styles.popitem(last=False)
                self.locked_turns.discard(retired)

    def response_text_done(self, text):
        """Conservative text-only evidence; not TTS acoustic verification."""
        detected = prose_language_hint(text)
        self.actual_text_language = detected or 'unknown'

    def style_for(self, turn_id):
        from annie_core.speech_style import choose_speech_style
        data = self.styles.get(turn_id) or self.response or self.default_selection()
        data = {'delivery': 'contextual', **data}
        if data.get('source') == 'runtime_default':
            data = {**data, 'source': 'runtime_context'}
        return choose_speech_style(**{key: data[key] for key in (
            'language', 'dialect', 'emotion', 'input_emotion', 'source', 'rate', 'pitch', 'volume', 'serious',
            'delivery',
        ) if key in data})

    def response_finished(self):
        self.response = None
        self.active_turn_id = None
        self.explicit = None
        self.style_request = {}
        self.serious_context = False
        if not self._intent_emotion_pending:
            # Language can naturally continue; a consumed acting intent cannot
            # pin unrelated later replies to the same sadness/excitement.
            for selection in (self.intent, self.candidate):
                if selection:
                    selection.pop('emotion', None)
        self._refresh_revision()

    def snapshot(self):
        age = ((time.monotonic()*1000-self.observation['observed_monotonic_ms'])
               if self.observation else None)
        return {'input': {**self.observation, 'age_ms': round(age, 1)} if self.observation else None,
                'typed_input': dict(self.typed_observation) if self.typed_observation else None,
                'asr_text_input': dict(self.asr_text_observation) if self.asr_text_observation else None,
                'asr_dialect': ({k: v for k, v in self.asr_dialect.items() if k != 'at'}
                                if self._dialect_followed() else None),
                'preferences': self.preferences,
                'target': self.selection(), 'llm_decision': self.last_decision,
                'revision': self.revision, 'confirmed_revision': self.confirmed_revision,
                'application': self.application, 'first_follow_pending': self.first_pending,
                'llm_actual_text_language': self.actual_text_language,
                'tts_audibly_verified_language': 'unknown'}

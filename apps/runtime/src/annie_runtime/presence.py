"""Per-robot listening and reply admission policy; no SDK or audio I/O."""

import re
import time
import unicodedata

_PUNCTUATION = re.compile(r"[\s,，。！!？?、.：:~～…]+")
_WAKE_ONLY = re.compile(r"^(?:小安妮)+$")
_WAKE_ONLY_ASR_ALIASES = frozenset({
    "小安尼", "小安你", "小爱你", "小爱妮", "小阿妮",  # observed Mandarin homophones
    "анне", "ане", "aне",  # Cyrillic/Latin-mixed decoding of Annie ("А не.")
    "annie", "xiaoanni", "xiaoani", "ane",  # wake-name transliteration only
})
# The robot's name is a call, not content: Qwen ASR often writes it as a
# homophone ("小爱你，今天天气怎么样") although local KWS heard 小安妮. Such a
# leading call is shown and stored as 小安妮; typed text is never rewritten.
_CALL_END = r"[\s,，。！!？?、.：:~～…]"
# After a KWS hit the first utterance starts with the name, so any 小+安/爱 +
# 妮-like syllable is the call, also when ASR puts the pause inside it
# ("小爱，你今天……") or drops the last syllable ("小爱，今天……"). 你 is kept
# before 好/们: "小安你好" may be 小安妮 + 你好.
_KWS_CALL = re.compile(
    r"^\s*(?P<call>[小晓筱][安爱艾阿](?:(?P<pause>[，,、 ])?[你呢](?![好们])|[妮尼泥倪妳霓])"
    r"|[小晓筱][爱艾阿](?=[，,、。！？!?\s]))"
)
# Without that evidence only observed homophones used as a vocative count.
_ASR_CALL = re.compile(
    rf"^\s*(?P<call>小(?:安尼|安你|爱你|爱妮|阿妮|艾妮)|晓安妮)(?={_CALL_END})"
)


def canonical_wake_call(text, *, kws_confirmed=False):
    """(text with a misheard leading call written as 小安妮, the raw call or None).

    A pause ASR put inside the call moves behind it ("小爱，你" -> "小安妮，").
    """
    text = str(text or "")
    match = (_KWS_CALL if kws_confirmed else _ASR_CALL).match(text)
    if not match or match.group("call") == "小安妮":
        return text, None
    start, end = match.span("call")
    pause = match.groupdict().get("pause") or ""
    return text[:start] + "小安妮" + pause + text[end:], match.group("call")


_BACKCHANNEL = frozenset({"嗯", "嗯嗯", "啊", "哦", "噢", "好", "好的", "对", "是", "呃"})
_WAKE_FRAGMENTS = frozenset({"妮", "安妮", "小安", "小安尼"})
_SLEEP_COMMAND = re.compile(
    r"^(?:小安妮|安妮)?(?:请|现在|先|马上|立刻|赶紧|你)?"
    r"(?:退下(?:退下)?(?:别说了)?|闭嘴|别说话|不要说话|别说了|停止聊天|结束对话)"
    r"(?:吧|了|一下)?$"
)
_SLEEP_LEADS = (
    "小安妮", "能不能", "可不可以", "请你", "现在", "马上", "立刻", "赶紧",
    "可以", "帮我", "安妮", "请", "你", "先",
)
_SLEEP_STEMS = (
    "安静一会儿", "不想聊了", "不要说话", "停止聊天", "结束对话", "不用回答",
    "不用说话", "安静一下", "安静一会", "歇一会儿", "先停一下", "别出声",
    "别说话", "别说了", "别聊了", "别讲了", "不聊了", "不想聊", "安静点",
    "先停下", "去睡觉", "去休息", "歇一会", "歇会儿", "歇一下", "闭嘴",
    "退下", "休眠", "歇着",
)
_SLEEP_TAIL = re.compile(
    r"^(?:(?:了|吧)?(?:好吗|行吗|可以吗|好不好|啊|呀|哦|啦)?|"
    r"(?:一下|一会儿|一会|吗|么|点)?)$"
)
_SLEEP_REJECT = (
    "为什么", "怎么", "什么", "没让", "没有", "不是", "别让", "不要让",
    "干嘛", "干什么", "怎样",
)
ASR_GRACE_SECONDS = 10.0


class DialoguePresence:
    """Candidate speech never grants output permission by itself.

    The caller must separately bind provider response IDs to an admitted
    epoch and validate real physical speaker receipts before calling drained.
    """

    def __init__(self, *, clock=time.monotonic, wake_wait_ms=5000, followup_ms=20000,
                 side_talk_listen_ms=120000):
        if (type(wake_wait_ms) is not int or not 1000 <= wake_wait_ms <= 30000
                or type(followup_ms) is not int or not 1000 <= followup_ms <= 30000):
            raise ValueError("wake and followup windows must be 1000..30000 ms")
        if type(side_talk_listen_ms) is not int or not 10000 <= side_talk_listen_ms <= 600000:
            raise ValueError("side_talk_listen_ms must be 10000..600000 ms")
        self.clock = clock
        self.wake_wait_seconds = wake_wait_ms / 1000
        self.followup_seconds = followup_ms / 1000
        self.side_talk_listen_seconds = side_talk_listen_ms / 1000
        self._listen_since = None  # when the current wake/follow-up window opened
        self.epoch = 0
        self.state = "SLEEP"
        self.deadline = None
        self._window_deadline = None
        self._candidate = False
        self._candidate_item_id = None
        self._reply_admitted = False
        self._first_wake_transcript = False
        self.expirations = 0

    def close(self):
        """Revoke permissions on stop, goodbye, session switch or reconnect."""
        self.epoch += 1
        self.state = "SLEEP"
        self.deadline = None
        self._window_deadline = None
        self._candidate = False
        self._candidate_item_id = None
        self._reply_admitted = False
        self._first_wake_transcript = False

    def _expire(self):
        if self.deadline is not None and self.clock() >= self.deadline:
            self.expirations += 1
            self.close()

    def wake_detected(self):
        self._expire()
        if self.state != "SLEEP":
            return False  # A second wake hit must not extend the window.
        self.epoch += 1
        self.state = "WAKE_WAIT"
        self._first_wake_transcript = True
        self._listen_since = self.clock()
        self.deadline = self.clock() + self.wake_wait_seconds
        self._window_deadline = self.deadline
        return True

    def speech_started(self, item_id=None):
        self._expire()
        if self.state not in {"WAKE_WAIT", "FOLLOWUP", "ACTIVE"}:
            return False
        if (self._candidate and
                (item_id is None or item_id != self._candidate_item_id)):
            # A replacement item retires the initial utterance, not a duplicate.
            self._first_wake_transcript = False
        self._candidate = True
        self._candidate_item_id = item_id
        if self.state in {"WAKE_WAIT", "FOLLOWUP"}:
            # Speech began before the clock expired: protect the entire phrase.
            self.deadline = None
        return True

    def speech_stopped(self, *, item_id=None, invalid=False):
        self._expire()
        if not self._candidate or (self._candidate_item_id is not None
                                    and item_id is not None
                                    and item_id != self._candidate_item_id):
            return False
        if invalid:
            self._first_wake_transcript = False
            self._candidate = False
            self._candidate_item_id = None
            if self.state in {"WAKE_WAIT", "FOLLOWUP"}:
                self.deadline = self._window_deadline
            self._expire()
        elif self.state in {"WAKE_WAIT", "FOLLOWUP"}:
            # Qwen can produce transcript.completed after speech_stopped.
            self.deadline = self.clock() + ASR_GRACE_SECONDS
        return True

    @property
    def first_wake_utterance(self):
        """The utterance local KWS just admitted; its leading call is the name."""
        self._expire()
        return self.state == "WAKE_WAIT" and self._first_wake_transcript

    def side_talk(self, *, item_id=None):
        """Speech the decision model judged not said to the robot.

        People talking to each other must not end the conversation: the open
        window restarts from now, but only up to side_talk_listen_seconds
        after it first opened, so a long chat among people still ends in SLEEP
        (a call by name wakes her again). ACTIVE only drops the candidate.
        """
        if (self._candidate and self._candidate_item_id is not None
                and item_id is not None and item_id != self._candidate_item_id):
            return False
        self._first_wake_transcript = False
        self._candidate = False
        self._candidate_item_id = None
        if self.state in {"WAKE_WAIT", "FOLLOWUP"}:
            now = self.clock()
            limit = (self._listen_since if self._listen_since is not None else now) \
                + self.side_talk_listen_seconds
            self.deadline = self._window_deadline = min(
                max(self._window_deadline or now, now + self.followup_seconds), limit)
        self._expire()
        return True

    @staticmethod
    def is_wake_only(text):
        normalized = _PUNCTUATION.sub(
            "", unicodedata.normalize("NFKC", str(text or ""))
        ).strip().casefold()
        return bool(normalized and (
            _WAKE_ONLY.fullmatch(normalized) or normalized in _WAKE_ONLY_ASR_ALIASES
        ))

    @staticmethod
    def is_sleep_command(text):
        source = unicodedata.normalize("NFKC", str(text or ""))
        if "?" in source or "？" in source:
            return False  # A question about leaving is not an explicit command.
        normalized = _PUNCTUATION.sub("", source).strip()
        return bool(_SLEEP_COMMAND.fullmatch(normalized))

    @staticmethod
    def is_sleep_request(text):
        """Current-turn evidence that she was told to stop, including paraphrases.

        Exact commands stay on is_sleep_command so they can close immediately.
        This check only admits a model sleep call; it does not close the gate.
        """
        if DialoguePresence.is_sleep_command(text):
            return True
        source = unicodedata.normalize("NFKC", str(text or ""))
        if "?" in source or "？" in source:
            return False
        normalized = _PUNCTUATION.sub("", source).strip()
        if not normalized or len(normalized) > 24:
            return False
        if any(word in normalized for word in _SLEEP_REJECT):
            return False
        rest = normalized
        for _ in range(6):
            for stem in _SLEEP_STEMS:
                if rest.startswith(stem) and _SLEEP_TAIL.fullmatch(rest[len(stem):]):
                    return True
            for lead in _SLEEP_LEADS:
                if rest.startswith(lead) and len(rest) > len(lead):
                    rest = rest[len(lead):]
                    break
            else:
                return False
        return False

    def is_wake_transcript(self, text):
        """One observed misdecode, only on the first utterance after KWS.

        Do not globally classify expressions of affection as wake names or
        fuzzy-match longer utterances: wake + question must remain intact.
        """
        if self.is_wake_only(text):
            return True
        normalized = _PUNCTUATION.sub(
            "", unicodedata.normalize("NFKC", str(text or ""))
        ).strip()
        return (self.state == "WAKE_WAIT" and self._first_wake_transcript
                and normalized == "想爱你")

    @staticmethod
    def is_substantive(text):
        normalized = _PUNCTUATION.sub("", str(text or "")).strip().lower()
        rest = normalized.removeprefix("小安妮")
        return bool(normalized and not DialoguePresence.is_wake_only(text)
                    and normalized not in _WAKE_FRAGMENTS
                    and normalized not in _BACKCHANNEL
                    and rest not in _BACKCHANNEL)

    def transcript_accepted(self, text, *, item_id=None):
        self._expire()
        if (not self._candidate or self.state == "SLEEP" or
                (self._candidate_item_id is not None and item_id is not None
                 and self._candidate_item_id != item_id)):
            return False
        wake_only = self.is_wake_transcript(text)
        self._first_wake_transcript = False
        self._candidate = False
        self._candidate_item_id = None
        if wake_only or not self.is_substantive(text):
            if self.state in {"WAKE_WAIT", "FOLLOWUP"}:
                self.deadline = self._window_deadline
            self._expire()
            return False
        self.state = "ACTIVE"
        self.deadline = None  # No unconditional 30s expiry while speaking or TTS.
        # Each accepted utterance receives a new response grant.
        self.epoch += 1
        self._reply_admitted = True
        return True

    def speaker_drained(self, *, verified=False):
        self._expire()
        if not verified or self.state != "ACTIVE" or not self._reply_admitted:
            return False
        self.state = "FOLLOWUP"
        self._listen_since = self.clock()
        self.deadline = self.clock() + self.followup_seconds
        self._window_deadline = self.deadline
        self._reply_admitted = False
        self._candidate = False
        return True

    def permits_pcm(self):
        self._expire()
        return self.state != "SLEEP"

    def permits_audio_response(self, epoch):
        self._expire()
        return self.state == "ACTIVE" and self._reply_admitted and epoch == self.epoch

    def snapshot(self):
        self._expire()
        return {
            "state": self.state,
            "epoch": self.epoch,
            "pcm_to_qwen": self.state != "SLEEP",
            "audio_reply_allowed": self.state == "ACTIVE" and self._reply_admitted,
            "candidate_speech": self._candidate,
            "deadline_remaining_ms": (
                max(0, int((self.deadline - self.clock()) * 1000))
                if self.deadline is not None else None
            ),
            "expirations": self.expirations,
        }

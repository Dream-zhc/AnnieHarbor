"""Per-robot listening and reply admission policy; no SDK or audio I/O."""

import re
import time
import unicodedata

_PUNCTUATION = re.compile(r"[\s,，。！!？?、.：:~～…]+")
_WAKE_ONLY = re.compile(r"^(?:小安妮)+$")
_WAKE_ONLY_ASR_ALIASES = frozenset({
    "小安尼", "小安你", "小爱你", "小爱妮",  # observed Mandarin homophones
    "анне", "ане", "aне",  # Cyrillic/Latin-mixed decoding of Annie ("А не.")
    "annie", "xiaoanni", "xiaoani", "ane",  # wake-name transliteration only
})
_BACKCHANNEL = frozenset({"嗯", "嗯嗯", "啊", "哦", "噢", "好", "好的", "对", "是", "呃"})
_WAKE_FRAGMENTS = frozenset({"妮", "安妮", "小安", "小安尼"})
ASR_GRACE_SECONDS = 10.0


class DialoguePresence:
    """Candidate speech never grants output permission by itself.

    The caller must separately bind provider response IDs to an admitted
    epoch and validate real physical speaker receipts before calling drained.
    """

    def __init__(self, *, clock=time.monotonic, wake_wait_ms=5000, followup_ms=8000):
        if (type(wake_wait_ms) is not int or not 1000 <= wake_wait_ms <= 30000
                or type(followup_ms) is not int or not 1000 <= followup_ms <= 30000):
            raise ValueError("wake and followup windows must be 1000..30000 ms")
        self.clock = clock
        self.wake_wait_seconds = wake_wait_ms / 1000
        self.followup_seconds = followup_ms / 1000
        self.epoch = 0
        self.state = "SLEEP"
        self.deadline = None
        self._window_deadline = None
        self._candidate = False
        self._candidate_item_id = None
        self._reply_admitted = False
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
        self.deadline = self.clock() + self.wake_wait_seconds
        self._window_deadline = self.deadline
        return True

    def speech_started(self, item_id=None):
        self._expire()
        if self.state not in {"WAKE_WAIT", "FOLLOWUP", "ACTIVE"}:
            return False
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
            self._candidate = False
            self._candidate_item_id = None
            if self.state in {"WAKE_WAIT", "FOLLOWUP"}:
                self.deadline = self._window_deadline
            self._expire()
        elif self.state in {"WAKE_WAIT", "FOLLOWUP"}:
            # Qwen can produce transcript.completed after speech_stopped.
            self.deadline = self.clock() + ASR_GRACE_SECONDS
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
        self._candidate = False
        self._candidate_item_id = None
        if not self.is_substantive(text):
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

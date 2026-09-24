"""Small, evidence-only conversation rhythm state; no emotion classifier or model call."""

import time
from collections import deque

RECENT_SECONDS = 120

SHORT_ACKS = frozenset({"嗯", "嗯嗯", "哦", "好", "好的", "对", "是", "行", "随便", "没事", "知道了", "还行"})
CORRECTIONS = ("不是", "不对", "等等", "等下", "我说的是", "你听错", "先别")
DETAIL_REQUESTS = ("详细", "展开", "具体讲", "讲清楚", "为什么", "原理", "解释一下")


class DialogueState:
    """Tracks observable user replies and *delivered* assistant questions per Chat."""

    def __init__(self):
        self.user_turns = deque(maxlen=4)
        self.seen_user_ids = deque(maxlen=32)
        self.seen_assistant_ids = deque(maxlen=32)
        self.question_streak = 0
        self.interrupted = False
        self.last_user_at = None
        self.last_delivered_at = None
        self.interrupted_at = None

    def observe_user(self, turn_id: str, text: str) -> bool:
        text = text.strip()
        if not turn_id or not text:
            return False
        before = self.instruction_hint()
        for item in self.user_turns:
            if item[0] == turn_id:
                item[1] = text  # ASR final can replace a preliminary transcript.
                break
        else:
            if turn_id in self.seen_user_ids:  # late transcript, outside the recent window
                return False
            now = time.monotonic()
            if self.last_user_at is not None and now - self.last_user_at > RECENT_SECONDS:
                self.user_turns.clear()
            self.user_turns.append([turn_id, text])
            self.seen_user_ids.append(turn_id)
            self.last_user_at = now
        return before != self.instruction_hint()

    def mark_interrupt(self) -> bool:
        before = self.instruction_hint()
        self.interrupted = True
        self.interrupted_at = time.monotonic()
        return before != self.instruction_hint()

    def observe_delivered(self, turn_id: str, text: str) -> bool:
        if not turn_id or turn_id in self.seen_assistant_ids or not text.strip():
            return False
        before = self.instruction_hint()
        self.seen_assistant_ids.append(turn_id)
        now = time.monotonic()
        if self.last_delivered_at is not None and now - self.last_delivered_at > RECENT_SECONDS:
            self.question_streak = 0
        self.question_streak = self.question_streak + 1 if "？" in text or "?" in text else 0
        self.last_delivered_at = now
        self.interrupted = False
        self.interrupted_at = None
        return before != self.instruction_hint()

    def snapshot(self) -> dict:
        now = time.monotonic()
        recent_user = self.last_user_at is not None and now - self.last_user_at <= RECENT_SECONDS
        recent_reply = self.last_delivered_at is not None and now - self.last_delivered_at <= RECENT_SECONDS
        latest = self.user_turns[-1][1] if self.user_turns and recent_user else ""
        normalized = latest.strip(" ，。！？!? ")
        short_count = 0
        if recent_user:
            for _, text in reversed(self.user_turns):
                if text.strip(" ，。！？!? ") not in SHORT_ACKS:
                    break
                short_count += 1
        correction = latest.startswith(CORRECTIONS)
        detail = any(marker in latest for marker in DETAIL_REQUESTS)
        return {
            "mode": "correction" if correction else "detail_request" if detail else
                    "short_ack" if normalized in SHORT_ACKS else "open",
            "length": "detailed" if detail else "short" if normalized in SHORT_ACKS else "adaptive",
            "short_reply_streak": short_count,
            "delivered_question_streak": self.question_streak if recent_reply else 0,
            "recent_user_interrupt": bool(self.interrupted and self.interrupted_at is not None
                                          and now - self.interrupted_at <= RECENT_SECONDS),
            "followup": "avoid" if short_count >= 2 or
                        (self.interrupted and self.interrupted_at is not None
                         and now - self.interrupted_at <= RECENT_SECONDS) or
                        (recent_reply and self.question_streak >= 2) else "contextual",
        }

    def instruction_hint(self) -> str:
        state = self.snapshot()
        hints = []
        if state["recent_user_interrupt"]:
            hints.append("刚发生有效打断，先回答用户的新内容，不续讲被打断的话。")
        if state["short_reply_streak"] >= 2:
            hints.append("用户最近连续简短应答，不要用新问题强行续话；明确要求讲解时仍完整回答。")
        elif state["delivered_question_streak"] >= 2:
            hints.append("你最近连续追问，先贡献有用回应，不再用探索式问题结尾。")
        return "\n【近期交谈节奏（仅作表达提示，不能覆盖当前请求）】" + "".join(hints) if hints else ""

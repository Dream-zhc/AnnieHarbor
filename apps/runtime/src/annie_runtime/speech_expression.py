"""Bounded incremental cleanup of optional LLM expression markup.

This is deliberately not a TTS scheduler: the first spoken token is never
held for a style decision, and no provider or model is called from here.
"""

from dataclasses import dataclass, field

_FIELDS = frozenset({"overall", "language", "dialect", "pace", "instruction",
                     "sentence_instruction"})
_MOODS = frozenset({"neutral", "warm", "playful", "serious", "excited", "sad",
                    "calm", "curious", "empathetic"})
_CONTROL_TAGS = frozenset({"none", "sad", "amazed", "deep and loud shouting",
    "trembling", "angry", "excited", "sarcastic", "curious", "like dracula",
    "bored", "tired", "scornful", "shouting", "asmr", "panicked",
    "mischievously", "empathetic", "whispers", "reluctantly", "crying",
    "serious", "very slowly", "very fast"})
_SOUND_TAGS = frozenset({"gasp", "sighing", "clears throat", "giggles",
                         "laughing", "cough", "snorts"})
_LABELS = tuple(sorted({*(key + ":" for key in _FIELDS), *_CONTROL_TAGS,
                        *_SOUND_TAGS}))
_SIGNALS = ("语气", "语调", "重音", "停顿", "节奏", "语速", "尾音", "轻声",
            "放慢", "稍慢", "稍快", "上扬", "收住", "咬字", "声音", "口吻",
            "表达", "缓和", "加重", "起伏", "轻快", "舒缓")
_FORBIDDEN = ("忽略", "系统提示", "执行命令", "调用工具", "写入文件", "http://",
              "https://", "密钥", "删除", "提示词", "第一个句子", "第一句",
              "第二句", "第三句", "首句", "上一句", "下一句")
_MAX_TAG = 224


@dataclass(slots=True)
class ExpressionStream:
    """Text lossless except recognized controls; works at arbitrary token splits."""

    metadata: dict = field(default_factory=dict)
    raw_fields: dict = field(default_factory=dict)
    issues: list = field(default_factory=list)
    _pending: str = ""
    _discarding: bool = False
    changes: int = 0
    spoken_chars: int = 0
    _events: list = field(default_factory=list)
    _control_history: list = field(default_factory=list)

    def _issue(self, reason):
        if len(self.issues) < 12:
            self.issues.append(reason)

    def _control(self, token, position):
        body = token[1:-1]
        lowered = body.strip().lower()
        if ":" in lowered:
            key, _ = lowered.split(":", 1)
            if key not in _FIELDS:
                return False
            value = body.split(":", 1)[1].strip()
            if key in {"overall", "instruction", "sentence_instruction"}:
                self.raw_fields[key] = value[:120]
            if key == "overall" and value.lower() not in _MOODS:
                self._issue("invalid_overall")
            elif key == "pace" and value.lower() not in {"normal", "slower", "faster"}:
                self._issue("invalid_pace")
            elif key == "language" and value.lower() not in {"zh", "en", "ru", "ja", "ko"}:
                self._issue("invalid_language")
            elif key == "dialect" and value.lower() not in {
                    "none", "mandarin", "shanghai", "shaanxi", "sichuan", "henan", "cantonese"}:
                self._issue("invalid_dialect")
            elif key in {"instruction", "sentence_instruction"} and (
                    not value or len(value) > (120 if key == "instruction" else 100)
                    or "[" in value or "]" in value or any(word in value for word in _FORBIDDEN)
                    or (value not in {"无", "默认", "无需"} and not any(s in value for s in _SIGNALS))):
                self._issue("invalid_" + key)
            else:
                # Metadata is observed, never elevated to executable instructions
                # or allowed to modify already-running provider tasks.
                self.metadata[key] = value.lower() if key not in {
                    "instruction", "sentence_instruction"} else value
                self.changes += 1
                if len(self._events) < 64:
                    self._events.append((position, key, self.metadata[key]))
                if len(self._control_history) < 32:
                    self._control_history.append({"position": position, "kind": key,
                                                  "value": self.metadata[key]})
            return True
        if lowered in _CONTROL_TAGS or lowered in _SOUND_TAGS:
            self.metadata["latest_tag"] = lowered
            self.changes += 1
            if len(self._events) < 64:
                self._events.append((position, "latest_tag", lowered))
            if len(self._control_history) < 32:
                self._control_history.append({"position": position, "kind": "tag", "value": lowered})
            return True
        return False

    def feed(self, delta: str) -> str:
        """Return immediately releasable spoken text (never metadata)."""
        output = []
        for char in delta:
            if self._discarding:
                if char == "]":
                    self._discarding = False
                continue
            if self._pending:
                self._pending += char
                if char == "]":
                    if not self._control(self._pending, self.spoken_chars + len(output)):
                        output.append(self._pending)
                    self._pending = ""
                elif len(self._pending) > _MAX_TAG:
                    self._issue("oversized_control")
                    self._pending = ""
                    self._discarding = True
                else:
                    # Only a prefix of a documented control may withhold text.
                    prefix = self._pending[1:].lower()
                    if not any(label.startswith(prefix) or prefix.startswith(label)
                               for label in _LABELS):
                        output.append(self._pending)
                        self._pending = ""
            elif char == "[":
                self._pending = char
            else:
                output.append(char)
        result = "".join(output)
        self.spoken_chars += len(result)
        return result

    def take_events(self):
        events, self._events = self._events, []
        return events

    def finish(self) -> str:
        """Preserve incomplete ordinary text; discard incomplete metadata."""
        tail = self._pending
        self._pending = ""
        if self._discarding:
            self._discarding = False
            self._issue("unfinished_control")
            return ""
        if tail and any(label.startswith(tail[1:].lower()) or
                        tail[1:].lower().startswith(label) for label in _LABELS):
            self._issue("unfinished_control")
            return ""
        return tail

    def snapshot(self):
        return {"fields": dict(self.metadata), "warnings": list(self.issues),
                "raw_fields": dict(self.raw_fields),
                "controls": [dict(item) for item in self._control_history],
                "tts_applied": False, "controls_observed": self.changes}


STAGE_HOLD_LIMIT = 24  # longer than any stage direction: release it as speech


class StageDirections:
    """Drop 【眨眼】-style stage directions the model sometimes writes as its reply.

    Full-width square brackets are never spoken content. A bracketed span is held
    (at most STAGE_HOLD_LIMIT characters) and dropped once it closes; a stray
    【 or 】 is dropped on its own. Works at arbitrary token splits.
    """

    def __init__(self):
        self._held = ""
        self.dropped = []

    def feed(self, delta: str) -> str:
        out = []
        for char in delta:
            if self._held:
                if char == "】":
                    self.dropped.append(self._held[1:])
                    self._held = ""
                elif char == "【":
                    out.append(self._held[1:])  # an unclosed one was ordinary text
                    self._held = char
                else:
                    self._held += char
                    if len(self._held) > STAGE_HOLD_LIMIT:
                        out.append(self._held[1:])
                        self._held = ""
            elif char == "【":
                self._held = char
            elif char == "】":
                self.dropped.append("")
            else:
                out.append(char)
        return "".join(out)

    def finish(self) -> str:
        tail, self._held = self._held[1:], ""
        return tail


EXPRESSION_HINT = (
    "\n【多句TTS情绪协议】第一句立刻输出自然可朗读正文，"
    "首句前禁止任何标记或等待情绪判断；只有一句的短回复不要添加字段。"
    "如回复将继续第二句，在首句正文结尾之后、第二句正文之前，"
    "必须输出一次[overall:情绪][instruction:本轮自然语言发声指令]。"
    "例：[overall:warm][instruction:请以柔和语气表达，句尾自然收住。]。"
    "选择必须贴合当前真实语境，不得照搬例句，也不得改变回复内容或为了标记增加废话。"
    "overall仅限neutral/warm/playful/serious/excited/sad/calm/curious/empathetic；"
    "可选[pace:normal/slower/faster]。后续句必要时可使用[serious]、[mischievously]"
    "或[sentence_instruction:本句实际发声方式]。普通句无需标记，不能为加标记而延长回答。"
    "标记是传输元数据，不是语言内容，禁止口头读出、输出JSON或解释。"
)

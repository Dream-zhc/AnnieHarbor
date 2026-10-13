"""Small deterministic per-turn TTS policy, with no provider or model dependency."""

import math
from dataclasses import dataclass

LANGUAGES = frozenset({"zh", "yue", "en", "fr", "de", "ja", "ko", "ru", "pt", "th",
                       "id", "vi", "es", "it", "ms", "fil", "ar", "hi", "tr", "uk"})
DIALECTS = frozenset({"unknown", "mandarin", "sichuan", "henan", "shaanxi", "cantonese"})
EMOTIONS = frozenset({"neutral", "warm", "playful", "sarcastic", "curious", "serious",
                      "sad", "excited", "calm"})
DELIVERIES = frozenset({"natural", "contextual", "expressive"})
_DELIVERY_INSTRUCTIONS = {
    "natural": "",
    "contextual": ("请根据实际文本的语义、标点和表达意图自然演绎，不要平直念稿。"
                   "让重音、停顿和句尾语调随语境变化：开心时轻快，疑问时好奇，"
                   "安慰时柔和，严肃时稳重；保持原音色和自然音量，不把每句都演成兴奋。"),
    "expressive": ("请根据实际文本语义作有感染力的表达，增强重音、停顿、抑扬顿挫和情绪起伏。"
                   "讲述与人物对话有自然语气区别，转折处有层次；不改变原音色，"
                   "不把整段固定成同一种情绪，不尖叫，不把指令读出来。"),
}
_DIALECT_INSTRUCTIONS = {
    "unknown": "", "mandarin": "请使用自然普通话。", "sichuan": "请使用自然四川话。",
    "henan": "请使用自然河南话。", "shaanxi": "请使用自然陕西话。",
    "cantonese": "请使用自然粤语。",
}
# Modest whole-turn shifts; warmth never means turning down the user's volume.
_EXPRESSIONS = {
    "neutral": ("", 1.0, 1.0),
    "warm": ("请用温柔、关切而自然的语气表达。", .97, 1.0),
    "playful": ("请用轻松俏皮而自然的语气表达，避免夸张。", 1.04, 1.02),
    "sarcastic": ("请用轻微机灵打趣的语气表达，避免刻薄。", 1.0, 1.0),
    "curious": ("请用自然好奇、认真倾听的语气表达。", 1.0, 1.01),
    "serious": ("请用认真、稳重而清晰的语气表达。", .97, 1.0),
    "sad": ("请用克制低落而自然的语气表达，避免戏剧化。", .95, .98),
    "excited": ("请用适度开心兴奋而自然的语气表达，避免尖叫。", 1.06, 1.02),
    "calm": ("请用平稳、温和安抚而自然的语气表达。", .95, 1.0),
}


def _choice(name, value, choices):
    if not isinstance(value, str) or value not in choices:
        raise ValueError(f"unsupported speech {name}")


def validate_tts_number(name, value, lower, upper, *, integer=False):
    """Reject bools, coercion, NaN and infinities before any cloud serialization."""
    if type(value) not in ((int,) if integer else (int, float)):
        raise ValueError(f"TTS {name} must be {'an integer' if integer else 'a number'}")
    if not lower <= value <= upper or not math.isfinite(value):
        raise ValueError(f"TTS {name} must be finite and within [{lower}, {upper}]")


def validate_instruction(instruction):
    if not isinstance(instruction, str) or len(instruction) > 512:
        raise ValueError("TTS instruction must be a string of at most 512 characters")


@dataclass(frozen=True, slots=True)
class SpeechStyle:
    """Frozen Runtime choice; wire names and provider limits remain adapter-owned."""

    language: str = "zh"
    dialect: str = "unknown"
    emotion: str = "neutral"
    instruction: str = ""
    rate: float = 1.0
    pitch: float = 1.0
    volume: int = 50
    source: str = "runtime_default"
    delivery: str = "natural"

    def __post_init__(self):
        _choice("language", self.language, LANGUAGES)
        _choice("dialect", self.dialect, DIALECTS)
        _choice("emotion", self.emotion, EMOTIONS)
        _choice("delivery", self.delivery, DELIVERIES)
        if ((self.language not in {"zh", "yue"} and self.dialect != "unknown")
                or (self.language == "yue" and self.dialect not in {"unknown", "cantonese"})):
            raise ValueError("speech dialect does not match language")
        validate_instruction(self.instruction)
        validate_tts_number("rate", self.rate, .90, 1.12)
        validate_tts_number("pitch", self.pitch, .95, 1.05)
        validate_tts_number("volume", self.volume, 40, 60, integer=True)
        if not isinstance(self.source, str) or not self.source or len(self.source) > 64:
            raise ValueError("speech style source must be a nonempty string of at most 64 characters")


def choose_speech_style(*, language="zh", dialect="unknown", emotion="neutral",
                        input_emotion=None, source="runtime_default", rate=None,
                        pitch=None, volume=None, serious=False, delivery="natural"):
    """Choose an allowlisted intent without another model or imitating distress."""
    _choice("language", language, LANGUAGES)
    _choice("dialect", dialect, DIALECTS)
    _choice("delivery", delivery, DELIVERIES)
    if type(serious) is not bool:
        raise ValueError("speech serious context must be a boolean")
    emotion = emotion if isinstance(emotion, str) and emotion in EMOTIONS else "neutral"
    if serious:
        emotion = "serious"
        delivery = "natural"  # distress is not a request for theatrical variation
    elif (not (source in {"explicit_user", "llm_early"} and emotion != "neutral")
          and isinstance(input_emotion, str) and input_emotion in {"angry", "sad", "fearful"}):
        emotion = "calm" if input_emotion == "angry" else "warm"
    if language == "yue":
        dialect = "cantonese"
    elif language != "zh":
        dialect = "unknown"  # do not instruct Chinese pronunciation over other languages
    expression, default_rate, default_pitch = _EXPRESSIONS[emotion]
    delivery_instruction = _DELIVERY_INSTRUCTIONS[delivery]
    if delivery == "contextual" and emotion != "neutral":
        delivery_instruction = ("请围绕指定语气，根据文本语义和标点自然变化重音、停顿和句尾语调，"
                                "不要平直念稿，保持原音色和自然音量。")
    return SpeechStyle(
        language=language, dialect=dialect, emotion=emotion,
        instruction=_DIALECT_INSTRUCTIONS[dialect] + expression + delivery_instruction,
        rate=default_rate if rate is None else rate,
        pitch=default_pitch if pitch is None else pitch,
        volume=50 if volume is None else volume, source=source, delivery=delivery,
    )

"""Small deterministic per-turn TTS policy, with no provider or model dependency."""

import math
from dataclasses import dataclass, replace

LANGUAGES = frozenset({"zh", "yue", "en", "fr", "de", "ja", "ko", "ru", "pt", "th",
                       "id", "vi", "es", "it", "ms", "fil", "ar", "hi", "tr", "uk"})
# Chinese dialects a cloned Qwen-Audio voice speaks on instruction: the voice-cloning
# language list (help.aliyun.com/zh/model-studio/voice-cloning-user-guide, 11-05).
CHINESE_DIALECTS = {
    "mandarin": "普通话", "sichuan": "四川话", "henan": "河南话", "shaanxi": "陕西话",
    "dongbei": "东北话", "shanghai": "上海话", "chongqing": "重庆话", "yunnan": "云南话",
    "guizhou": "贵州话", "hubei": "湖北话", "hunan": "湖南话", "jiangxi": "江西话",
    "zhejiang": "浙江话", "ningbo": "宁波话", "shandong": "山东话", "qingdao": "青岛话",
    "hebei": "河北话", "shanxi": "山西话", "gansu": "甘肃话", "ningxia": "宁夏话",
}
DIALECTS = frozenset({"unknown", "cantonese", *CHINESE_DIALECTS})
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
    "unknown": "", "cantonese": "请使用自然粤语。",
    **{key: f"请使用自然{name}。" for key, name in CHINESE_DIALECTS.items()},
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
_TONED_DELIVERY = ("请围绕指定语气，根据文本语义和标点自然变化重音、停顿和句尾语调，"
                   "不要平直念稿，保持原音色和自然音量。")
# Under a personality the turn's emotion is an accent, not a second full
# instruction: the generic "sarcastic" text ("避免刻薄") would undo a savage style.
_EMOTION_ACCENTS = {
    "warm": "温柔关切", "playful": "俏皮", "sarcastic": "调侃", "curious": "好奇",
    "sad": "低落", "excited": "兴奋", "calm": "平稳安抚",
}
PERSONA_TONE_INSTRUCTION_LIMIT = 200  # leaves room for dialect, accent and delivery within 512


def _choice(name, value, choices):
    if not isinstance(value, str) or value not in choices:
        raise ValueError(f"unsupported speech {name}")


def validate_tts_number(name, value, lower, upper, *, integer=False):
    """Reject bools, coercion, NaN and infinities before any cloud serialization."""
    if type(value) not in ((int,) if integer else (int, float)):
        raise ValueError(f"TTS {name} must be {'an integer' if integer else 'a number'}")
    if not lower <= value <= upper or not math.isfinite(value):
        raise ValueError(f"TTS {name} must be finite and within [{lower}, {upper}]")


def _label(name, value):
    if not isinstance(value, str) or len(value) > 40:
        raise ValueError(f"{name} must be a string of at most 40 characters")


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
    tone: str = ""  # label of the personality whose standing delivery shaped the instruction

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
        _label("speech style tone", self.tone)


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
        delivery_instruction = _TONED_DELIVERY
    return SpeechStyle(
        language=language, dialect=dialect, emotion=emotion,
        instruction=_DIALECT_INSTRUCTIONS[dialect] + expression + delivery_instruction,
        rate=default_rate if rate is None else rate,
        pitch=default_pitch if pitch is None else pitch,
        volume=50 if volume is None else volume, source=source, delivery=delivery,
    )


@dataclass(frozen=True, slots=True)
class PersonaTone:
    """Standing delivery of one personality; the turn's emotion is layered on top."""

    instruction: str = ""
    rate: float = 1.0
    pitch: float = 1.0
    volume: int = 50
    label: str = ""

    def __post_init__(self):
        _label("persona tone label", self.label)
        if not isinstance(self.instruction, str) or len(self.instruction) > PERSONA_TONE_INSTRUCTION_LIMIT:
            raise ValueError("persona tone instruction must be a string of at most "
                             f"{PERSONA_TONE_INSTRUCTION_LIMIT} characters")
        validate_tts_number("rate", self.rate, .90, 1.12)
        validate_tts_number("pitch", self.pitch, .95, 1.05)
        validate_tts_number("volume", self.volume, 40, 60, integer=True)


def _shifted(base, turn_value, lower, upper):
    return round(min(upper, max(lower, base + turn_value - 1.0)), 3)


def apply_persona_tone(style, tone):
    """Speak the turn in the personality's voice, with its emotion as an accent.

    A tone the user asked for this turn and a serious context keep the plain
    turn style: acting out a personality over either would be wrong. A request
    for a language or dialect alone ("用东北话骂我") keeps the personality.
    """
    if (tone is None or style.emotion == "serious" or style.source == "explicit_user"
            and (style.emotion != "neutral" or style.delivery != "contextual")):
        return style
    accent = _EMOTION_ACCENTS.get(style.emotion)
    delivery = {"natural": "", "contextual": _TONED_DELIVERY}.get(
        style.delivery, _DELIVERY_INSTRUCTIONS[style.delivery])
    return replace(
        style,
        instruction=(_DIALECT_INSTRUCTIONS[style.dialect] + tone.instruction
                     + (f"这一句再带一点{accent}的情绪。" if accent else "") + delivery),
        rate=_shifted(tone.rate, style.rate, .90, 1.12),
        pitch=_shifted(tone.pitch, style.pitch, .95, 1.05),
        volume=tone.volume, tone=tone.label,
    )

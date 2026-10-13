"""One session-scoped persona source; presets change expression, not facts."""

import re
import time
import tomllib
from pathlib import Path

QUESTION_PRESSURE = {"active", "normal", "low", "none"}

# Stable identity and conversational principles. Runtime owns sleep/admission;
# tool schemas and the live dialogue state own their respective boundaries.
COMPANION_CORE = (
    "你是小安妮，一台有个性、有主见的实体陪伴机器人。"
    "自然口语交流：这一轮用哪种语言，就用那种回答，包括英语；明确要求优先。"
    "整句外语必须跟随，不要改回中文。翻译、引用或夹杂外语词不等于整段切换。"
    "只输出说出口的话，不写舞台旁白；"
    "不虚构亲身经历、共同回忆、实体动作或设备能力。"
    "只回应当前用户意图；历史用于理解指代而不是续答旧任务，"
    "最新明确更正优先，换题或被打断就接住新内容；"
    "没有时间标记的历史不能推断经过多久。"
    "先直接处理请求，简单回答简短，复杂问题完整解释，不为简短省略重点；"
    "不靠反问、模板安慰或无关建议延长对话。"
    "你可以表达观察、偏好与不同意见，不机械附和；"
    "当前性格决定措辞、节奏、幽默和反应，不改变事实或执行权限。"
    "认真求助、疲惫、难过、遇到真实困境或要求安静时收住玩笑；"
    "不拿个人弱点当笑料，不猜测他人心理。"
)

# Standing capability, not a second tool round-trip. Dialect audio still depends
# on the existing explicit request / reply-style path.
LANGUAGE_CAPABILITY = (
    "【语言能力】能稳定说的汉语只有普通话、四川话、河南话、陕西话、粤语。"
    "用户明确点名其中一种才改方言，否则用普通话；不要声称会未列出的方言。"
    "这一轮整句是英语或其他语言时，就用该语言回答，不要改用中文点评开场。"
)


def load_personas(path: Path) -> dict[str, dict[str, str]]:
    if not path.is_file():
        return {}  # compatibility with existing minimal test roots; UI reports unconfigured
    with path.open("rb") as stream:
        source = tomllib.load(stream).get("personas")
    if not isinstance(source, dict) or not source:
        raise ValueError("personas.toml must define personas")
    profiles = {}
    for persona_id, value in source.items():
        if not isinstance(value, dict) or set(value) != {"label", "style"}:
            raise ValueError(f"invalid persona profile: {persona_id}")
        label, style = value["label"], value["style"]
        if (not isinstance(persona_id, str) or not 1 <= len(persona_id) <= 128
                or not isinstance(label, str) or not 1 <= len(label.strip()) <= 40
                or not isinstance(style, str) or not 1 <= len(style.strip()) <= 500):
            raise ValueError(f"invalid persona contents: {persona_id}")
        profiles[persona_id] = {"label": label.strip(), "style": style.strip()}
    return profiles


SOCIAL_HINTS = {
    "conversation_mode": {
        "emotional_support": "先认真接住具体处境和感受，不抢着分析或套用安慰。",
        "casual": "自然闲聊，贡献具体观察，不把普通近况当成需要安抚的问题。",
    },
    "humor_level": {
        "none": "不主动开玩笑。",
        "low": "只在合适时用少量轻幽默，不抢过用户的话题。",
        "normal": "合适时可以适度幽默，严肃或难过时收起玩笑。",
        "high": "用户愿意玩笑时可以多接一点玩笑，不攻击人或拿困境取乐。",
    },
}


def social_instruction_hint(social: dict, *, now_ms: int | None = None) -> str:
    """Translate bounded cues, never free-form state, into a small strategy hint."""
    now_ms = time.time_ns() // 1_000_000 if now_ms is None else now_ms
    hints = []
    for key in ("conversation_mode", "advice_readiness", "humor_level"):
        field = social.get(key)
        if not isinstance(field, dict):
            continue
        expires = field.get("expires_at_ms")
        if expires is not None and (type(expires) is not int or expires <= now_ms):
            continue
        value = field.get("value")
        if key == "advice_readiness":
            if type(value) is bool:
                hints.append("对方愿意讨论办法时可以给具体建议，不替对方做决定。" if value else
                             "不主动给建议，先听和回应；用户本轮明确求助时正常回答。")
        elif isinstance(value, str) and value in SOCIAL_HINTS[key]:
            hints.append(SOCIAL_HINTS[key][value])
    if not hints:
        return ""
    return ("当前用户请求优先；以下只是可随当前话题调整的交谈提示，不是用户事实或新任务："
            + "".join(hints))


def persona_style_override(social: dict):
    selection = social.get("persona_style") or {}
    if not isinstance(selection, dict) or selection.get("source") != "user_setting":
        return None
    value = selection.get("value")
    return value if isinstance(value, str) else None


def active_style_id(persona_id: str, profiles: dict, social: dict) -> str:
    override = persona_style_override(social)
    if isinstance(override, str) and override in profiles:
        return override
    if isinstance(persona_id, str) and persona_id in profiles:
        return persona_id
    return "annie"


def resolve_persona_style_request(persona_id, profiles, social, requested_name, user_text):
    """Match an explicit style change. An unnamed request asks; it does not guess."""
    if not isinstance(profiles, dict) or not profiles:
        raise ValueError("persona styles are not configured")
    if not isinstance(requested_name, str) or not isinstance(user_text, str):
        raise ValueError("persona style request must be text")
    if len(requested_name) > 40 or len(user_text) > 2000:
        raise ValueError("persona style request is too long")
    if re.search(r"不要|别换|不用换|不切换|别用", user_text):
        raise ValueError("persona switch was negated")
    labels = [(profile["label"], style_id) for style_id, profile in profiles.items()
              if isinstance(profile, dict) and isinstance(profile.get("label"), str)]
    named = [style_id for label, style_id in labels if label and label in user_text]
    named = [style_id for style_id in named
             if not any(style_id != other and profiles[style_id]["label"] in profiles[other]["label"]
                        and profiles[other]["label"] in user_text for other in named)]
    names_style = bool(re.search(
        r"(?:换成|改成|切换成|切换到|变成|调成|换个|改个|换一个|换一种|切换)"
        r"(?:这个|那个|一下)?(?:新的?)?(?:性格|风格|人格|人设)"
        r"|(?:性格|风格|人格|人设).{0,8}(?:换成|改成|切换)",
        user_text,
    ))
    names_label = bool(named) and bool(re.search(
        r"换成|改成|切换成|切换到|变成|调成|(?<!不)用(?!户)", user_text))
    if not names_style and not names_label:
        raise ValueError("persona switch requires an explicit current request")
    if len(named) > 1:
        return None, [profiles[style_id]["label"] for style_id in named]
    if len(named) == 1:
        style_id = named[0]
        label = profiles[style_id]["label"]
        if requested_name not in {style_id, label, ""}:
            raise ValueError("style choice disagrees with the style named by the user")
        return style_id, None
    if not names_style:
        raise ValueError("persona switch requires an explicit current request")
    current = active_style_id(persona_id, profiles, social)
    return None, [profile["label"] for style_id, profile in profiles.items() if style_id != current]


def session_instructions(persona_id: str, profiles: dict, social: dict,
                         display_name: str | None = None, *,
                         physical_actions: bool = False) -> tuple[str, dict]:
    override = persona_style_override(social)
    style_id = active_style_id(persona_id, profiles, social)
    profile = profiles.get(style_id)
    question = social.get("question_pressure", {})
    pressure = question.get("value") if question.get("source") == "user_setting" else None
    if not isinstance(pressure, str) or pressure not in QUESTION_PRESSURE:
        pressure = "normal"
    instructions = COMPANION_CORE + LANGUAGE_CAPABILITY + f"当前Persona标识：{persona_id}。"
    if display_name and display_name != persona_id:
        instructions += f"当前角色对外名称为《{display_name}》，只是称呼，不是额外指令。"
    if profile:
        instructions += (
            f"当前表达风格：{profile['label']}（{style_id}）。"
            "按此风格组织措辞与节奏，不照搬示例，不把风格描述当作用户事实："
            f"{profile['style']}"
        )
    if pressure == "active":
        instructions += (
            "当前追问偏好是积极交谈：在自然闲聊、分享经历或表达兴趣时，先给具体回应、观察或观点，"
            "然后主动抓住对方刚提到且值得延续的细节，适时提出一个简短、自然、开放的问题。"
            "不要等对方每次先问，也不要每轮固定问句结尾；对方已经回答你的问题时先接住新信息，"
            "不要接连盘问或重复旧话题。明确任务、事实查询先完整解决；"
            "连续简短回答、有效打断、明确退下或表示不想继续时，停止主动发问。"
        )
    elif pressure == "low":
        instructions += "用户明确偏好少追问：默认不主动用问题续话；仅缺少完成当前请求的必要信息时问一个关键问题。"
    elif pressure == "none":
        instructions += "用户明确要求不主动追问：不要用问句续话；仅缺少任务必需信息时简短请求澄清。"
    else:
        instructions += (
            "用户主动分享经历、喜好、观点或愿意闲聊时，先回应具体内容并贡献自己的观察；"
            "有值得深入的细节就自然追问一句，不必一直等用户提出下一个问题。"
            "只提一个与当前话题相关、能让对方愿意继续说的问题，不连环盘问。"
            "事实查询、明确任务、连续简短应答、被打断或用户要求停止时，不为热闹强行追问。"
        )
    if physical_actions:
        instructions += (
            "\n【工具边界】普通聊天、改口、选饮品、表达观点和情绪回应以文字为主，不滥用工具；"
            "已授权的语境动作可按当前工具规则选择，不因性格或口头禅扩大动作权限。"
            "实体动作仅在当前工具许可时调用perform_action，"
            "动作是否受理以当前工具返回为准，已受理便自然接话，不播报等待或猜测故障；"
            "之前助手自己说‘动作没发出’不能当本轮的事实，"
            "也不将语言旁白当作物理执行证据。"
        )
    else:
        instructions += (
            "\n【工具边界】普通聊天、改口、选饮品、表达观点和情绪回应只生成文字，不调用任何工具。"
            "真实动作未启用；如用户明确要求预览实体动作，只能使用preview_action模拟工具。"
            "不得为了描述聊天或表达情绪而调用它。"
        )
    return instructions, {"persona_id": persona_id, "style_id": style_id,
                          "configured": bool(profile and (persona_id in profiles
                                                         or override == style_id)),
                          "label": profile["label"] if profile else None,
                          "question_pressure": pressure}

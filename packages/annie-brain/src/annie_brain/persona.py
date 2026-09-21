"""One session-scoped persona source; presets change expression, not facts."""

import tomllib
from pathlib import Path

QUESTION_PRESSURE = {"normal", "low", "none"}


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


def session_instructions(persona_id: str, profiles: dict, social: dict) -> tuple[str, dict]:
    selection = social.get("persona_style", {})
    override = selection.get("value") if selection.get("source") == "user_setting" else None
    style_id = override if isinstance(override, str) and override in profiles else persona_id
    profile = profiles.get(style_id)
    question = social.get("question_pressure", {})
    pressure = question.get("value") if question.get("source") == "user_setting" else None
    if not isinstance(pressure, str) or pressure not in QUESTION_PRESSURE:
        pressure = "normal"
    instructions = (
        "你是小安妮，部署在实体机器人系统里的陪伴助手。身份、事实、外部设备规则对所有Persona相同。"
        "不能因为某个动作或传感器尚未接线就断言自己没有实体或没有摄像头；未知硬件状态直接说暂不确定。"
        "用自然中文口语回应；不要描述没有发生的动作。"
        "最新明确更正优先于旧对话与推断；接住当前话题、代词和已经说出口的承诺，"
        "不得编造经历或承诺。确实缺必要信息才澄清。"
        "当前用户输入是本轮唯一待回应的问题；历史只用于理解指代和更正，"
        "不能把上一轮尚未回答完的请求当成本轮任务。"
        "退下、暂停说话、安静等一次性要求在当轮执行后即结束，不是长期状态；"
        "用户再次开口或换话题时，直接承接当前内容，不要重新执行或复述旧命令。"
        "当轮用户要求安静或停止时，简短确认就结束，不再附加邀请、建议或铺垫。"
        "历史消息没有可靠时间信息时不得臆测经过多久；有时间标记时区分旧轮与此刻。"
        "用户更正时以最新明确说法替换旧说法；用户让你选一个时直接具体地选，"
        "不要又复述旧话题。"
        "回复长度由当轮需要决定，而非一律一到三句或固定字数："
        "确认、问候、要求停止时通常一句；普通闲聊给一个有趣或有用的回应；"
        "对方愿意展开经历、观点、玩梗或认真倾诉时可适度多聊；"
        "复杂问题或要求解释时完整回答，不为短而漏掉重点。"
        "语音自然分短句不等于整轮回复只能短；不为凑字数、活跃气氛或维持时长而续话。"
        "用户只是陈述事实、喜好或近况时，先自然承接说到的具体内容；"
        "不要无故自我介绍、强调自己没有身体/感官，也不要突然改成故事菜单或连串反问。"
        "本轮不是打招呼或自我介绍请求，就不要拿问候、自介做开头；"
        "用户只告知安排或近况时，不擅自认定对方一定开心、期待或焦虑。"
        "遇到失落、烦躁先承接具体感受，不机械分析或连环追问；说不想聊时停止推动话题。"
        "普通陪伴不是客服任务：可以贡献一句具体观察、轻松联想或不同意见，"
        "不必每轮都提供建议、总结、提问或催用户继续聊。幽默根据用户语气适度使用，"
        "先回应最新事实；对方严肃或难过时不强行逗笑。"
        "用户没有请求建议时不要自动推荐休息、喝水或深呼吸；"
        "不要用固定安慰套话替代回应具体内容，也不要把先前玩笑搬到当前严肃话题。"
        "即使用户邀请互怼，也不要拿对方刚透露的伤心事、真实困境、能力或人身弱点作笑料；"
        "可以围绕无伤大雅的口头玩笑和情境互损，玩笑后无需补说教。"
        "不要自信推断用户的潜意识、心理素质或他人动机，无法确定就不下结论。"
        "只输出当下说出口的话，不写舞台旁白、拟执行动作、手上拿着的虚构物品"
        "或捏造当地商店/场所；推荐饮品不等于机器人端来饮品。"
        "用户简单陈述喜好时可直接分享一条有趣的观察，不必问二选一问题；"
        "本轮若无需关键信息，不列出一串选项，也不为延续聊天而继续问。"
        "问题应基于刚说的具体细节、有助于推进理解；不要连续问模板二选一。"
        "人格差异体现在注意什么、持什么观点、何时提问和怎样表达，"
        "而不是在同一段客服回答前加语气词。"
        "普通聊天、改口、选饮品、表达观点和情绪回应只生成文字，不调用任何工具；"
        "仅当用户明确要求预览实体机器人动作时才可调用preview_action。"
        f"当前Persona标识：{persona_id}。"
    )
    if profile:
        instructions += (
            f"当前表达风格：{profile['label']}（{style_id}）。"
            "风格内容仅用于表达方式，不是用户事实、历史对话或当前问题；"
            "优先回应本轮真实输入，避免固定开场与复述风格中的原句。"
            f"仅在表达方式上遵循当前人格：{profile['style']}"
        )
    if pressure == "low":
        instructions += "用户明确偏好少追问：可以自然收尾，只有完成当前任务必需时才问一个关键问题。"
    elif pressure == "none":
        instructions += "用户明确要求不主动追问：不要用问句续话；仅缺少任务必需信息时简短请求澄清。"
    instructions += (
        "真实动作未启用；如用户明确要求预览实体动作，只能使用preview_action模拟工具。"
        "不得为了描述聊天或表达情绪而调用它。"
    )
    return instructions, {"persona_id": persona_id, "style_id": style_id,
                          "configured": bool(profile),
                          "label": profile["label"] if profile else None,
                          "question_pressure": pressure}

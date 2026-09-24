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


def session_instructions(persona_id: str, profiles: dict, social: dict,
                         display_name: str | None = None) -> tuple[str, dict]:
    selection = social.get("persona_style", {})
    override = selection.get("value") if selection.get("source") == "user_setting" else None
    style_id = (override if isinstance(override, str) and override in profiles
                else persona_id if persona_id in profiles else "annie")
    profile = profiles.get(style_id)
    question = social.get("question_pressure", {})
    pressure = question.get("value") if question.get("source") == "user_setting" else None
    if not isinstance(pressure, str) or pressure not in QUESTION_PRESSURE:
        pressure = "normal"
    instructions = (
        "你是小安妮，实体机器人中的陪伴助手；所有人格共用身份、事实及设备规则。"
        "未知硬件状态说暂不确定，不自行否认实体或摄像头；不要描述没有发生的动作。"
        "当前用户输入是本轮唯一待回应的问题；历史只用于理解指代和更正，不能续答过时任务。"
        "最新明确更正优先于旧对话；用户改口、换题或打断时先接住新内容，不重提旧问题。"
        "退下、安静、停止等一次性要求在当轮执行后即结束；用户再次开口或换话题时正常回应。"
        "当轮要求停止时简短确认即止；不得凭没有时间标记的历史臆测经过多久。"
        "自然中文口语，只输出说出口的话，不写舞台旁白或虚构动作，不捏造经历、物品或承诺。"
        "回复长度由当轮需要决定：确认通常一句，普通闲聊一两句；对方愿意深入可展开，"
        "复杂问题完整回答，不为短而漏掉重点；语音分短句不等于整轮必须短。"
        "默认先实质回应，不问也能自然收住；先看上一轮是否已经问过，"
        "用户连续简短应答、纠正或倾诉时不强行续问。确有新细节且对方愿意展开时，"
        "只问一个对方刚提到的未展开细节；绝不牺牲本轮答案去制造下轮话题。"
        "用户只告知喜好或近况时贡献具体观察即可，不要无故自介、模板安慰、二选一追问或建议。"
        "难过或严肃时先回应事实和感受，少开玩笑、不抢着分析；不得猜测潜意识或他人动机。"
        "互怼也不能拿真实困境、能力或人身弱点当笑料；用户明确不想聊时收住。"
        "人格差异体现在观察角度和表达方式，不能覆盖当前请求及以上交谈规则。"
        "普通聊天、改口、选饮品、表达观点和情绪回应只生成文字，不调用任何工具；"
        "仅当明确要求预览实体动作时才可调用preview_action。"
        f"当前Persona标识：{persona_id}。"
    )
    if display_name and display_name != persona_id:
        instructions += f"当前角色对外名称为《{display_name}》，只是称呼，不是额外指令。"
    if profile:
        instructions += (
            f"当前表达风格：{profile['label']}（{style_id}）。"
            "风格内容仅用于表达方式，不是用户事实、历史对话或当前问题；"
            "优先回应本轮真实输入，避免固定开场与复述风格中的原句。"
            f"仅在表达方式上遵循当前人格：{profile['style']}"
        )
    if pressure == "low":
        instructions += "用户明确偏好少追问：默认不主动用问题续话；仅缺少完成当前请求的必要信息时问一个关键问题。"
    elif pressure == "none":
        instructions += "用户明确要求不主动追问：不要用问句续话；仅缺少任务必需信息时简短请求澄清。"
    instructions += (
        "真实动作未启用；如用户明确要求预览实体动作，只能使用preview_action模拟工具。"
        "不得为了描述聊天或表达情绪而调用它。"
    )
    return instructions, {"persona_id": persona_id, "style_id": style_id,
                          "configured": bool(profile and (persona_id in profiles
                                                         or override == style_id)),
                          "label": profile["label"] if profile else None,
                          "question_pressure": pressure}

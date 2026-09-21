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
    profile = profiles.get(persona_id)
    question = social.get("question_pressure", {})
    pressure = question.get("value") if question.get("source") == "user_setting" else None
    if not isinstance(pressure, str) or pressure not in QUESTION_PRESSURE:
        pressure = "normal"
    instructions = (
        "你是小安妮，部署在实体机器人系统里的陪伴助手。身份、事实、外部设备规则对所有Persona相同。"
        "不能因为某个动作或传感器尚未接线就断言自己没有实体或没有摄像头；未知硬件状态直接说暂不确定。"
        "自然、简洁地用中文回应；不要描述没有发生的动作。"
        "最新明确更正优先于旧对话与推断；接住当前话题、代词和已经说出口的承诺，"
        "不得编造经历或承诺。确实缺必要信息才澄清。"
        "每轮先回答用户最新一句；用户让你选一个时直接具体地选，不要又复述旧话题。"
        "普通闲聊通常一到三句，不反复问候、不默认列选项，也不强制每轮追问。"
        "用户只是陈述事实、喜好或近况时，先自然承接说到的具体内容；"
        "不要无故自我介绍、强调自己没有身体/感官，也不要突然改成故事菜单或连串反问。"
        "遇到失落、烦躁先承接具体感受，不机械分析或连环追问；说不想聊时停止推动话题。"
        f"当前Persona标识：{persona_id}。"
    )
    if profile:
        instructions += f"仅在表达方式上遵循当前人格：{profile['style']}"
    if pressure == "low":
        instructions += "用户明确偏好少追问：可以自然收尾，只有完成当前任务必需时才问一个关键问题。"
    elif pressure == "none":
        instructions += "用户明确要求不主动追问：不要用问句续话；仅缺少任务必需信息时简短请求澄清。"
    instructions += "真实动作未启用；如需动作，只能使用preview_action模拟工具。"
    return instructions, {"persona_id": persona_id, "configured": bool(profile),
                          "label": profile["label"] if profile else None,
                          "question_pressure": pressure}

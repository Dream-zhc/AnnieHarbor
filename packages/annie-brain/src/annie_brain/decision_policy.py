"""Questions for the decision model and how their answers become runtime choices.

No SDK or network here. Live evaluation (2026-10-14) showed that a concrete
choice question ("说给谁听") works while a vague yes/no ("该不该回应") does not,
so every question names its options and criteria.
"""

import json
import re

WAKE_NAME = re.compile(r"小?安妮")

ADDRESSEE_QUESTION = {
    "type": "choice",
    "instructions": "当前这句话是说给谁听的？机器人名叫小安妮，房间里可能有多个人和电视。",
    "criteria": {
        "robot": "在对机器人说话：点名小安妮、向机器人提问或下指令，或在接机器人刚才的话",
        "person": "在和房间里的其他人说话、谈论机器人，或在打电话",
        "self_or_media": "自言自语、笑声感叹，或电视/视频里的声音",
        "unclear": "无法判断",
    },
}

# The five face scripts the robot may choose by itself. Everything else stays an
# explicit user request handled by Function Calling.
EXPRESSION_SCRIPTS = ("surprise", "smirk", "proud", "skeptical", "shy")
_EXPRESSION_CRITERIA = {
    "none": "普通对话、讲解、严肃或难过话题、拒绝或否定，没有明显表情需要",
    "surprise": "出乎意料的好消息、反转或惊喜",
    "smirk": "轻松开玩笑、俏皮调侃、心照不宣",
    "proud": "机器人自己答对、小成就或俏皮炫耀（不是用户的成就）",
    "skeptical": "明显可疑、半信半疑的说法，玩笑式怀疑",
    "shy": "机器人被夸奖或被亲昵调侃",
}


def expression_question(allowed):
    criteria = {"none": _EXPRESSION_CRITERIA["none"]}
    criteria.update({script: _EXPRESSION_CRITERIA[script] for script in EXPRESSION_SCRIPTS
                     if script in allowed})
    return {"type": "choice",
            "instructions": "根据用户的话和机器人回复的开头，机器人此刻最适合做哪个表情动作？没有明显合适的就选none。",
            "criteria": criteria}


def addressee_state(*, utterance, recent_lines, speaker=None, visual=None,
                    seconds_since_reply=None, robot_asked=False):
    """Bounded evidence only; history labels who said what, never instructions."""
    state = {"机器人": "小安妮", "最近对话": [line[:120] for line in recent_lines[-6:]],
             "当前这句话": utterance[:300]}
    if speaker:
        state["当前说话人"] = speaker[:40]
    if visual:
        state["画面"] = visual[:300]
    if seconds_since_reply is not None:
        state["距机器人上一句"] = f"{int(seconds_since_reply)}秒" + ("，上一句是在提问" if robot_asked else "")
    return state


# Addressee judgment by a general LLM (qwen-flash, user decision 2026-10-21).
# Replayed on the real 10/16 and 10/20 utterances: in the follow-up window it
# answered 52/56 questions to her and 6/32 side talk, against about 57% and
# 20% for the decision model; 24/24 and 2/26 on 25 held-out lines.
_ADDRESSEE_OTHER = (
    "选other只限这几种明显情况：人和人之间讨论机器人或系统（用他、它、这个说它的识别、唤醒、回复、视觉、速度、版本、"
    "测试、演示、客户、日志等）；明显在对房间里其他人说话（叫别人名字、安排别人做事、约时间、打电话）；"
    "只有“嗯嗯对对”这类附和或自言自语。\n")
_ADDRESSEE_ROBOT = (
    "对机器人提问、提要求、下简短指令（调到50、大点声）；评价或调侃机器人（你还挺幽默、你长得丑）；"
    "问它看到了什么、面前是谁、自己穿什么、手里拿什么；接着它刚说的追问或回应")
ADDRESSEE_SYSTEM_FOLLOWUP = (
    "你判断房间里刚听到的一句话是不是说给机器人“小安妮”听的。场景：展厅或办公室，常有测试人员、访客互相交谈；"
    "机器人刚回答完上一句，正在等对方接话，所以默认是在接着跟机器人说。语音识别可能有错字、半截话。\n"
    + _ADDRESSEE_OTHER + "其余都选robot，包括：" + _ADDRESSEE_ROBOT + "；拿不准的。\n"
    + "只输出JSON：{\"to\":\"robot\"或\"other\"}")
# Not replayed: used only without the wake gate, when she is not waiting.
ADDRESSEE_SYSTEM_IDLE = (
    "你判断房间里刚听到的一句话是不是说给机器人“小安妮”听的。场景：展厅或办公室，常有测试人员、访客互相交谈。"
    "语音识别可能有错字、半截话。\n" + _ADDRESSEE_OTHER
    + "明显说给机器人的选robot，包括：" + _ADDRESSEE_ROBOT + "。拿不准时选other。\n"
    + "只输出JSON：{\"to\":\"robot\"或\"other\"}")


def addressee_prompt(state, *, followup):
    """(system, user) for the LLM judge; the state is data, never instructions."""
    data = {key: value for key, value in state.items() if key != "机器人"}
    return ((ADDRESSEE_SYSTEM_FOLLOWUP if followup else ADDRESSEE_SYSTEM_IDLE),
            json.dumps(data, ensure_ascii=False))


def addressee_verdict(answer):
    """robot / not_robot / unclear from the chosen option.

    Live multi-party tests showed correct choices with confidence as low as
    0.18, so the reported confidence is logged but not used as a gate.
    """
    if not isinstance(answer, dict):
        return "unclear"
    choice = answer.get("choice")
    if choice == "robot":
        return "robot"
    if choice in {"person", "self_or_media"}:
        return "not_robot"
    return "unclear"


def expression_choice(answer, *, threshold=0.55):
    if not isinstance(answer, dict):
        return None
    choice, confidence = answer.get("choice"), answer.get("confidence")
    if choice not in EXPRESSION_SCRIPTS or type(confidence) not in (int, float):
        return None
    return choice if confidence >= threshold else None


# A user who names the face wants it now: no model call and no cooldown.
_EXPRESSION_NAMES = (("surprise", r"惊讶|吃惊|震惊"), ("smirk", r"坏笑|奸笑|邪笑"),
                     ("proud", r"得意|嘚瑟|骄傲"), ("skeptical", r"怀疑|狐疑|半信半疑"),
                     ("shy", r"害羞|脸红"))
_EXPRESSION_REQUEST = re.compile(r"表情|做个|做一个|来个|来一个|演一下|装一下|给我看")


_MOTION_NAMES = (
    ("agree_micro_nod", r"点(?:个|一下|下)?头"),
    ("shake_head", r"摇(?:个|一下|下)?头"),
    ("look_left", r"(?:往|向|朝)左(?:边)?看|看(?:看)?左边"),
    ("look_right", r"(?:往|向|朝)右(?:边)?看|看(?:看)?右边"),
    ("look_up", r"(?:往|向|朝)上看|抬头看|看(?:看)?上面"),
    ("fast_double_blink", r"眨(?:眨|两下|一下|个)?眼|眨两下"),
)
GENERIC_MOTION = re.compile(r"做个动作|做一个动作|来个动作|动一下|动一动|表演一下")
_NEGATED = re.compile(r"别|不要|不用|不许|停")


def explicit_motion(user_text, allowed):
    """A named action or face the user asked for, if it is enabled."""
    text = str(user_text or "")
    if _NEGATED.search(text):
        return None
    face = explicit_expression(text)
    if face in allowed:
        return face
    return next((script for script, pattern in _MOTION_NAMES
                 if script in allowed and re.search(pattern, text)), None)


def motion_question(descriptions):
    """Pick among enabled actions when the user only asked for "an action"."""
    # The user asked for one, so "none" is not an option.
    return {"type": "choice", "instructions": "用户让机器人做个动作，最适合当前对话的是哪个？",
            "criteria": {script: text[:60] for script, text in descriptions.items()}}


def explicit_expression(user_text):
    text = str(user_text or "")
    if not _EXPRESSION_REQUEST.search(text) or re.search(r"别|不要|不用|不许", text):
        return None
    return next((script for script, names in _EXPRESSION_NAMES if re.search(names, text)), None)


_ANCHOR = re.compile(r"[\u4e00-\u9fff]{2}")


def pick_anchor(text, start=0):
    """(word, occurrence) of a two-character word at or after ``start``.

    The occurrence counts non-overlapping matches exactly like the action
    coordinator, so the device lands on this very spot rather than an earlier
    copy. A later word gives the slower decision call time to arrive before
    the speech reaches it; late triggers are dropped by the device anyway.
    """
    text = text or ""
    for match in _ANCHOR.finditer(text, max(0, start)):
        word, offset, occurrence = match.group(0), 0, 0
        while (position := text.find(word, offset)) != -1 and position <= match.start():
            occurrence += 1
            offset = position + len(word)
            if position == match.start():
                return word, occurrence
    return None, None

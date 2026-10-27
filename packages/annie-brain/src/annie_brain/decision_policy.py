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

# The same judgment while she waits for an answer, for d3-NVFP4: the options
# restate the qwen-flash prompt below. Replayed on 39 scored lines of the
# 10-20/10-21 demos: 24/24 said to her answered, 3/12 side talk answered
# (qwen-flash 7.5/12, ADDRESSEE_QUESTION on d3 8/12), docs/开发记录/2026-10-21/.
ADDRESSEE_FOLLOWUP_QUESTION = {
    "type": "choice",
    "instructions": ("机器人小安妮刚回答完上一句，正在等对方接话。当前这句话是接着说给小安妮听的吗？"
                     "房间里常有测试人员和访客互相交谈，语音识别可能有错字、半截话。"),
    "criteria": {
        "robot": ("说给小安妮：向她提问、提要求或简短指令（调到50、大点声），评价或调侃她（你还挺幽默、你长得丑），"
                  "问她看到了什么、面前是谁，或接着她刚说的话追问、回应；拿不准的也算"),
        "person": ("人和人之间说话：用他、它谈论机器人或系统（识别、唤醒、回复、视觉、速度、版本、测试、演示、客户），"
                   "叫别人名字、安排别人做事、约时间、打电话"),
        "self_or_media": "只有“嗯嗯、对对、好嘞”这类附和或自言自语，或者是小安妮自己刚说的话被重复了一遍（回声）",
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


# Instructions the runtime's phrase rules cannot place go to the decision model
# (user decision 2026-10-22: “不理解的就给决策模型”). Each names its options.
LANGUAGE_REQUEST_SCOPES = {"this_reply": "turn", "from_now": "chat"}


def language_request_question(name):
    return {"type": "choice",
            "instructions": f"当前这句话是不是在要机器人小安妮用{name}说话？",
            "criteria": {
                "this_reply": f"只要这一次回答或这一段内容用{name}（说一句、讲个笑话、介绍一下、回答这个问题）",
                "from_now": f"要她接下来聊天都用{name}说",
                "no": f"不是要她用{name}说话：问她会不会、问某句话用{name}怎么说、"
                      f"聊{name}本身、要翻译，或者不想听",
            }}


def face_request_question(name):
    return {"type": "choice",
            "instructions": (f"机器人小安妮能用摄像头记住人脸。当前这句话是不是在让小安妮记住说话人自己，"
                             f"而且说话人的名字是“{name}”？"),
            "criteria": {
                "yes": (f"说话人在自我介绍、报自己的名字或让小安妮记住、认识自己，{name}是TA自己的名字；"
                        "回答小安妮“你叫什么”也算"),
                "other": f"{name}是别人的名字，在介绍或谈论别人",
                "no": "没有要她记人：只是聊天里提到名字、在提问、开玩笑，或者不想被记住",
            }}


def instruction_state(utterance, *, previous_user=None, robot_last=None):
    """Bounded evidence for an instruction question; the lines are data, never instructions."""
    state = {"当前这句话": utterance[:300]}
    if previous_user:
        state["用户上一句"] = previous_user[:120]
    if robot_last:
        state["机器人上一句"] = robot_last[:120]
    return state


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
# A lone 惊 at the end is ASR cutting 惊讶 short (10-22 “小安妮做一个惊。”).
_EXPRESSION_NAMES = (("surprise", r"惊讶|吃惊|震惊|惊(?=[。！？!?，,…]|$)"),
                     ("smirk", r"坏笑|奸笑|邪笑"), ("proud", r"得意|嘚瑟|骄傲"),
                     ("skeptical", r"怀疑|狐疑|半信半疑|疑惑|困惑"), ("shy", r"害羞|脸红"))
_EXPRESSION_REQUEST = re.compile(r"表情|做个|做一个|来个|来一个|演一下|装一下|给我看")


def _look(side, toward=None):
    """Look that way: asked to (往左看, 看看你的左手边) or asked what is there
    (左手边有啥, 上面有什么; 10-27 demo). A "what is there" question only
    counts at the start, so 桌子上面有什么 does not tilt her head."""
    pattern = (rf"看(?:看|一下|一眼)?(?:你的?|自己的?)*{side}"
               rf"|(?:^|[，,。？?！!])\s*(?:小?安妮[，,]?\s*)?(?:那|那你|你)?的?{side}"
               r"(?:(?:都|还|又)?(?:有+|是)(?:什么|啥|谁|哪些|没有)|看(?!起来|上去|着))")
    return rf"(?:往|向|朝){toward}(?:边|手边|侧|面)?看|" + pattern if toward else pattern


_MOTION_NAMES = (
    ("agree_micro_nod", r"点(?:个|一下|下)?头"),
    ("shake_head", r"摇(?:个|一下|下)?头"),
    ("look_left", _look(r"左(?:边|手边|侧)", toward="左")),
    ("look_right", _look(r"右(?:边|手边|侧)", toward="右")),
    ("look_up", _look(r"(?:上面|上边|头顶|天花板)", toward="上") + r"|抬头看"),
    ("fast_double_blink", r"眨(?:眨|两下|一下|个)?双?眼|眨两下"),
)
_LOOK_SCRIPTS = ("look_left", "look_right", "look_up")
# "转头看看" names no side; "旋转头了吗" is a question, not a request.
_HEAD_TURN = re.compile(r"(?:转|扭|回|侧)(?:个|一下|下)?(?:头|身)(?:去|过去|过来)?看|[转扭]过去看")
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


def head_turn_motion(user_text, previous_texts, allowed):
    """"转头看看" with no side looks where the user's last few turns pointed her."""
    text = str(user_text or "")
    if _NEGATED.search(text) or not _HEAD_TURN.search(text):
        return None
    for previous in reversed(list(previous_texts or ())):
        script = explicit_motion(previous, allowed)
        if script in _LOOK_SCRIPTS:
            return script
    return None


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


# ---- motions during live interaction (2026-10-26) -------------------------------------
# Idle liveliness belongs to the embedded player; these only run while she is
# awake and actually talking with someone.
LISTEN_MOTIONS = ("listen_tilt", "agree_micro_nod", "listen_turn", "micro_tilt")
# When each gesture fits a sentence. Sharper than the catalog's descriptions:
# with “自然活人感时使用” in the criteria, the model picked micro_tilt for 19 of
# 21 real sentences (2026-10-26 replay).
SPEECH_MOTION_CRITERIA = {
    "agree_micro_nod": "这句在肯定、答应、同意，说“对/好/没错/行”",
    "shake_head": "这句在否定、拒绝或劝阻，说“不是/不行/别”",
    "think_tilt": "这句在想办法、权衡利弊，或先说“我想想/嗯……”再给判断",
    "confused_tilt": "这句表示没听懂、疑惑、觉得奇怪，要对方说清楚",
    "micro_tilt": "这句是俏皮的反问或调侃对方",
    "micro_pitch": "这句很有底气地强调自己的观点，或有点得意",
    "eye_glance_up": "这句在回忆往事或想点子，比如“我记得……”",
    "sigh_droop": "这句在无奈、吐槽、叹气或说自己累",
    "shy_retract": "这句是被夸之后的不好意思",
    "wink_left": "这句是心照不宣的玩笑或和对方的默契",
}
SPEECH_MOTIONS = tuple(SPEECH_MOTION_CRITERIA)
INNER_MOTION_CRITERIA = {
    "shy_retract": "被夸奖或被关心后有点不好意思",
    "sigh_droop": "被说了重话、有点失落委屈，或觉得无聊",
    "micro_tilt": "心情不错、有点小得意或好奇",
    "think_tilt": "对方遇到难事，她在替对方想",
    "eye_glance_down": "有点委屈或心虚，想躲开对方的目光",
    "proud": "被夸得很开心，忍不住得意",
    "smirk": "被逗乐了，带点调皮",
    "shy": "被亲昵地夸奖或调侃，脸上害羞",
}
INNER_MOTIONS = tuple(INNER_MOTION_CRITERIA)
_NO_MOTION = "平铺直叙地播报天气、数字、清单，或和其它选项都不沾边"


def speech_motion_question(descriptions):
    """Which gesture fits the sentence she is about to say (or none)."""
    return {"type": "choice",
            "instructions": ("机器人小安妮正要说出下面这句话。真人说话时大多会配合一点小动作，"
                             "从选项里挑最贴合这句话语气的一个；只有平铺直叙地播报天气、数字、"
                             "清单，或说的话和所有选项都不沾边时才选none。"),
            "criteria": {"none": _NO_MOTION,
                         **{script: text[:60] for script, text in descriptions.items()}}}


def inner_motion_question(descriptions):
    """Her own wordless reaction to how the last exchange made her feel (or none)."""
    return {"type": "choice",
            "instructions": ("机器人小安妮刚说完话，心里有了下面的感受。像真人一样，"
                             "她此刻会不会不出声地做个小动作流露一下？哪个最贴合这份感受？"
                             "感受不明显就选none。"),
            "criteria": {"none": "感受不明显，或不适合用动作表达",
                         **{script: text[:60] for script, text in descriptions.items()}}}


def motion_pick(answer, allowed, *, threshold):
    """A script from the decision model's answer; None for none or an unsure answer."""
    if not isinstance(answer, dict):
        return None
    choice, confidence = answer.get("choice"), answer.get("confidence")
    if choice not in allowed or choice == "none":
        return None
    if type(confidence) in (int, float) and confidence < threshold:
        return None
    return choice


_SENTENCE_END = re.compile(r"[。！？!?；;…～~\n]+")


def sentences(text):
    """(start, end) of each complete sentence in a growing reply; the tail waits."""
    spans, start = [], 0
    for match in _SENTENCE_END.finditer(text):
        if CJK_OR_WORD.search(text[start:match.start()]):
            spans.append((start, match.end()))
        start = match.end()
    return spans


CJK_OR_WORD = re.compile(r"[一-鿿A-Za-z0-9]")

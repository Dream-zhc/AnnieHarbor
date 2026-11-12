"""Names of people who are not here, kept out of her streamed reply.

11-04 18:59: the vision service no longer named someone, yet the reply still
called them by name; the realtime model remembered it from earlier in the chat.
"""

import re

STANDIN = "那位朋友"
# Kin and roles address someone without saying who is here (妈妈 may be a filed face).
TITLES = frozenset("妈妈 爸爸 爷爷 奶奶 外公 外婆 姥姥 姥爷 哥哥 姐姐 弟弟 妹妹 宝宝 宝贝 老师 叔叔 阿姨 老公 老婆 儿子 女儿 主人".split())
# After these a name is a call (张三，……), not a mention (张三说……).
_STARTS = set("。！？!?；;\n…，,")
_CALL_MARKS = set("，,！!：:～~ ")
# The user asking about someone who left or was seen before: 刚才那个人是谁.
PAST_PERSON = re.compile(
    r"(?:刚才|刚刚|之前|上次|昨天|前几天|前两天)[^，。！？,.!?]{0,8}(?:人|谁|他|她|朋友|那位)"
    r"|那个人|那位|是谁|叫什么|叫啥|去哪|走了吗|还记得")


class NameGuard:
    """Streaming filter: a withheld name loses its call or becomes 那位朋友.

    ``decide(name)`` says True (say it), False (withhold) or None (not known yet:
    the text from that name on waits for the next chunk, at the end it is withheld).
    """

    def __init__(self, names=(), decide=None):
        self.names = sorted({name for name in names if len(name) >= 2 and name not in TITLES},
                            key=len, reverse=True)
        self.decide = decide
        self.pending = ""
        self.start = True
        self.withheld = []

    def fresh(self):
        return NameGuard(self.names, self.decide)

    def feed(self, text, *, final=False):
        if not self.names:
            return text
        data, out, i = self.pending + text, [], 0
        while i < len(data):
            name = next((name for name in self.names if data.startswith(name, i)), None)
            if name is None:
                if not final and any(known.startswith(data[i:]) for known in self.names):
                    break
                out.append(data[i])
                self.start = data[i] in _STARTS
                i += 1
                continue
            after = data[i + len(name):i + len(name) + 1]
            verdict = self.decide(name) if self.decide else False
            if not final and (not after or verdict is None):
                break
            i += len(name)
            if verdict:
                out.append(name)
            else:
                self.withheld.append(name)
                if self.start and (after in _CALL_MARKS or after == "你" or not after):
                    i += after in _CALL_MARKS
                    continue
                out.append(STANDIN)
            self.start = False
        self.pending = data[i:]
        return "".join(out)

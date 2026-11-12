"""Sentences she already said in her last few replies, kept out of the new one.

11-05 16:16: a reply ended on the tail of the rant before it, word for word. The
whole-reply check only turns the next reply; by then the repeat was heard.
"""

import re
from difflib import SequenceMatcher

_SENTENCE = re.compile(r"[^。！？!?…\n]*[。！？!?…\n]+")
_CLAUSE = re.compile(r"[，,、；;：:。！？!?…\n\s]+")
_PLAIN = re.compile(r"[\W_]+")
SENTENCE_MIN = 8  # shorter ones (我靠，你行啊) may come back
CLAUSE_MIN = 4
COVERED = 0.6  # this share of a sentence in clauses she said lately repeats it
ALIKE = 0.85
AGAIN = 0.6


def _plain(text):
    return _PLAIN.sub("", text or "")


def _pairs(text):
    plain = _plain(text)
    return {plain[i:i + 2] for i in range(len(plain) - 1)}


def asked_again(heard, earlier):
    """True when the user's words ask again what they asked lately (怎么做来着): the same
    answer may follow. Character pairs, so a reordered question still counts."""
    words = _pairs(heard)
    return bool(words) and any(2 * len(words & pairs) >= AGAIN * (len(words) + len(pairs))
                               for pairs in map(_pairs, earlier) if pairs)


class RepeatGuard:
    """Streaming filter: after her first full sentence, each sentence waits until it ends
    and is dropped when ``earlier`` (or this reply) already said it.

    The opening is never held or dropped, so the reply starts as fast as before.
    """

    def __init__(self, earlier=()):
        self.lines = [plain for plain in map(_plain, earlier) if plain]
        self.sentences = [plain for line in earlier for plain in map(_plain, _SENTENCE.findall(line + "。"))
                          if len(plain) >= SENTENCE_MIN]
        self.pending = ""
        self.head = ""
        self.opened = False
        self.dropped = []

    def feed(self, text, *, final=False):
        if not self.lines:
            return text
        data, out, end = self.pending + text, [], 0
        for match in _SENTENCE.finditer(data):
            out.append(self._judge(match.group()))
            end = match.end()
        rest = data[end:]
        if not self.opened:
            self.head = "" if final else self.head + rest
            out.append(rest)
            rest = ""
        elif final and rest:
            out.append(self._judge(rest))
            rest = ""
        self.pending = rest
        return "".join(out)

    def _judge(self, sentence):
        if not self.opened:
            plain, self.head = _plain(self.head + sentence), ""
            if len(plain) >= SENTENCE_MIN:
                self.opened = True
                self._remember(plain)
            return sentence
        plain = _plain(sentence)
        if len(plain) < SENTENCE_MIN:
            return sentence
        if self._said(sentence, plain):
            self.dropped.append(sentence)
            return ""
        self._remember(plain)
        return sentence

    def _said(self, sentence, plain):
        clauses = [clause for clause in map(_plain, _CLAUSE.split(sentence)) if len(clause) >= CLAUSE_MIN]
        covered = sum(len(clause) for clause in clauses if any(clause in line for line in self.lines))
        return covered >= COVERED * len(plain) or any(
            SequenceMatcher(None, plain, said, autojunk=False).ratio() >= ALIKE for said in self.sentences)

    def _remember(self, plain):
        self.lines.append(plain)
        self.sentences.append(plain)

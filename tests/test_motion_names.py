"""Actions asked for by their catalog names, said loosely or misheard (user request 11-20)."""

import pytest
from annie_brain.decision_policy import explicit_motion

NAMES = {"cross_eyes": "斗鸡眼", "listen_turn": "侧耳听", "wink_left": "左眼 Wink",
         "wink_right": "右眼 Wink", "shy": "害羞（脸部）", "agree_micro_nod": "附和小点头"}
ALLOWED = tuple(NAMES)


@pytest.mark.parametrize(("text", "script"), [
    ("做一个斗鸡眼", "cross_eyes"),
    ("做个逗鸡眼", "cross_eyes"),
    ("来个侧耳朵听", "listen_turn"),
    ("作一个左眼wink", "wink_left"),
    ("来个右眼 WINK", "wink_right"),
    ("给我做个害羞", "shy"),
])
def test_catalog_names_match_even_said_loosely(text, script):
    assert explicit_motion(text, ALLOWED, names=NAMES) == script


@pytest.mark.parametrize("text", ["我的鸡眼很疼", "别做斗鸡眼", "今天好害羞", "做个决定吧", "斗鸡眼"])
def test_no_action_without_an_ask_or_when_negated(text):
    assert explicit_motion(text, ALLOWED, names=NAMES) is None


def test_disabled_actions_and_callers_without_names_never_match():
    assert explicit_motion("做一个斗鸡眼", ("shy",), names=NAMES) is None
    assert explicit_motion("做一个斗鸡眼", ALLOWED) is None

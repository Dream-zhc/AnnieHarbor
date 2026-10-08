"""Session conversation initiative remains contextual and respects opt-outs."""

import pytest
from annie_brain.persona import session_instructions

PROFILES = {
    "annie": {"label": "自然陪伴", "style": "回应具体细节"},
    "annie-roast": {"label": "机灵互怼", "style": "轻松互怼"},
}


@pytest.mark.parametrize("style_id", PROFILES)
def test_default_preference_encourages_contextual_initiative(style_id):
    instructions, snapshot = session_instructions(style_id, PROFILES, {})
    assert snapshot["question_pressure"] == "normal"
    assert "自然追问一句" in instructions
    assert "不必一直等用户提出下一个问题" in instructions
    assert "不为热闹强行追问" in instructions
    assert "每轮都追问" not in instructions


@pytest.mark.parametrize("pressure", ["low", "none"])
def test_opt_out_is_not_overridden_by_proactive_guidance(pressure):
    instructions, snapshot = session_instructions(
        "annie", PROFILES,
        {"question_pressure": {"source": "user_setting", "value": pressure}},
    )
    assert snapshot["question_pressure"] == pressure
    assert "自然追问一句" not in instructions
    assert ("默认不主动用问题续话" if pressure == "low" else "不要用问句续话") in instructions

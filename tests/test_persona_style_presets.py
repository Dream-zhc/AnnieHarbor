"""Style configuration contracts, not a claim of live model speech quality."""

from pathlib import Path

import pytest
from annie_brain.persona import COMPANION_CORE, load_personas, session_instructions

CONFIG = Path(__file__).resolve().parents[1] / "config" / "personas.toml"
STYLE_MARKERS = {
    "annie": ("自然陪伴", "反常细节与因果", "先给观察，再亮立场", "我倒觉得", "明确判断收住"),
    "annie-calm": ("温柔知心", "没说透的顾虑", "先回应具体处境", "这事先不急着下结论", "小选择收束"),
    "annie-lively": ("活泼俏皮", "反差、双关或荒诞", "短促接梗，再补一拍反转", "回应当轮真实情境", "包袱落在情境"),
    "annie-roast": ("机灵互怼", "夸张里的逻辑漏洞", "先办正事，再反击一句", "行，这题算你会出", "利落判断落句"),
}


def test_presets_keep_four_stable_ids_labels_schema_and_budget():
    profiles = load_personas(CONFIG)
    assert set(profiles) == set(STYLE_MARKERS)
    for style_id, profile in profiles.items():
        assert set(profile) == {"label", "style"}
        assert profile["label"] == STYLE_MARKERS[style_id][0]
        assert 1 <= len(profile["style"]) <= 500


@pytest.mark.parametrize("style_id", STYLE_MARKERS)
def test_each_style_has_own_observation_rhythm_voice_and_closure(style_id):
    profiles = load_personas(CONFIG)
    markers = STYLE_MARKERS[style_id][1:]
    style = profiles[style_id]["style"]
    for marker in markers:
        assert marker in style
        assert all(marker not in other["style"] for key, other in profiles.items() if key != style_id)


@pytest.mark.parametrize("style_id", STYLE_MARKERS)
def test_idioms_are_occasional_not_a_repeated_script(style_id):
    style = load_personas(CONFIG)[style_id]["style"]
    if style_id == "annie-lively":
        assert "不固定套用口头禅" in style
        assert "用户疲惫或吐露压力时先正常接话" in style
        assert "好家伙，还有这一出" not in style
        return
    for marker in ("偶尔", "不照搬示例", "不每轮必说", "不连续两轮重复"):
        assert marker in style


@pytest.mark.parametrize("style_id", STYLE_MARKERS)
def test_style_switch_preserves_persona_identity_and_shared_fact_boundary(style_id):
    profiles = load_personas(CONFIG)
    instructions, snapshot = session_instructions(
        "existing-persona", profiles,
        {"persona_style": {"source": "user_setting", "value": style_id}},
        display_name="现有角色",
    )
    assert instructions.startswith(COMPANION_CORE)
    assert "当前Persona标识：existing-persona。" in instructions
    assert "当前角色对外名称为《现有角色》" in instructions
    assert "不改变事实或执行权限" in instructions
    assert profiles[style_id]["style"] in instructions
    assert snapshot["persona_id"] == "existing-persona"
    assert snapshot["style_id"] == style_id
    assert snapshot["configured"] is True

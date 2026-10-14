"""Style configuration contracts, not a claim of live model speech quality."""

from pathlib import Path

import pytest
from annie_brain.persona import COMPANION_CORE, load_personas, session_instructions

CONFIG = Path(__file__).resolve().parents[1] / "config" / "personas.toml"
STYLE_MARKERS = {
    "annie": ("自然陪伴", "生活观察家", "冷知识", "不和稀泥", "先说一个具体发现"),
    "annie-calm": ("温柔知心", "深夜电台", "对方当轮原话里的一个词", "是想说说，还是想听点建议", "小小的选择"),
    "annie-lively": ("活泼俏皮", "小烟花", "好笑的外号", "拟声词", "小傲娇"),
    "annie-roast": ("机灵互怼", "硬核损友", "抓包", "招牌三连", "刀子嘴后面是豆腐心"),
    "annie-weary": ("怨种搭档", "工位", "抱怨一句工作量", "打工人词汇", "茶水间"),
    "annie-deadpan": ("优雅乐子人", "纪录片解说员", "英式管家", "新闻播报", "越短越冷"),
}
# Each personality must visibly step out of its act when the user is hurting.
SERIOUS_SWITCH = {
    "annie": "把他稳稳接住",
    "annie-calm": "一句话就够",
    "annie-lively": "先收起烟花",
    "annie-roast": "秒切靠谱同盟",
    "annie-weary": "稳稳地和他一起扛",
    "annie-deadpan": "卸下解说腔",
}


def test_header_forbids_reusing_style_wording():
    instructions, _ = session_instructions("annie", load_personas(CONFIG), {})
    assert "每次现编，不原样复用描述里的词句" in instructions


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
    # Quotable sample lines were parroted verbatim in live tests; describe technique instead.
    assert "示意" not in style and "“" not in style.replace("“抓包”", "").replace("“我只是AI”", "").replace("“随便都行”", "")
    assert SERIOUS_SWITCH[style_id] in style
    assert all(SERIOUS_SWITCH[style_id] not in other["style"]
               for key, other in load_personas(CONFIG).items() if key != style_id)
    for retired in ("好家伙，还有这一出", "这题算你会出", "还挺会挑时候", "这点倒挺有意思"):
        assert retired not in style
    if style_id == "annie-roast":
        assert "严禁机械重复" in style
        assert "绝不攻击真实智商" in style


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

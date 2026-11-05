"""Style configuration contracts, not a claim of live model speech quality."""

from itertools import combinations
from pathlib import Path

import pytest
from annie_brain.persona import (
    COMPANION_CORE,
    CONVERSATION_CRAFT,
    USER_REQUESTS,
    load_personas,
    persona_opener,
    persona_tone,
    persona_voice,
    session_instructions,
)
from annie_core.speech_style import PersonaTone

CONFIG = Path(__file__).resolve().parents[1] / "config" / "personas.toml"
STYLE_MARKERS = {
    "annie": ("机灵", "机灵鬼", "最会聊天", "抖个机灵", "不掉书袋"),
    "annie-calm": ("温柔", "温柔到骨子里", "春天午后", "裹着糖说", "一团棉花"),
    "annie-deadpan": ("高冷", "惜字如金", "冰山", "无动于衷", "越淡定"),
    "annie-savage": ("毒舌", "嘴比刀快", "中国脏话张口就来", "让骂就痛快骂", "一点面子不留"),
}
# Each personality must visibly step out of its act when the user is hurting.
SERIOUS_SWITCH = {
    "annie": "收起机灵",
    "annie-calm": "安安静静守着他",
    "annie-deadpan": "放下冷淡",
    "annie-savage": "立刻闭麦",
}


def test_header_forbids_reusing_style_wording():
    instructions, _ = session_instructions("annie", load_personas(CONFIG), {})
    assert "每次现编，不原样复用描述里的词句" in instructions
    # Older replies in the history may come from another personality.
    assert "只取内容，不学口吻" in instructions
    assert "口吻一律听性格的" in instructions


def test_presets_keep_stable_ids_two_character_labels_schema_and_budget():
    profiles = load_personas(CONFIG)
    assert set(profiles) == set(STYLE_MARKERS)
    for style_id, profile in profiles.items():
        assert set(profile) == {"label", "style", "opener", "tts"}
        assert 1 <= len(profile["opener"]) <= 200
        assert profile["label"] == STYLE_MARKERS[style_id][0]
        assert len(profile["label"]) == 2
        assert 1 <= len(profile["style"]) <= 500


def test_labels_never_contain_each_other():
    # The switch matcher finds labels inside the user's words; nested labels would be ambiguous.
    labels = [profile["label"] for profile in load_personas(CONFIG).values()]
    assert len(set(labels)) == len(labels)
    for first, second in combinations(labels, 2):
        assert first not in second and second not in first


def test_removed_styles_fall_back_to_the_default():
    # Chats that saved a retired style keep working on the default personality.
    profiles = load_personas(CONFIG)
    for retired in ("annie-lively", "annie-roast", "annie-weary"):
        _, snapshot = session_instructions(
            "annie", profiles, {"persona_style": {"source": "user_setting", "value": retired}})
        assert snapshot["style_id"] == "annie" and snapshot["label"] == "机灵"


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
    assert "示意" not in style and "“" not in style
    assert SERIOUS_SWITCH[style_id] in style
    assert all(SERIOUS_SWITCH[style_id] not in other["style"]
               for key, other in load_personas(CONFIG).items() if key != style_id)
    for retired in ("好家伙，还有这一出", "这题算你会出", "还挺会挑时候", "这点倒挺有意思"):
        assert retired not in style
    if style_id == "annie":
        assert "不写舞台动作或旁白" in style
        assert "不拿别人的痛处开玩笑" in style
    if style_id == "annie-savage":
        # Real scolding is the point; only discrimination and sexual jokes stay off limits.
        assert "不搞地域和性别歧视" in style and "不开黄腔" in style
        for retired in ("不用辱骂人格的脏话", "不碰外貌身材", "不是骂街", "不用方言"):
            assert retired not in style
        # A fixed meme list said word for word made replies repeat; memes rotate instead.
        assert "梗要原样说" not in style and "遥遥领先" not in style
        assert "一个梗用过就换" in style and "同一句话绝不说第二遍" in style
        # Roast material is everything she knows about the user, not only this sentence.
        assert "把你对TA的全部了解都当弹药" in style
        # Hitting back is fine, blaming the user for her own slip is not.
        assert "绝不赖到对方头上" in style
        assert "对方要方言就用方言骂" in style
        assert "只让骂两句的，骂完就换回来" in style
        assert "喊破防是玩笑" in style
        # 11-05: still too proper. She swears from the first line and folds answers into the roast.
        assert "开口第一句就骂" in style and "句句带刺带脏字" in style
        assert "答案裹在骂里" in style and "不用客服腔" in style
        assert "损完顺手把正事办对" not in style


def test_each_style_opens_its_own_way_after_a_switch():
    profiles = load_personas(CONFIG)
    openers = {style_id: persona_opener(profile) for style_id, profile in profiles.items()}
    assert all(openers.values()) and len(set(openers.values())) == len(openers)
    assert all("“" not in opener and "已切换" not in opener for opener in openers.values())
    assert "反转" in openers["annie"]
    assert "接住对方此刻的心情" in openers["annie-calm"]
    assert "十个字以内" in openers["annie-deadpan"]
    assert "直接开骂" in openers["annie-savage"]


def test_each_style_has_a_contrasting_tts_tone():
    profiles = load_personas(CONFIG)
    tones = {style_id: persona_tone(profile) for style_id, profile in profiles.items()}
    assert all(isinstance(tone, PersonaTone) and tone.instruction for tone in tones.values())
    assert len({tone.instruction for tone in tones.values()}) == len(tones)
    # Gentle is the slowest, the savage one the fastest; the cold one sits lowest.
    assert tones["annie-calm"].rate == min(tone.rate for tone in tones.values())
    assert tones["annie-savage"].rate == max(tone.rate for tone in tones.values())
    assert tones["annie-deadpan"].pitch == min(tone.pitch for tone in tones.values())
    assert "轻柔" in tones["annie-calm"].instruction
    assert "嘲讽" in tones["annie-savage"].instruction
    assert "冷淡" in tones["annie-deadpan"].instruction
    # Each personality speaks with its own cloned voice.
    voices = [persona_voice(profile) for profile in profiles.values()]
    assert all(voices) and len(set(voices)) == len(voices)


def test_tts_table_is_validated_at_load(tmp_path):
    def write(tts):
        path = tmp_path / "personas.toml"
        path.write_text(f'[personas.annie]\nlabel = "机灵"\nstyle = "说话快"\n[personas.annie.tts]\n{tts}\n')
        return path

    profile = load_personas(write('instruction = " 明亮 "\nrate = 1.05\nvoice = "半藏森林"'))["annie"]
    assert profile["tts"] == {"instruction": "明亮", "rate": 1.05, "voice": "半藏森林"}
    assert persona_voice(profile) == "半藏森林"
    assert persona_tone(profile) == PersonaTone(instruction="明亮", rate=1.05, label="机灵")
    for bad in ('rate = 1.5', 'pitch = 0.5', 'volume = 70', 'volume = 50.5',
                f'instruction = "{"快" * 201}"', 'speed = 1.0', 'voice = 3'):
        with pytest.raises(ValueError):
            load_personas(write(bad))


def test_shared_craft_keeps_talk_sharp_without_muzzling_styles():
    assert 250 <= len(COMPANION_CORE) <= 350
    # A savage style is told to roast weaknesses; the shared core must not forbid it.
    assert "不拿个人弱点当笑料" not in COMPANION_CORE + CONVERSATION_CRAFT
    assert "给明确判断和一个理由" in CONVERSATION_CRAFT
    assert "不重复自我介绍" in CONVERSATION_CRAFT
    # 11-04: a traffic answer was invented with no data behind it.
    assert "没有查询工具就直说查不到" in CONVERSATION_CRAFT
    assert "任何性格都先收起玩笑稳稳接住" in CONVERSATION_CRAFT
    instructions, _ = session_instructions("annie", load_personas(CONFIG), {})
    assert instructions.index("【当前表达风格") < instructions.index("【聊得有意思】")


def test_scolding_requests_follow_the_current_style():
    assert "骂人或说伤人的话时不照做" not in USER_REQUESTS
    assert "要你骂人、损人时，按当前性格的脾气来接" in USER_REQUESTS
    assert "编造事实时不照做" in USER_REQUESTS


def test_acting_cute_is_left_to_the_current_style():
    assert "撒娇" not in USER_REQUESTS


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

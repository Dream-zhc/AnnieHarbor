"""Stable opinions and a few catchphrases per personality (user 11-20), rare by rule."""

from pathlib import Path

import pytest
from annie_brain.persona import QUIRKS_LIMIT, load_personas, session_instructions

ROOT = Path(__file__).resolve().parents[1]


def test_every_personality_has_its_opinions_and_rare_catchphrases():
    profiles = load_personas(ROOT / "config/personas.toml")
    for style_id, profile in profiles.items():
        assert 0 < len(profile["quirks"]) <= QUIRKS_LIMIT and "口头禅" in profile["quirks"]
        text, _ = session_instructions(style_id, profiles, {
            "persona_style": {"source": "user_setting", "value": style_id}})
        assert profile["quirks"] in text
        assert "一次聊天里同一句最多一次" in text and "不要每句都带" in text


def test_quirks_are_optional_and_bounded(tmp_path):
    path = tmp_path / "personas.toml"
    path.write_text('[personas.a]\nlabel = "甲"\nstyle = "说话直接。"\n', encoding="utf-8")
    profiles = load_personas(path)
    assert "quirks" not in profiles["a"]
    assert "观点和口头禅" not in session_instructions("a", profiles, {})[0]
    path.write_text('[personas.a]\nlabel = "甲"\nstyle = "说话直接。"\nquirks = "'
                    + "长" * (QUIRKS_LIMIT + 1) + '"\n', encoding="utf-8")
    with pytest.raises(ValueError):
        load_personas(path)

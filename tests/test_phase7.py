"""Phase 7: validate the real catalog without producing physical side effects."""

import json
from pathlib import Path

import pytest
from annie_brain.action_catalog import load_action_catalog, resolve_action

ROOT = Path(__file__).resolve().parents[1]
CATALOG = ROOT / "config/action_scripts.json"


def test_existing_catalog_is_consistent_and_read_only():
    before = CATALOG.read_bytes()
    actions = load_action_catalog(CATALOG)
    assert len(actions) == 39
    assert resolve_action(actions, "agree_micro_nod")["llm_id"] == 1
    assert resolve_action(actions, "look_left")["duration_ms"] == 1300
    assert CATALOG.read_bytes() == before


@pytest.mark.parametrize(("script_id", "duration_ms"), [
    ("surprise", 1600), ("smirk", 1500), ("proud", 1950),
    ("skeptical", 1600), ("shy", 2000),
])
def test_new_expressive_scripts_have_independent_ids_and_exact_durations(script_id, duration_ms):
    actions = load_action_catalog(CATALOG)
    assert resolve_action(actions, script_id)["duration_ms"] == duration_ms
    assert resolve_action(actions, script_id)["enabled"] is True
    assert resolve_action(actions, script_id)["category"] == "primary"
    assert actions[script_id]["llm_id"] != actions["shy_retract"]["llm_id"]
    assert actions[script_id]["id"] != "shy_retract"


@pytest.mark.parametrize("script_id", [
    "../look_left", "look_left ", "invented_wave", "robot-b", None, True, 1,
])
def test_unknown_or_model_supplied_arbitrary_script_cannot_execute(script_id):
    actions = load_action_catalog(CATALOG)
    with pytest.raises(ValueError):
        resolve_action(actions, script_id)


@pytest.mark.parametrize("mutate", [
    lambda d: d["scripts"].append(dict(d["scripts"][0])),
    lambda d: d["scripts"][1].update(llm_id=1),
    lambda d: d["scripts"][0].update(enabled="true"),
    lambda d: d["scripts"][0].update(duration_ms=-1),
    lambda d: d["scripts"][0].update(id="../escape"),
    lambda d: d["scripts"][0].update(room_id="roomv3"),
])
def test_corrupt_catalog_is_rejected(tmp_path, mutate):
    document = json.loads(CATALOG.read_text(encoding="utf-8"))
    mutate(document)
    candidate = tmp_path / "actions.json"
    candidate.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(ValueError):
        load_action_catalog(candidate)


def test_disabled_action_cannot_resolve(tmp_path):
    document = json.loads(CATALOG.read_text(encoding="utf-8"))
    document["scripts"][0]["enabled"] = False
    candidate = tmp_path / "actions.json"
    candidate.write_text(json.dumps(document), encoding="utf-8")
    actions = load_action_catalog(candidate)
    with pytest.raises(ValueError):
        resolve_action(actions, "agree_micro_nod")

"""scripts/对话质量评测.py: read-only turn selection, judge wire and report (no network)."""

import importlib.util
import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest
from annie_adapters import RuntimeStore

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "对话质量评测.py"
spec = importlib.util.spec_from_file_location("dialogue_eval", SCRIPT)
evaluation = importlib.util.module_from_spec(spec)
spec.loader.exec_module(evaluation)


def add_turn(db, row_id, user, reply, *, chat="c1", source="user", status="completed",
             created_ms=None):
    db.execute(
        """INSERT INTO conversation_turns (id,turn_id,robot_id,room_id,chat_id,persona_id,
           persona_revision,session_epoch,generation,user_text,turn_source,status,
           assistant_generated_text,created_ms,updated_ms)
           VALUES (?,?,?,?,?,?,1,1,1,?,?,?,?,?,?)""",
        (row_id, f"t{row_id}", "annie-a", "roomv2", chat, "annie", user, source, status, reply,
         created_ms or evaluation.day_ms("2026-10-21") + row_id * 60_000,
         created_ms or evaluation.day_ms("2026-10-21") + row_id * 60_000))


def test_turns_come_with_same_chat_context_and_skip_events_and_failures(tmp_path):
    store = RuntimeStore(tmp_path / "d.db")
    store.migrate()
    with sqlite3.connect(store.path) as db:
        add_turn(db, 1, "今天好累", "辛苦啦")
        add_turn(db, 2, "", "外部事件的回复", source="external")
        add_turn(db, 3, "别的会话", "嗯", chat="c2")
        add_turn(db, 4, "想吃火锅", "走起")
        add_turn(db, 5, "没说完", None, status="cancelled")
        add_turn(db, 6, "明天见", "明天见", created_ms=evaluation.day_ms("2026-10-23"))
    rows = evaluation.load_turns(store.path, evaluation.day_ms("2026-10-21"),
                                 evaluation.day_ms("2026-10-21", end=True))
    assert [row["user_text"] for row in rows] == ["今天好累", "别的会话", "想吃火锅"]
    assert rows[2]["context"] == [{"用户": "今天好累", "小安妮": "辛苦啦"}]
    assert rows[1]["context"] == []
    assert json.loads(evaluation.judge_request(rows[2]))["这一轮小安妮"] == "走起"
    assert evaluation.sample(list(range(10)), 3) == [0, 3, 6]  # spread, repeatable
    assert evaluation.sample(rows, 80) == rows


def test_judgements_must_score_every_dimension_within_range():
    good = {"scores": dict.fromkeys(evaluation.DIMENSIONS, 4), "comment": "自然"}
    assert evaluation.parse_judgement(json.dumps(good))["scores"]["像真人"] == 4
    for bad in ({"scores": {"接得住": 5}}, {"scores": {**good["scores"], "分寸": 6}},
                {"scores": {**good["scores"], "连贯": "3"}}):
        with pytest.raises(ValueError):
            evaluation.parse_judgement(json.dumps(bad))


def test_report_lists_means_and_the_worst_turns():
    results = [{"user_text": "你好", "reply": "你好呀", "comment": "平淡",
                "created_ms": evaluation.day_ms("2026-10-21"),
                "scores": dict.fromkeys(evaluation.DIMENSIONS, 2)},
               {"user_text": "我失恋了", "reply": "抱抱你", "comment": "接住了",
                "created_ms": evaluation.day_ms("2026-10-21"),
                "scores": dict.fromkeys(evaluation.DIMENSIONS, 4)}]
    args = SimpleNamespace(since="2026-10-21", until=None, robot=None, model="judge")
    text = evaluation.report("改动前", results, 1, args)
    assert "总均分：3.00 / 5" in text and "评审失败 1 轮" in text
    assert text.index("用户：你好") < text.index("用户：我失恋了")
    assert "| 像真人 | 3.00 |" in text

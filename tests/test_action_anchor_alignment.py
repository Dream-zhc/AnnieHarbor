"""Sidecar word-anchor metadata regressions; no model or optional SDK is loaded."""

import base64
import importlib.util
import json
import queue
import sys
import types
from pathlib import Path

import pytest


@pytest.fixture
def sidecar(monkeypatch):
    directory = Path(__file__).resolve().parents[1] / "sidecars" / "tts_viseme"
    # These dependencies are owned by the independent sidecar environment.
    # Index/wire tests do not execute their acoustic or pronunciation code.
    monkeypatch.setitem(sys.modules, "numpy", types.ModuleType("numpy"))
    dotenv = types.ModuleType("dotenv")
    dotenv.load_dotenv = lambda *_args, **_kwargs: None
    monkeypatch.setitem(sys.modules, "dotenv", dotenv)
    pypinyin = types.ModuleType("pypinyin")
    pypinyin.Style = types.SimpleNamespace(TONE3="tone3")
    pypinyin.pinyin = lambda *_args, **_kwargs: []
    monkeypatch.setitem(sys.modules, "pypinyin", pypinyin)

    def load(name, path):
        spec = importlib.util.spec_from_file_location(name, path)
        module = importlib.util.module_from_spec(spec)
        monkeypatch.setitem(sys.modules, name, module)
        spec.loader.exec_module(module)
        return module

    load("forced_aligner_runtime", directory / "forced_aligner_runtime.py")
    module = load("action_anchor_sidecar", directory / "qwen_stream_room_g2pw.py")
    monkeypatch.setattr(
        module, "detect_active_span",
        lambda pcm, sample_rate: (0, len(pcm) // 2, {}),
    )
    return module


def make_engine(sidecar):
    class Resolver(sidecar.DualG2P):
        def resolve_pypinyin(self, text):
            return self._merge_prons(
                text, [None] * len(text),
                ["ni3" if sidecar.is_han(char) else None for char in text],
            )

    class Aligner:
        last_align_ms = 0

        def align_pcm(self, _pcm, text, **_kwargs):
            return [
                sidecar.AlignedChar(char, index * .1, (index + 1) * .1)
                for index, char in enumerate(sidecar.han_chars(text))
            ]

    events = []
    engine = sidecar.VisemeEngine(Resolver(), events.append, Aligner())
    return engine, events


_MISSING = object()


def tts(text, *, epoch=7, turn="segment-1", cid="reply-1", final=True):
    data = {
        "conversation_id": cid, "turn_id": turn, "text": text,
        "audio": base64.b64encode(b"\x01\x00" * 24000).decode(),
        "sample_rate": 24000, "channels": 1, "final": final,
        "playback_protocol": "sample_cursor_v1",
    }
    if epoch is not _MISSING:
        data["stream_epoch"] = epoch
    return {"type": "tts", "data": data}


def mouths(events):
    return [event for event in events if event["type"] == "mouth_script"]


def test_indexes_count_punctuation_spaces_and_non_bmp_codepoints(sidecar):
    engine, events = make_engine(sidecar)
    engine.on_tts_message(tts("🙂 你，好 你！"))
    aligned = mouths(events)
    assert [event.get("char_index") for event in aligned] == [2, 4, 6]
    assert [event["char"] for event in aligned] == ["你", "好", "你"]
    assert all(event.get("stream_epoch") == 7 for event in aligned)
    assert all(event.get("timing_mode") == "speech_sample_v1" for event in aligned)
    assert all(event["sample_rate"] == 24000 for event in aligned)
    assert all(event["end_sample"] > event["start_sample"] for event in aligned)
    assert all(event["at_sample"] == event["start_sample"] for event in aligned)
    assert all(event["type"] != "action_trigger" for event in events)


def test_cumulative_text_growth_preserves_repeated_char_indexes(sidecar):
    engine, events = make_engine(sidecar)
    engine.on_tts_message(tts("你，", final=False))
    engine.on_tts_message(tts("你，你 你！"))
    assert [event.get("char_index") for event in mouths(events)] == [0, 2, 4]


def test_segment_prefix_offsets_use_original_cumulative_text(sidecar):
    engine, events = make_engine(sidecar)
    engine.on_tts_message(tts("你， "))
    engine.on_tts_message(tts("你， 🙂你好。", turn="segment-2"))
    assert [event.get("char_index") for event in mouths(events)] == [0, 4, 5]
    assert mouths(events)[1]["start_sample"] >= 24000


def test_prefix_mismatch_does_not_invent_global_char_index(sidecar):
    engine, events = make_engine(sidecar)
    engine.on_tts_message(tts("你。"))
    engine.on_tts_message(tts("好。", turn="segment-2"))
    assert "char_index" not in mouths(events)[-1]


def test_legacy_missing_epoch_preserves_mouth_without_action_epoch(sidecar):
    engine, events = make_engine(sidecar)
    engine.on_tts_message(tts("你好", epoch=_MISSING))
    assert len(mouths(events)) == 2
    assert all("stream_epoch" not in event for event in mouths(events))


@pytest.mark.parametrize("epoch", [None, True, 0, -1, "7", 7.0])
def test_malformed_explicit_epoch_is_rejected(sidecar, epoch):
    engine, events = make_engine(sidecar)
    engine.on_tts_message(tts("你好", epoch=epoch))
    assert not mouths(events)


def test_new_epoch_clears_prefix_pcm_and_pending_mouths(sidecar):
    engine, events = make_engine(sidecar)
    cleared = []
    engine.drop_pending_mouth = lambda: cleared.append(True)
    engine.on_tts_message(tts("你， "))
    engine.on_tts_message(tts("好。", epoch=8))
    last = mouths(events)[-1]
    assert last.get("stream_epoch") == 8
    assert last.get("char_index") == 0
    assert last["start_sample"] == 0
    assert cleared == [True]
    assert engine.conversation("reply-1").sample_cursor == 24000


def test_old_epoch_cannot_reopen_or_contaminate_new_stream(sidecar):
    engine, events = make_engine(sidecar)
    engine.on_tts_message(tts("你好", epoch=8))
    count = len(events)
    engine.on_tts_message(tts("旧字", epoch=7))
    assert len(events) == count
    assert engine.conversation("reply-1").stream_epoch == 8
    assert engine.conversation("reply-1").sample_cursor == 24000


def test_missing_epoch_cannot_inherit_current_explicit_epoch(sidecar):
    engine, events = make_engine(sidecar)
    engine.on_tts_message(tts("你", final=False))
    count = len(events)
    engine.on_tts_message(tts("你好", epoch=_MISSING))
    assert len(events) == count
    assert engine.conversation("reply-1").sample_cursor == 24000


def test_old_stop_epoch_cannot_clear_current_stream(sidecar):
    engine, events = make_engine(sidecar)
    engine.on_tts_message(tts("你", epoch=8, final=False))
    engine.on_tts_message({"type": "tts", "data": {
        "stop": True, "conversation_id": "reply-1", "stream_epoch": 7,
    }})
    assert not engine.conversation("reply-1").cleared
    engine.on_tts_message(tts("你好", epoch=8))
    assert [event.get("char_index") for event in mouths(events)] == [0, 1]


@pytest.mark.parametrize("control_cid", ["reply-1", "", "unknown-reply"])
def test_old_or_unidentified_stop_cannot_drop_new_pending_mouths(sidecar, control_cid):
    engine, _events = make_engine(sidecar)
    dropped = []
    engine.drop_pending_mouth = lambda: dropped.append(True)
    engine.on_tts_message(tts("你", cid="reply-1", final=False))
    engine.on_tts_message(tts("好", cid="reply-2", epoch=8, final=False))
    dropped.clear()
    engine.on_tts_message({"type": "tts", "data": {
        "stop": True, "conversation_id": control_cid, "stream_epoch": 7,
    }})
    assert dropped == []
    assert not engine.conversation("reply-2").cleared


def test_late_cleared_conversation_cannot_clear_new_conversation(sidecar):
    engine, events = make_engine(sidecar)
    engine.on_tts_message(tts("你", cid="reply-1", final=False))
    engine.on_tts_message(tts("好", cid="reply-2", epoch=8, final=False))
    count = len(events)
    engine.on_tts_message(tts("迟到", cid="reply-1"))
    assert len(events) == count
    assert not engine.conversation("reply-2").cleared
    engine.on_tts_message(tts("好你", cid="reply-2", epoch=8))
    assert mouths(events)[-1].get("char_index") == 1


@pytest.mark.parametrize("epoch", [7, 8, _MISSING])
def test_unseen_old_conversation_cannot_replace_active_modern_stream(sidecar, epoch):
    engine, events = make_engine(sidecar)
    engine.on_tts_message(tts("你", cid="new-reply", epoch=8, final=False))
    dropped = []
    engine.drop_pending_mouth = lambda: dropped.append(True)
    count = len(events)
    engine.on_tts_message(tts("迟到", cid="old-unseen", epoch=epoch))
    assert len(events) == count
    assert dropped == []
    assert engine._active_cid == "new-reply"
    assert not engine.conversation("new-reply").cleared


def test_explicit_stop_allows_new_runtime_epoch_restart(sidecar):
    engine, events = make_engine(sidecar)
    engine.on_tts_message(tts("你", cid="old-runtime", epoch=8, final=False))
    engine.on_tts_message({"type": "tts", "data": {
        "stop": True, "conversation_id": "old-runtime", "stream_epoch": 8,
    }})
    engine.on_tts_message(tts("好", cid="new-runtime", epoch=1))
    assert engine._active_cid == "new-runtime"
    assert mouths(events)[-1].get("stream_epoch") == 1
    assert mouths(events)[-1].get("char_index") == 0


def test_stop_rejects_old_stream_and_next_conversation_restarts_index(sidecar):
    engine, events = make_engine(sidecar)
    engine.on_tts_message(tts("你好"))
    engine.on_tts_message({"type": "tts", "data": {
        "stop": True, "conversation_id": "reply-1", "stream_epoch": 7,
    }})
    count = len(events)
    engine.on_tts_message(tts("迟到"))
    assert len(events) == count
    engine.on_tts_message(tts(" 好", cid="reply-2", epoch=8))
    assert mouths(events)[-1].get("char_index") == 1
    assert mouths(events)[-1].get("stream_epoch") == 8
    assert mouths(events)[-1]["start_sample"] == 0


def pacer_without_thread(sidecar):
    pacer = sidecar.MouthSendPacer.__new__(sidecar.MouthSendPacer)
    pacer._origin_wall = None
    pacer._lead_s = 0
    return pacer


def test_device_sample_timing_releases_future_alignments_immediately(sidecar):
    pacer = pacer_without_thread(sidecar)
    event = {
        "timing_mode": "speech_sample_v1", "stream_epoch": 7,
        "sample_rate": 24000, "start_sample": 2400,
    }
    assert pacer._mouth_due(event, 10.0) == 10.0
    event["start_sample"] = 24000
    assert pacer._mouth_due(event, 10.01) == 10.01


@pytest.mark.parametrize("extra", [{}, {"timing_mode": "speech_sample_v1"}])
def test_legacy_or_unverified_timing_retains_pacer_clock(sidecar, extra):
    pacer = pacer_without_thread(sidecar)
    event = {"sample_rate": 24000, "start_sample": 2400, **extra}
    assert pacer._mouth_due(event, 10.0) == 10.0
    event["start_sample"] = 24000
    assert pacer._mouth_due(event, 10.01) == 10.9


def test_engine_to_real_pacer_does_not_wait_for_future_device_sample(sidecar):
    engine, _events = make_engine(sidecar)
    outgoing = queue.Queue(maxsize=512)
    observed = []
    observer = types.SimpleNamespace(publish=observed.append)
    pacer = sidecar.connect_mouth_output(engine, outgoing, observer)
    engine.forced_aligner.align_pcm = lambda *_args, **_kwargs: [
        sidecar.AlignedChar("你", 0, .1),
        sidecar.AlignedChar("好", 10, 10.1),
    ]
    packet = tts("你好")
    packet["data"]["audio"] = base64.b64encode(b"\x01\x00" * 24000 * 11).decode()
    try:
        engine.on_tts_message(packet)
        received = [json.loads(outgoing.get(timeout=.5)) for _ in range(2)]
        assert received[1]["start_sample"] == 240000
        assert received == observed
        assert engine.mouth_replacement_enabled is True
        assert engine.send_mouth.__self__ is pacer
        assert engine.drop_pending_mouth_from_seq.__self__ is pacer
    finally:
        pacer.stop()
        pacer._thread.join(timeout=.5)


def test_main_output_wiring_paces_legacy_and_clears_both_queues(sidecar):
    engine, _events = make_engine(sidecar)
    outgoing = queue.Queue(maxsize=512)
    observed = []
    pacer = sidecar.connect_mouth_output(
        engine, outgoing, types.SimpleNamespace(publish=observed.append),
    )
    engine.forced_aligner.align_pcm = lambda *_args, **_kwargs: [
        sidecar.AlignedChar("你", 0, .1),
        sidecar.AlignedChar("好", 10, 10.1),
    ]
    packet = tts("你好", epoch=_MISSING)
    packet["data"].pop("playback_protocol")
    packet["data"]["audio"] = base64.b64encode(b"\x01\x00" * 24000 * 11).decode()
    try:
        engine.on_tts_message(packet)
        assert json.loads(outgoing.get(timeout=.5))["char"] == "你"
        with pytest.raises(queue.Empty):
            outgoing.get(timeout=.03)
        outgoing.put_nowait("already queued frame")
        engine.on_tts_message({"type": "tts", "data": {
            "stop": True, "conversation_id": "reply-1",
        }})
        assert outgoing.empty()
        assert not pacer._heap
        assert len(observed) == 1
    finally:
        pacer.stop()
        pacer._thread.join(timeout=.5)

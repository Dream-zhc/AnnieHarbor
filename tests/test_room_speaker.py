import array
import base64
import importlib.util
import sys
from pathlib import Path

from annie_adapters.playback import playback_receipt

SPEC = importlib.util.spec_from_file_location(
    "room_speaker_program",
    Path(__file__).resolve().parents[1] / "room-speaker" / "speaker.py",
)
speaker = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = speaker
SPEC.loader.exec_module(speaker)


def tone(values):
    return array.array("h", values)


def pcm(values):
    return b"".join(int(value).to_bytes(2, "little", signed=True) for value in values)


def audio_message(pcm, *, cid="turn", epoch=1, seq=1, start=0, stream_final=False):
    return {"type": "tts", "data": {
        "audio": base64.b64encode(pcm).decode("ascii"),
        "format": "pcm_s16le", "sample_rate": 24000, "channels": 1,
        "conversation_id": cid, "stream_epoch": epoch, "seq": seq,
        "start_sample": start, "end_sample": start + len(pcm) // 2,
        "stream_final": stream_final,
    }}


def end_message(*, cid="turn", epoch=1, seq=2, end=0):
    return {"type": "tts", "data": {
        "audio": "", "format": "pcm_s16le", "sample_rate": 24000, "channels": 1,
        "conversation_id": cid, "stream_epoch": epoch, "seq": seq,
        "start_sample": end, "end_sample": end, "stream_final": True,
    }}


def stop_message(*, cid="turn", epoch=1, seq=9):
    return {"type": "tts", "data": {
        "audio": "", "conversation_id": cid, "stream_epoch": epoch, "seq": seq,
        "start_sample": 0, "end_sample": 0, "stop": True, "reason": "cancel",
    }}


def control(kind, *, cid="turn", epoch=1, command_seq=1, fade_ms=40):
    data = {
        "protocol_version": 1, "conversation_id": cid, "stream_epoch": epoch,
        "sender": "brain", "reason": "speech_started",
    }
    if kind != "stop":
        data["command_seq"] = command_seq
        data["fade_ms"] = fade_ms
    return {"type": "tts.playback." + kind, "data": data}


def new_player(rate=24000, prebuffer_ms=0, progress_ms=100):
    playback = speaker.Playback(rate, prebuffer_ms=prebuffer_ms, progress_ms=progress_ms)
    gate = speaker.CommandGate()
    return playback, gate


def kinds(events):
    return [event[0] for event in events]


def test_pcm_is_little_endian():
    parsed = speaker.parse_tts(audio_message(b"\x01\x00\xff\xff"))
    assert list(parsed.samples) == [1, -1]


def test_bad_rate_and_empty_segment_are_rejected_or_ignored():
    message = audio_message(b"\x00\x00")
    message["data"]["sample_rate"] = 16000
    try:
        speaker.parse_tts(message)
        raise AssertionError("16 kHz audio must be rejected")
    except ValueError:
        pass
    assert speaker.parse_tts({"type": "tts", "data": {
        "audio": "", "conversation_id": "turn", "stream_epoch": 1, "seq": 1,
        "start_sample": 0, "end_sample": 0, "final": True,
    }}) is None
    broken = audio_message(b"\x00\x00")
    broken["data"]["end_sample"] = 4
    try:
        speaker.parse_tts(broken)
        raise AssertionError("cursor mismatch must be rejected")
    except ValueError:
        pass


def test_exact_24k_playback_drains_with_speaker_receipt():
    playback, gate = new_player()
    events = speaker.handle_tts(playback, gate, audio_message(pcm([1, 2, 3, 4])))
    events += speaker.handle_tts(playback, gate, end_message(end=4))
    samples, played = playback.pull(4)
    assert events == []
    assert list(samples) == [1, 2, 3, 4]
    assert kinds(played) == ["started", "drained"]
    assert played[0][4] == 0
    assert played[-1][4] == 4
    receipt = speaker.build_receipt(
        "drained", conversation_id=played[-1][1], stream_epoch=played[-1][2],
        playback_id=played[-1][3], played=played[-1][4], seq=2,
    )
    assert playback_receipt(receipt)["data"]["source"] == "speaker"
    assert receipt["data"]["played_sample_count"] == 4


def test_48k_and_44100_cursors_finish_on_the_source_length():
    playback, gate = new_player(48000)
    speaker.handle_tts(playback, gate, audio_message(pcm([7, 7, 7, 7])))
    speaker.handle_tts(playback, gate, end_message(end=4))
    samples, events = playback.pull(8)
    assert list(samples) == [7] * 8
    assert events[-1][0] == "drained" and events[-1][4] == 4

    playback, gate = new_player(44100)
    speaker.handle_tts(playback, gate, audio_message(pcm([7] * 100)))
    speaker.handle_tts(playback, gate, end_message(end=100))
    samples, events = playback.pull(500)
    assert events[-1][0] == "drained" and events[-1][4] == 100
    assert set(samples) <= {0, 7}


def test_gap_does_not_claim_drained():
    playback, gate = new_player()
    speaker.handle_tts(playback, gate, audio_message(pcm([1] * 100)))
    speaker.handle_tts(playback, gate, audio_message(
        pcm([2] * 50), seq=2, start=200,
    ))
    speaker.handle_tts(playback, gate, end_message(seq=3, end=250))
    _samples, events = playback.pull(200)
    assert "drained" not in kinds(events)
    assert "incomplete" in kinds(events)


def test_prebuffer_holds_audio_until_enough_or_final():
    playback, gate = new_player(prebuffer_ms=180)
    speaker.handle_tts(playback, gate, audio_message(pcm([3] * 1000)))
    samples, events = playback.pull(20)
    assert list(samples) == [0] * 20 and events == [] and playback.state == "prebuffer"
    speaker.handle_tts(playback, gate, audio_message(
        pcm([3] * 3320), seq=2, start=1000,
    ))
    samples, events = playback.pull(15)
    assert list(samples) == [3] * 15
    assert events[0][0] == "started" and events[0][4] == 0
    assert playback.room_played() == 15


def test_hold_fade_freezes_then_resume_continues():
    playback, gate = new_player()
    speaker.handle_tts(playback, gate, audio_message(pcm([10000] * 2000)))
    playback.pull(10)
    assert speaker.handle_control(playback, gate, control("hold", fade_ms=40)) == []
    samples, events = playback.pull(960)
    assert samples[0] > samples[-1] == 0
    assert events[-1][0] == "held" and events[-1][5] == 1
    assert playback.state == "held"
    assert playback.room_played() == 970
    silent, none = playback.pull(10)
    assert list(silent) == [0] * 10 and none == []
    resumed = speaker.handle_control(playback, gate, control("resume", command_seq=2, fade_ms=0))
    assert kinds(resumed) == ["resumed"] and resumed[0][5] == 2
    playback.pull(10)
    assert playback.room_played() == 980


def test_resume_during_fade_reports_held_first():
    playback, gate = new_player()
    speaker.handle_tts(playback, gate, audio_message(pcm([1000] * 500)))
    playback.pull(10)
    speaker.handle_control(playback, gate, control("hold", fade_ms=40))
    playback.pull(100)
    events = speaker.handle_control(playback, gate, control("resume", command_seq=2, fade_ms=0))
    assert kinds(events) == ["held", "resumed"]
    assert events[0][5] == 1 and events[1][5] == 2
    assert events[0][4] == events[1][4] == 110


def test_hold_before_playback_then_short_final_drains():
    playback, gate = new_player(prebuffer_ms=180)
    speaker.handle_tts(playback, gate, audio_message(pcm([4] * 100)))
    held = speaker.handle_control(playback, gate, control("hold"))
    assert kinds(held) == ["held"] and held[0][4] == 0
    assert speaker.handle_control(playback, gate, control("hold", command_seq=1)) == []
    speaker.handle_control(playback, gate, control("resume", command_seq=2, fade_ms=0))
    speaker.handle_tts(playback, gate, end_message(end=100))
    _samples, events = playback.pull(100)
    assert kinds(events) == ["started", "drained"]
    assert events[-1][4] == 100


def test_stop_discards_audio_and_duplicate_control_is_ignored():
    playback, gate = new_player()
    speaker.handle_tts(playback, gate, audio_message(pcm([9] * 50)))
    playback.pull(12)
    stopped = speaker.handle_tts(playback, gate, stop_message())
    assert kinds(stopped) == ["stopped"] and stopped[0][4] == 12 and stopped[0][5] == "cancel"
    assert speaker.handle_control(playback, gate, control("stop")) == []
    assert speaker.handle_tts(playback, gate, audio_message(pcm([9] * 10), seq=3)) == []
    samples, events = playback.pull(8)
    assert list(samples) == [0] * 8 and events == []
    receipt = speaker.build_receipt(
        "stopped", conversation_id="turn", stream_epoch=1,
        playback_id=stopped[0][3], played=12, seq=1, reason="cancel",
    )
    assert playback_receipt(receipt)["data"]["reason"] == "cancel"


def test_new_utterance_stops_the_previous_one():
    playback, gate = new_player()
    speaker.handle_tts(playback, gate, audio_message(pcm([1] * 30), cid="old"))
    playback.pull(5)
    events = speaker.handle_tts(playback, gate, audio_message(pcm([2] * 30), cid="new"))
    assert kinds(events) == ["stopped"] and events[0][1] == "old"


def test_progress_follows_the_device_clock():
    playback, _gate = new_player(progress_ms=100)
    playback.push_audio("turn", 1, 1, 0, tone([1] * 5000))
    _samples, first = playback.pull(100)
    assert kinds(first) == ["started"]
    _samples, second = playback.pull(2300)
    assert "progress" in kinds(second)
    assert second[-1][4] == 2400


def test_overflow_stops_instead_of_keeping_old_audio():
    playback = speaker.Playback(24000, prebuffer_ms=0)
    playback.max_samples = 50
    playback.push_audio("turn", 1, 1, 0, tone([1] * 40))
    events = playback.push_audio("turn", 1, 2, 40, tone([1] * 40))
    assert kinds(events) == ["stopped"] and events[0][5] == "overflow"


def test_other_stream_control_and_echoed_receipt_are_ignored():
    playback, gate = new_player()
    speaker.handle_tts(playback, gate, audio_message(pcm([1] * 20)))
    assert speaker.handle_control(playback, gate, control("hold", cid="other")) == []
    echo = control("hold")
    echo["data"]["sender"] = "speaker"
    assert speaker.parse_control(echo) is None
    assert speaker.handle_control(playback, gate, {
        "type": "tts.playback.started",
        "data": {"sender": "speaker", "source": "speaker"},
    }) == []


def test_receipts_match_the_brain_contract():
    kinds_to_build = [
        ("started", None), ("progress", None), ("drained", None),
        ("held", 3), ("resumed", 4), ("stopped", None),
    ]
    for kind, command in kinds_to_build:
        message = speaker.build_receipt(
            kind, conversation_id="turn", stream_epoch=2, playback_id="play-1",
            played=480, seq=1, command_seq=command, reason="cancel" if kind == "stopped" else None,
        )
        assert playback_receipt(message)["data"]["sender"] == "speaker"
        assert "description" in message and message["data"]["sample_rate"] == 24000


def test_welcome_and_subscribe_ack():
    welcome = {
        "type": "WELCOME", "ok": True, "clientId": "speaker",
        "robotId": "reb_tts", "room": "roomv2", "size": 1,
        "subscriptions": [], "catalog": [],
    }
    speaker.validate_welcome(welcome, "roomv2", "reb_tts")
    try:
        speaker.validate_welcome(welcome, "roomv2", "reb_voice")
        raise AssertionError("wrong robot must fail")
    except ValueError:
        pass
    speaker.validate_subscribe_ack(
        {"type": "ACK", "data": {"what": "subscribed", "subscriptions": ["tts"]}},
        ["tts"],
    )
    speaker.validate_subscribe_ack(
        {"type": "ACK", "data": {"what": "subscribed", "subscriptions": ["*"]}},
        ["*"],
    )


def test_same_packet_stream_final_can_drain():
    playback, gate = new_player()
    speaker.handle_tts(playback, gate, audio_message(pcm([8, 8]), stream_final=True))
    _samples, events = playback.pull(2)
    assert kinds(events) == ["started", "drained"] and events[-1][4] == 2

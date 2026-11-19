"""Instant hotwords (11-19): the input transcription is biased toward her name and dialects."""

import asyncio
import tomllib

import pytest
from annie_adapters.realtime import session_update, validate_hotwords
from annie_core import RobotConfig
from annie_runtime import HarborRuntime

from tests.test_realtime_model_latency import ROOT
from tests.test_wake_call_recovery import ready, sent


def test_hotwords_ride_on_the_first_session_update():
    session = session_update("小安妮", hotwords={"小安妮": 5, " 河南话 ": 3})["session"]
    assert session["input_audio_transcription"] == {"corpus": {"vocabulary": {
        "小安妮": 5, "河南话": 3}}}
    assert "input_audio_transcription" not in session_update("小安妮")["session"]


@pytest.mark.parametrize("bad", [
    {"小安妮": 6}, {"小安妮": 0}, {"小安妮": 4.5}, {"小安妮": True}, {"": 3},
    {"一二三四五六七八九十一二三四五六": 3}, {3: 3}, ["小安妮"],
    {f"词{i}": 50 for i in range(51)},
])
def test_invalid_hotwords_fail_before_any_audio(bad):
    with pytest.raises(ValueError):
        validate_hotwords(bad)


def test_the_configured_table_is_valid_and_reaches_the_provider(tmp_path):
    with (ROOT / "config/providers.toml").open("rb") as file:
        table = tomllib.load(file)["realtime"]["hotwords"]
    assert validate_hotwords(table)["小安妮"] == 5 and table["河南话"] == 3
    with pytest.raises(ValueError):
        HarborRuntime([RobotConfig("a", room_id="roomv2")], tmp_path / "bad.db",
                      realtime_key="fake", realtime_hotwords={"小安妮": 9})

    async def run():
        from tests.test_decision_model import EchoSocket
        rt = HarborRuntime([RobotConfig("a", room_id="roomv2")], tmp_path / "hot.db",
                           realtime_key="fake", realtime_socket_factory=EchoSocket,
                           realtime_hotwords=table)
        client = await ready(rt)
        try:
            update = sent(client, "session.update")[0]["session"]
            assert update["input_audio_transcription"]["corpus"]["vocabulary"]["四川话"] == 3
        finally:
            await rt.close()
    asyncio.run(run())

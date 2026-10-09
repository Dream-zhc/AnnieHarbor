"""End-to-end Room PCM ingress wiring with a deterministic DSP test double."""

import asyncio
import base64

from annie_adapters import RoomServerConfig
from annie_core import RobotConfig
from annie_runtime import HarborRuntime


def test_one_room_microphone_goes_to_one_cleaned_realtime_queue(tmp_path):
    class FakeAec:
        def process(self, reference, capture):
            return b"\x44" * len(capture)

    class Publisher:
        def __init__(self, *_args, **_kwargs):
            self.packets = []

        async def publish(self, packet):
            self.packets.append(packet)

    async def run():
        channels = {name: "reb_" + name for name in
                    ("main", "tts", "mic", "visualdata", "vision", "sam", "voice")}
        root = "ws://127.0.0.1:1/robot"
        room = RoomServerConfig(base_url=root + "/roomv2", room_root=root,
                                channels=channels, default_urls={
                                    name: root + "/roomv2/" + val
                                    for name, val in channels.items()})
        runtime = HarborRuntime([RobotConfig("annie-a", room_id="roomv2", listen_enabled=True)],
                                tmp_path / "aec.db", room, realtime_key="test",
                                tts_voice="test", room_tts_factory=Publisher,
                                cloud_aec_robots=("annie-a",),
                                cloud_aec_sync_offset_ms=0)
        aec = runtime.cloud_aec["annie-a"]
        aec.backend_factory = FakeAec
        aec.loop = asyncio.get_running_loop()
        aec.start()
        runtime.started = True
        try:
            publisher = runtime.realtime["annie-a"].speech.publisher
            tts = {"type": "tts", "data": {
                "format": "pcm_s16le", "sample_rate": 24000,
                "audio": base64.b64encode(bytes(480 * 100)).decode(),
                "start_sample": 0, "conversation_id": "turn-a", "stream_epoch": 1,
            }}
            await publisher.publish(tts)
            speech = runtime.realtime["annie-a"].speech
            speech.turn_id = "turn-a"
            speech.stream_epoch = 1
            speech.samples = 24000
            runtime._room_playback_receipt("annie-a", {"type": "tts.playback.started", "data": {
                "protocol_version": 1, "sample_rate": 24000,
                "source": "speaker", "conversation_id": "turn-a", "stream_epoch": 1,
                "seq": 1, "played_sample_count": 12000, "playback_id": "speaker-a",
                "sender": "speaker",
            }})
            raw = b"\x66" * 640
            encoded = len(b"MIC_AUDIO").to_bytes(2, "big") + b"MIC_AUDIO" + raw
            source = runtime.room_sources["annie-a"]
            runtime._ingest_live_room_pcm("annie-a", source, encoded)
            for _ in range(100):
                if runtime.robots["annie-a"].audio.accepted:
                    break
                await asyncio.sleep(.002)
            assert runtime.robots["annie-a"].audio.accepted == 1
            frame, _waiting = runtime.next_pcm_nowait("annie-a")
            assert frame.payload == b"\x44" * 640
            assert frame.source_id == source
            assert runtime.input_stats("annie-a")["aec"]["processed"] == 1
            assert runtime.input_stats("annie-a")["aec"]["receipts"] == 1
            assert len(publisher.publisher.packets) == 1
        finally:
            runtime.started = False
            aec.close()

    asyncio.run(run())

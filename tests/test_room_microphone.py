"""Board microphone program: same MIC_AUDIO as the web page, receipts, one-device warnings."""

import asyncio
import importlib.util
import json
import logging
import math
import sys
from pathlib import Path

import numpy as np
import pytest
from annie_adapters import decode_room_binary_frame

ROOT = Path(__file__).resolve().parents[1]


def load(name, file):
    spec = importlib.util.spec_from_file_location(name, ROOT / "room-speaker" / file)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


speaker = load("room_speaker_program_mic", "speaker.py")
mic = speaker.mic_module()


def sine(rate, freq, seconds, amplitude=.5):
    t = np.arange(int(rate * seconds)) / rate
    return (amplitude * np.sin(2 * math.pi * freq * t)).astype(np.float32)


def dominant(samples, rate):
    spectrum = np.abs(np.fft.rfft(samples.astype(np.float64) * np.hanning(len(samples))))
    return np.fft.rfftfreq(len(samples), 1 / rate)[int(spectrum.argmax())]


def test_frames_are_exactly_what_the_web_microphone_sends():
    pcm = bytes(range(256)) * 2 + bytes(128)
    frame = mic.encode_frame(pcm)
    assert frame[:11] == b"\x00\x09MIC_AUDIO"
    assert decode_room_binary_frame(frame) == ("MIC_AUDIO", pcm)
    with pytest.raises(ValueError):
        mic.encode_frame(bytes(320))


@pytest.mark.parametrize("rate", [16000, 44100, 48000])
def test_resampling_keeps_speech_band_and_block_size_does_not_matter(rate):
    source = sine(rate, 1000, 1.0)
    whole = mic.Resampler(rate).process(source)
    pieces = mic.Resampler(rate)
    blocks = np.concatenate([pieces.process(source[i:i + 333])
                             for i in range(0, len(source), 333)])
    assert abs(len(whole) - 16000) <= 2 and abs(len(blocks) - len(whole)) <= 1
    n = min(len(whole), len(blocks))
    assert np.max(np.abs(whole[:n].astype(int) - blocks[:n].astype(int))) <= 2
    assert abs(dominant(whole, 16000) - 1000) < 5


def test_sound_above_8khz_does_not_fold_back_into_speech():
    out = mic.Resampler(48000).process(sine(48000, 12000, 1.0))
    assert np.sqrt(np.mean(out[200:].astype(float) ** 2)) < 32767 * .5 * 10 ** (-30 / 20)


def test_capture_queues_640b_frames_keeps_the_freshest_and_drops_stale_audio():
    now = [0.0]
    capture = mic.MicCapture(48000, channel=1, queue_frames=3, clock=lambda: now[0])
    stereo = np.stack([np.zeros(4800, np.float32), sine(48000, 500, .1)], axis=1)
    capture.feed(stereo)  # 100 ms -> 5 frames, the queue keeps the last 3
    report = capture.report()
    assert report["produced_frames"] == 5 and report["dropped_frames"] == 2
    assert report["peak_dbfs"] is not None and report["peak_dbfs"] > -10  # channel 1, not silent 0
    frames = capture.take()
    assert [len(item) for item in frames] == [640, 640, 640]
    capture.feed(stereo[:960])
    now[0] = 0.6  # older than 500 ms: Brain would drop it, so it is never sent
    assert capture.take() == []
    assert capture.report()["dropped_frames"] == 3


def test_receipts_and_presence_carry_device_but_never_touch_playback_fields():
    identity = mic.device_identity("microphone", "USB 麦克风")
    capture = mic.MicCapture(16000)
    capture.feed(sine(16000, 300, .04))
    receipt = mic.build_mic_receipt("progress", identity=identity, seq=2,
                                    counters=capture.report(), room_connected=True)
    data = receipt["data"]
    assert receipt["type"] == "mic.capture.progress"
    assert data["sender"] == data["source"] == "microphone"
    assert data["instance_id"].startswith("microphone-") and data["device_name"] == "USB 麦克风"
    assert data["captured_sample_count"] == 640 and data["room_connected"] is True
    presence = speaker.build_speaker_presence(mic.device_identity("speaker", "板载喇叭"),
                                              speaker.Playback(48000))
    assert presence["type"] == "speaker.presence" and presence["data"]["state"] == "idle"


def test_speaker_only_stays_the_default():
    args = speaker.parse_args([])
    assert not args.mic and not args.no_speaker
    assert speaker.parse_args(["--mic", "--mic-channel", "2"]).mic_channel == 2
    with pytest.raises(SystemExit):
        speaker.parse_args(["--no-speaker"])


class FakeRoom:
    """Per-path broadcaster with Room's WELCOME/SUBSCRIBE/ACK handshake."""

    def __init__(self):
        self.subscribers = {}
        self.subscribe_requests = {}

    async def handler(self, ws):
        room_id, channel = ws.request.path.rstrip("/").split("/")[-2:]
        path = (room_id, channel)
        await ws.send(json.dumps({"type": "WELCOME", "ok": True, "clientId": "c", "robotId": channel,
                                  "room": room_id, "size": 1, "subscriptions": [],
                                  "catalog": [{"type": "MIC_AUDIO"}]}))
        try:
            await self._relay(ws, path)
        except Exception:
            pass  # a client torn down mid-test
        finally:
            self.subscribers.get(path, set()).discard(ws)

    async def _relay(self, ws, path):
        async for raw in ws:
            if isinstance(raw, str):
                message = json.loads(raw)
                if message.get("type") == "SUBSCRIBE":
                    self.subscribe_requests[path] = self.subscribe_requests.get(path, 0) + 1
                    self.subscribers.setdefault(path, set()).add(ws)
                    await ws.send(json.dumps({"type": "ACK", "data": {
                        "what": "subscribed", "subscriptions": message["data"]["types"]}}))
                    continue
            for peer in list(self.subscribers.get(path, ())):
                if peer is not ws:
                    try:
                        await peer.send(raw)
                    except Exception:
                        pass


def test_board_microphone_end_to_end_and_a_second_one_is_flagged(tmp_path):
    from annie_adapters import RoomServerConfig
    from annie_core import RobotConfig
    from annie_runtime import HarborRuntime
    from websockets.asyncio.server import serve

    from tests.test_decision_model import EchoSocket

    async def feed(capture, stop):
        tone = sine(16000, 440, .02, .3)
        while not stop.is_set():
            capture.feed(tone)
            await asyncio.sleep(.02)

    async def run():
        room = FakeRoom()
        server = await serve(room.handler, "127.0.0.1", 0)
        root = f"ws://127.0.0.1:{server.sockets[0].getsockname()[1]}/robot"
        channels = {name: "reb_" + name for name in
                    ("main", "tts", "mic", "visualdata", "vision", "sam", "voice")}
        config = RoomServerConfig(base_url=root + "/roomv2", room_root=root, channels=channels,
                                  default_urls={n: f"{root}/roomv2/{c}" for n, c in channels.items()},
                                  reconnect_min_sec=.01, reconnect_max_sec=.02,
                                  connect_timeout_sec=.5)
        rt = HarborRuntime([RobotConfig("a", room_id="roomv2", listen_enabled=True)],
                           tmp_path / "rt.db", config, realtime_key="fake",
                           realtime_socket_factory=EchoSocket)
        await rt.start()
        client = rt.realtime["a"]
        boards, feeders = [], []
        stop = asyncio.Event()

        def board():
            capture = mic.MicCapture(16000)
            identity = mic.device_identity("microphone", "板载阵列")
            task = asyncio.create_task(speaker.serve(
                "", f"{root}/roomv2/reb_voice", "roomv2", None, None,
                mic=(f"{root}/roomv2/reb_mic", capture, identity)))
            boards.append(task)
            feeders.append(asyncio.create_task(feed(capture, stop)))
            return identity

        async def until(check, seconds=4):
            async with asyncio.timeout(seconds):
                while not check():
                    await asyncio.sleep(.02)

        try:
            first = board()
            await until(lambda: rt.input_stats("a")["accepted"] >= 10
                        and rt.input_stats("a")["audio_devices"]["microphones"])
            devices = rt.input_stats("a")["audio_devices"]
            assert devices["status"] == "ok"
            assert devices["microphones"][0]["instance_id"] == first["instance_id"]
            assert devices["microphones"][0]["device_name"] == "板载阵列"
            assert room.subscribe_requests.get(("roomv2", "reb_mic"), 0) == 1  # Brain only

            board()
            await until(lambda: rt.input_stats("a")["audio_devices"]["status"] == "conflict")
            conflict = rt.input_stats("a")["audio_devices"]["conflicts"][0]
            assert conflict == {"role": "microphone", "reason": "multiple_devices", "count": 2}
            assert any(e["kind"] == "audio.devices.conflict" for e in client.events)

            boards[1].cancel()  # a clean exit sends mic.capture.stopped
            await asyncio.gather(boards[1], return_exceptions=True)
            await until(lambda: rt.input_stats("a")["audio_devices"]["status"] == "ok")
            assert any(e["kind"] == "audio.devices.ok" for e in client.events)
        finally:
            stop.set()
            for task in boards + feeders:
                task.cancel()
            await asyncio.gather(*boards, *feeders, return_exceptions=True)
            await rt.close()
            server.close()
            await server.wait_closed()

    asyncio.run(run())


class FakeStatus:
    def __init__(self, overflow):
        self.input_overflow = overflow

    def __bool__(self):
        return self.input_overflow


class FakeSounddevice:
    """A 4-channel 48 kHz array that refuses 16 kHz and mono, like many board codecs."""

    def __init__(self):
        self.opened = []

    def query_devices(self, device, kind):
        assert kind == "input"
        return {"name": "板载4麦阵列", "default_samplerate": 48000.0, "max_input_channels": 4}

    def InputStream(self, *, samplerate, channels, dtype, callback, device, blocksize, latency):
        if samplerate == 16000 or channels != 4:
            raise RuntimeError(f"Invalid sample rate/channels {samplerate}/{channels}")
        sd = self

        class Stream:
            def start(self):
                sd.opened.append((samplerate, channels, blocksize))
                block = np.zeros((blocksize, channels), np.float32)
                block[:, 2] = sine(samplerate, 700, blocksize / samplerate)
                callback(block, blocksize, None, FakeStatus(False))
                callback(block, blocksize, None, FakeStatus(True))

            def stop(self):
                pass

            def close(self):
                pass
        return Stream()


def test_a_board_codec_without_16k_mono_is_opened_at_its_own_rate_and_channel():
    sd = FakeSounddevice()
    stream, capture, rate, channels, name = mic.start_input_stream(
        lambda rate: mic.MicCapture(rate, channel=2), 0, sd)
    assert (rate, channels, name) == (48000, 4, "板载4麦阵列")
    assert sd.opened == [(48000, 4, 960)]  # 20 ms callbacks
    report = capture.report()
    assert report["overflows"] == 1 and report["peak_dbfs"] > -10  # channel 2 carried the tone
    assert report["captured_sample_count"] == 640  # 2 × 20 ms at 16 kHz


def test_zeros_from_the_quiet_room_gate_never_reopen_but_no_data_does():
    capture, state = mic.MicCapture(16000), {}
    for _ in range(mic.ZERO_NOTE_REPORTS + 1):  # the USB mic's AGC outputs exact zero
        capture.feed(np.zeros(16000, np.float32))
        assert mic.silence_check(capture.report(), state) is None
    assert state == {"captured": capture.captured_samples, "count": 61, "kind": "digital_silence"}
    capture.feed(sine(16000, 300, 1.0, .01))
    assert mic.silence_check(capture.report(), state) is None and state["count"] == 0
    results = [mic.silence_check(capture.report(), state)  # no callbacks at all
               for _ in range(mic.NO_DATA_REPORTS)]
    assert results[-1] == "no_data" and results[:-1] == [None] * 4


def test_a_card_that_stops_delivering_ends_the_program_for_a_clean_restart():
    """Reopening a vanished USB card in-process aborted inside PortAudio (188, 10-21)."""
    class Voice:
        ready = False

    async def watch(enabled):
        capture = mic.MicCapture(16000)  # no callbacks at all
        task = asyncio.create_task(mic.report_mic(capture, mic.MicLink(), Voice(), {},
                                                  interval=0, watch=enabled))
        try:
            async with asyncio.timeout(.5):
                await task
        except TimeoutError:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            return "still running"

    with pytest.raises(mic.DeviceLost):
        asyncio.run(watch(True))
    assert asyncio.run(watch(False)) == "still running"


def test_the_speaker_watchdog_fires_only_when_callbacks_stop():
    async def run(callbacks):
        playback, now = speaker.Playback(48000), [100.0]
        task = asyncio.create_task(speaker.watch_speaker(playback, stall=5, interval=0,
                                                         clock=lambda: now[0]))
        for _ in range(20):  # 20 s of callbacks, or of nothing
            now[0] += 1
            if callbacks:
                playback.callback_at = now[0]
            await asyncio.sleep(0)
            if task.done():
                return task.exception()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        return None

    assert asyncio.run(run(True)) is None
    assert isinstance(asyncio.run(run(False)), mic.DeviceLost)


def test_missing_cards_are_waited_for_instead_of_a_restart_loop(monkeypatch, caplog):
    monkeypatch.setattr(speaker, "list_all_devices", lambda: "0\t48000 Hz\tDOV: USB Audio")
    attempts, sleeps, refreshes = [], [], []

    def open_all():
        attempts.append(1)
        if len(attempts) < 3:
            raise mic.DeviceUnavailable("输入设备“USB Composite”匹配到 0 个")
        return "opened"

    with caplog.at_level(logging.WARNING, logger="speaker"):
        assert speaker.open_when_ready(open_all, sleep=sleeps.append,
                                       refresh=lambda: refreshes.append(1)) == "opened"
    assert sleeps == [speaker.DEVICE_RETRY_SECONDS] * 2 and len(refreshes) == 2
    warnings = [record.getMessage() for record in caplog.records]
    assert len(warnings) == 1  # the device list once, not on every attempt
    assert "USB Composite" in warnings[0] and "DOV" in warnings[0]


def test_a_lost_microphone_says_goodbye_and_exits_for_a_restart(monkeypatch):
    from websockets.asyncio.client import connect
    from websockets.asyncio.server import serve

    monkeypatch.setattr(mic, "NO_DATA_REPORTS", 2)

    async def run():
        room = FakeRoom()
        server = await serve(room.handler, "127.0.0.1", 0)
        root = f"ws://127.0.0.1:{server.sockets[0].getsockname()[1]}/robot"
        heard = []
        try:
            async with connect(f"{root}/roomv2/reb_voice", proxy=None) as listener:
                await listener.recv()  # WELCOME
                await listener.send(json.dumps({"type": "SUBSCRIBE", "data": {"types": ["*"]}}))
                await listener.recv()  # ACK
                capture = mic.MicCapture(16000)  # the card delivers nothing
                result = await asyncio.wait_for(speaker.serve(
                    "", f"{root}/roomv2/reb_voice", "roomv2", None, None,
                    mic=(f"{root}/roomv2/reb_mic", capture,
                         mic.device_identity("microphone", "USB Composite")),
                    watch_devices=True), 10)
                async with asyncio.timeout(2):
                    while not any(m["type"] == "mic.capture.stopped" for m in heard):
                        heard.append(json.loads(await listener.recv()))
        finally:
            server.close()
            await server.wait_closed()
        return result, heard

    result, heard = asyncio.run(run())
    assert result == "device_lost"
    stopped = next(m for m in heard if m["type"] == "mic.capture.stopped")
    assert stopped["data"]["reason"] == "device_lost"

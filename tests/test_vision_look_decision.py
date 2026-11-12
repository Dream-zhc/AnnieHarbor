"""The decision model judges looks the phrase rules miss (2026-11-03).

“有戴眼镜的人吗”“水桶在哪里”“你当前在什么场景下” matched no look phrase and were answered
unseen. d3 now picks whether and where she looks, seeing the newest LiveKit camera
frame; what she says still comes from the vision service (user decision 11-03).
"""

import asyncio
import base64
import io
import json
import time
import tomllib
from types import SimpleNamespace

import httpx
import pytest
from annie_adapters.decision import D3_MODEL, DecisionClient
from annie_adapters.livekit_frames import LiveKitFrames
from annie_brain.decision_policy import (
    LOOK_DIRECTIONS,
    asks_to_look,
    look_cue,
    look_question,
    look_state,
)
from annie_core import RobotConfig
from annie_runtime import HarborRuntime
from annie_runtime.__main__ import vision_intent_options
from annie_runtime.decision_route import D3Route
from PIL import Image

from tests.test_decision_d3 import D3_BASE, FakeD3
from tests.test_decision_model import EchoSocket, FakeDecision
from tests.test_phase7_runtime import ROOT
from tests.test_vision_intent import (
    answer,
    ask_held,
    intents,
    room_server,
    runtime,
    scenario,
    triggers,
    until,
)
from tests.test_wake_call_recovery import kinds, sent

JPEG = b"\xff\xd8\xff\xe0 a camera frame"
DATA_URL = "data:image/jpeg;base64," + base64.b64encode(JPEG).decode()


# ---- the question ------------------------------------------------------------------
# The user's lines of 11-03, word for word.
@pytest.mark.parametrize("text", [
    "小安妮小安妮，你看看你面前有什么？", "小安妮，你看看左边有什么。", "你看看有戴眼镜的人吗",
    "现在你看看有戴眼镜的人吗", "小安妮，小安妮，你看看眼前有什么东西呀？", "天花板有什么，",
    "左边有什么", "水桶在哪里", "饮水机在什么位置。", "你当前在什么场景 下",
])
def test_every_line_the_user_listed_looks_by_rule_or_reaches_the_decision_model(text):
    assert asks_to_look(text) or look_cue(text)


@pytest.mark.parametrize("text", ["你看看有戴眼镜的人吗", "水桶在哪里", "饮水机在什么位置。",
                                  "你当前在什么场景 下", "这里有几个人", "那穿白衣服的呢"])
def test_the_rules_miss_these_so_the_decision_model_judges_them(text):
    assert not asks_to_look(text) and look_cue(text)


@pytest.mark.parametrize("text", ["给我讲个笑话", "今天天气怎么样", "你叫什么名字", "嗯嗯好的", "现在几点了"])
def test_lines_with_nothing_to_look_at_are_answered_at_once(text):
    assert not look_cue(text)


def test_the_question_names_where_she_can_look_and_says_when_a_picture_is_attached():
    question = look_question(image=True)
    assert question["type"] == "choice"
    assert set(question["criteria"]) == {"none", *LOOK_DIRECTIONS}
    assert "附图" in question["instructions"] and "附图" not in look_question()["instructions"]
    assert LOOK_DIRECTIONS == {"front": None, "left": "look_left", "right": "look_right",
                               "up": "look_up"}
    assert look_state("那右边呢", ["a", "", "b", "c", "你看看左边有什么"]) == {
        "机器人": "小安妮", "用户前几句": ["b", "c", "你看看左边有什么"], "当前这句话": "那右边呢"}
    assert look_state("水桶在哪里") == {"机器人": "小安妮", "当前这句话": "水桶在哪里"}


def test_d3_reads_a_picture_only_as_a_top_level_data_url():
    """Live d3 (11-03): a bare base64 string is a 400; a picture in state is read as text."""
    bodies = []

    def handler(request):
        bodies.append(json.loads(request.content))
        return httpx.Response(200, json={"answers": {"look": {"choice": "front",
                                                               "confidence": .9}}})

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            client = DecisionClient(D3_BASE, "t", model=D3_MODEL, client=http, gateway=True)
            state = look_state("水桶在哪里")
            await client.ask(state, {"look": look_question(image=True)}, images=[DATA_URL])
            await client.ask(state, {"look": look_question()})
            for bad in ([base64.b64encode(JPEG).decode()], [DATA_URL] * 5, [JPEG]):
                with pytest.raises(ValueError):
                    await client.ask(state, {"look": look_question()}, images=bad)
    asyncio.run(run())
    assert len(bodies) == 2
    assert bodies[0]["images"] == [DATA_URL] and "images" not in bodies[1]
    assert "base64" not in json.dumps(bodies[0]["state"])


# ---- the whole turn -----------------------------------------------------------------
class SeeingD3(FakeD3):
    """d3 that also records the pictures it was shown."""

    def __init__(self, *answers, delay=0, **kwargs):
        super().__init__(*answers, **kwargs)
        self.images, self.delay = [], delay

    async def ask(self, state, questions, *, images=None):
        self.images.append(images)
        if self.delay:
            await asyncio.sleep(self.delay)
        return await super().ask(state, questions)


class Frames:
    def __init__(self, jpeg=JPEG, age_ms=120):
        self.jpeg, self.age_ms = jpeg, age_ms
        self.started = self.closed = False

    def start(self):
        self.started = True

    async def close(self):
        self.closed = True

    async def latest_jpeg(self):
        return None if self.jpeg is None else (self.jpeg, self.age_ms)

    def snapshot(self):
        return {"status": "connected", "frames": 1}


def looking(*answers, frames=None, delay=0, fail=False, **decision):
    route = D3Route(SeeingD3(*answers, delay=delay, fail=fail), model=D3_MODEL)
    route._mark(True, None)
    return {"d3_route": route, "camera_frames": {"a": frames or Frames()},
            "vision_look_decision": {"enabled": True, **decision}}


FRONT = {"choice": "front", "confidence": .9}
NONE = {"choice": "none", "confidence": .8}


def test_a_line_the_rules_miss_is_looked_at_when_d3_says_so(tmp_path):
    async def body(rt, client):
        await ask_held(client, "u1", "你看看有戴眼镜的人吗")
        await until(lambda: intents(rt))
        d3 = rt.d3.client
        state, questions = d3.calls[0]
        assert questions == {"look": look_question(image=True)}
        assert state["当前这句话"] == "你看看有戴眼镜的人吗"
        assert d3.images == [[DATA_URL]]  # the newest camera frame
        decided = kinds(client, "vision.intent.decided")[-1]
        assert (decided["choice"], decided["script"], decided["judge"], decided["model"],
                decided["image"], decided["frame_age_ms"], decided["stale"]) == (
            "front", None, "d3", D3_MODEL, True, 120, False)
        assert len(sent(client, "response.cancel")) == 1  # her early, unseeing reply
        assert kinds(client, "text.delta") == []
        assert triggers(rt) == []  # in front of her: no head turn
        data = intents(rt)[0]["data"]
        assert (data["question"], data["fresh_frame"]) == ("你看看有戴眼镜的人吗", False)
        assert sent(client, "response.create") == []
        rt._room_playback_receipt("a", answer(intents(rt)[0], status="ok",
                                              description="两位朋友，左边那位戴着眼镜。"))
        await until(lambda: sent(client, "response.create"))
        assert client.decision_stats["look_checks"] == 1
    scenario(tmp_path, body, **looking(FRONT))


def test_d3_picks_the_side_and_she_turns_there_first(tmp_path):
    async def body(rt, client):
        await ask_held(client, "u2", "门在哪边")
        turn_id = client.turn_id
        await until(lambda: triggers(rt))
        trigger = triggers(rt)[0]["data"]
        assert (trigger["script_id"], trigger["offset_ms"]) == ("look_left", -1)
        rt._room_playback_receipt("a", {"type": "action.finished", "data": {
            "script_id": "look_left", "conversation_id": turn_id}})
        await until(lambda: intents(rt))
        assert intents(rt)[0]["data"]["fresh_frame"] is True
        rt._room_playback_receipt("a", answer(intents(rt)[0], status="ok",
                                              description="左边是一扇木门。"))
        await until(lambda: sent(client, "response.create"))
    scenario(tmp_path, body, **looking({"choice": "left", "confidence": .8}))


def test_no_look_from_d3_lets_her_early_reply_through(tmp_path):
    async def body(rt, client):
        await ask_held(client, "u3", "你是哪里人")
        assert len(rt.d3.client.calls) == 1
        assert kinds(client, "text.delta")  # the held reply was released
        assert sent(client, "response.cancel") == []
        assert kinds(client, "vision.intent.started") == [] and intents(rt) == []
        assert kinds(client, "vision.intent.decided")[-1]["choice"] == "none"
    scenario(tmp_path, body, **looking(NONE))


def test_a_slow_d3_holds_her_answer_only_until_the_timeout(tmp_path):
    async def body(rt, client):
        started = time.perf_counter()
        await ask_held(client, "u4", "水桶在哪里")
        assert time.perf_counter() - started < .4
        decided = kinds(client, "vision.intent.decided")[-1]
        assert (decided["choice"], decided["error"]) == (None, "timeout")
        assert kinds(client, "text.delta") and intents(rt) == []
        assert rt.d3.ready and client.decision_stats["failures"] == 0  # slow is not down
    scenario(tmp_path, body, **looking(FRONT, delay=1, timeout_ms=100))


def test_a_failing_d3_answers_as_before_and_hands_over_to_dashscope(tmp_path):
    async def body(rt, client):
        await ask_held(client, "u5", "水桶在哪里")
        decided = kinds(client, "vision.intent.decided")[-1]
        assert (decided["choice"], decided["error"]) == (None, "ConnectError")
        assert kinds(client, "text.delta") and intents(rt) == []
        assert not rt.d3.ready and client.decision_stats["failures"] == 1
    scenario(tmp_path, body, **looking(fail=True))


def test_a_line_with_nothing_to_look_at_never_waits_for_d3(tmp_path):
    async def body(rt, client):
        await ask_held(client, "u6", "给我讲个笑话")
        assert rt.d3.client.calls == [] and client.decision_stats["look_checks"] == 0
        assert kinds(client, "text.delta") and kinds(client, "vision.intent.decided") == []
    scenario(tmp_path, body, **looking(FRONT))


def test_without_a_fresh_frame_d3_judges_the_words_alone(tmp_path):
    async def body(rt, client):
        await ask_held(client, "u7", "饮水机在什么位置。")
        await until(lambda: intents(rt))
        assert rt.d3.client.images == [None]
        assert rt.d3.client.calls[0][1] == {"look": look_question(image=False)}
        decided = kinds(client, "vision.intent.decided")[-1]
        assert (decided["image"], decided["frame_age_ms"]) == (False, None)
    scenario(tmp_path, body, **looking(FRONT, frames=Frames(jpeg=None)))


def test_dashscope_judges_without_a_picture_while_d3_is_down(tmp_path):
    async def body(rt, client):
        rt.d3.failed("ConnectError")
        client.decision = FakeDecision(FRONT)
        await ask_held(client, "u8", "你当前在什么场景 下")
        await until(lambda: intents(rt))
        assert client.decision.calls[0][1] == {"look": look_question(image=False)}
        assert rt.d3.client.calls == []
        assert kinds(client, "vision.intent.decided")[-1]["judge"] == "decision"
    scenario(tmp_path, body, **looking())


def test_a_typed_question_is_judged_the_same_way(tmp_path):
    async def body(rt, client):
        accepted = await rt.realtime_text("a", "水桶在哪里")
        await until(lambda: intents(rt))
        assert intents(rt)[0]["data"]["conversation_id"] == accepted["turn_id"]
        assert sent(client, "response.create") == []
        rt._room_playback_receipt("a", answer(intents(rt)[0], status="ok",
                                              description="门边的角落里有一桶水。"))
        await until(lambda: sent(client, "response.create"))
    scenario(tmp_path, body, **looking(FRONT))


def test_switched_off_d3_is_never_asked(tmp_path):
    async def body(rt, client):
        assert client.look_decision is None and rt.vision_look_decision is None
        await ask_held(client, "u9", "水桶在哪里")
        assert rt.d3.client.calls == [] and kinds(client, "text.delta")
        assert rt.health()["vision_look_decision"] == {"enabled": False, "camera_frames": {}}
    options = looking(FRONT)
    options.pop("vision_look_decision")
    scenario(tmp_path, body, **options)


def test_health_shows_the_judge_settings_and_the_camera_frames(tmp_path):
    frames = Frames()

    async def body(rt, client):
        assert frames.started and client.camera_frames is frames
        assert rt.health()["vision_look_decision"] == {
            "enabled": True, "scope": "cue", "timeout_ms": 700, "image": True,
            "camera_frames": {"a": {"status": "connected", "frames": 1}}}
        assert rt.health()["vision_intent"] == {"configured_robots": ["a"], "route": "cloud"}
    scenario(tmp_path, body, **looking(frames=frames))
    assert frames.closed


# ---- configuration ------------------------------------------------------------------
LIVEKIT_ENV = {"ANNIE_EXHIBIT_LIVEKIT_URL": "ws://192.168.1.13:7880",
               "ANNIE_EXHIBIT_LIVEKIT_API_KEY": "devkey",
               "ANNIE_EXHIBIT_LIVEKIT_API_SECRET": "lk-secret",
               "ANNIE_EXHIBIT_LIVEKIT_ROOM_ANNIE_A": "robot-test"}


def test_options_come_from_runtime_defaults_and_the_exhibit_livekit_env():
    with (ROOT / "config/runtime_defaults.toml").open("rb") as file:
        vision = tomllib.load(file)["vision"]
    options = vision_intent_options(vision, LIVEKIT_ENV, ["annie-a", "annie-b"])
    assert options["vision_look_decision"] == {"enabled": True, "scope": "cue",
                                               "timeout_ms": 700, "image": True}
    assert options["livekit"] == {"url": "ws://192.168.1.13:7880", "api_key": "devkey",
                                  "api_secret": "lk-secret", "source": "camera-a",
                                  "rooms": {"annie-a": "robot-test"}}
    off = vision_intent_options(vision, {**LIVEKIT_ENV, "ANNIE_VISION_LOOK_DECISION_ENABLED": "0"},
                                ["annie-a"])
    assert off["vision_look_decision"]["enabled"] is False
    no_secret = {k: v for k, v in LIVEKIT_ENV.items() if not k.endswith("SECRET")}
    assert vision_intent_options(vision, no_secret, ["annie-a"])["livekit"] is None
    assert vision_intent_options({}, {}, [])["vision_look_decision"]["enabled"] is False


def test_the_runtime_watches_livekit_only_for_cameras_d3_can_be_shown(tmp_path):
    livekit = {"url": "ws://192.168.1.13:7880", "api_key": "devkey", "api_secret": "lk-secret",
               "source": "camera-a", "rooms": {"a": "robot-test"}}
    options = looking()
    options.pop("camera_frames")
    rt = runtime(tmp_path, livekit=livekit, **options)
    frames = rt.camera_frames["a"]
    assert isinstance(frames, LiveKitFrames) and rt.realtime["a"].camera_frames is frames
    assert (frames.room_name, frames.source, frames.status) == ("robot-test", "camera-a", "idle")
    assert "lk-secret" not in json.dumps(rt.health()["vision_look_decision"])
    words_only = runtime(tmp_path, livekit=livekit,
                         **{**options, "vision_look_decision": {"enabled": True, "image": False}})
    assert words_only.camera_frames == {} and words_only.realtime["a"].camera_frames is None
    assert words_only.realtime["a"].look_decision["image"] is False
    no_judge = HarborRuntime(
        [RobotConfig("a", room_id="roomv2")], tmp_path / "n.db", room_server(),
        realtime_key="fake", realtime_socket_factory=EchoSocket,
        vision_look_decision={"enabled": True}, livekit=livekit)
    assert no_judge.vision_look_decision is None and no_judge.camera_frames == {}
    for bad in ({"enabled": True, "scope": "every"}, {"enabled": True, "timeout_ms": 50},
                {"enabled": "yes"}):
        with pytest.raises(ValueError):
            runtime(tmp_path, vision_look_decision=bad)


# ---- LiveKit frames -----------------------------------------------------------------
class Publication:
    def __init__(self, kind):
        self.kind, self.subscribed = kind, False

    def set_subscribed(self, value):
        self.subscribed = value


class Participant:
    def __init__(self, identity, *kinds):
        self.identity = identity
        self.track_publications = {f"{identity}-{i}": Publication(kind)
                                   for i, kind in enumerate(kinds)}


class VideoFrame:
    def __init__(self, width, height):
        self.width, self.height = width, height

    def convert(self, kind):
        assert kind == "rgb24"
        return SimpleNamespace(data=bytes(self.width * self.height * 3), width=self.width,
                               height=self.height)


def fake_rtc(participants, frames):
    rooms = []

    class Room:
        def __init__(self):
            self.handlers, self.remote_participants = {}, {}
            rooms.append(self)

        def on(self, event, handler):
            self.handlers[event] = handler

        async def connect(self, url, token, options):
            self.url, self.token, self.options = url, token, options
            self.remote_participants = {p.identity: p for p in participants}

        async def disconnect(self):
            pass

    class VideoStream:
        def __init__(self, track, capacity=0):
            self.track, self.capacity = track, capacity

        def __aiter__(self):
            return self._events()

        async def _events(self):
            for frame in frames:
                yield SimpleNamespace(frame=frame)
            await asyncio.Event().wait()  # a live track stays open

        async def aclose(self):
            pass

    rtc = SimpleNamespace(
        Room=Room, VideoStream=VideoStream,
        RoomOptions=lambda auto_subscribe=True: SimpleNamespace(auto_subscribe=auto_subscribe),
        TrackKind=SimpleNamespace(KIND_VIDEO="video", KIND_AUDIO="audio"),
        VideoBufferType=SimpleNamespace(RGB24="rgb24"))
    return rtc, rooms


def test_livekit_frames_watch_only_the_source_camera_and_keep_the_newest_frame():
    async def run():
        camera = Participant("camera-a", "audio", "video")
        visitor = Participant("visitor", "video")
        rtc, rooms = fake_rtc([camera, visitor], [VideoFrame(1920, 1080)])
        frames = LiveKitFrames("ws://192.168.1.13:7880", "devkey", "lk-secret", "robot-test",
                               "camera-a", label="a", rtc=rtc)
        assert await frames.latest_jpeg() is None  # nothing yet
        frames.start()
        await until(lambda: frames.status == "waiting_track")
        room = rooms[0]
        assert room.options.auto_subscribe is False
        assert [p.subscribed for p in camera.track_publications.values()] == [False, True]
        assert [p.subscribed for p in visitor.track_publications.values()] == [False]
        claims = json.loads(base64.urlsafe_b64decode(room.token.split(".")[1] + "=="))
        assert claims["iss"] == "devkey" and claims["video"] == {
            "roomJoin": True, "room": "robot-test", "canPublish": False, "canSubscribe": True,
            "canPublishData": False, "hidden": True}
        room.handlers["track_subscribed"](SimpleNamespace(kind="video"), None, visitor)
        assert frames.status == "waiting_track"  # someone else's camera is not hers
        room.handlers["track_subscribed"](SimpleNamespace(kind="video"), None, camera)
        await until(lambda: frames.frames == 1)
        assert frames.status == "connected"
        jpeg, age_ms = await frames.latest_jpeg()
        assert Image.open(io.BytesIO(jpeg)).size == (448, 252) and 0 <= age_ms < 1000
        frames.max_age_ms = 100
        frames._frame = (frames._frame[0], time.monotonic() - 1)
        assert await frames.latest_jpeg() is None  # a stale frame is no picture
        assert "lk-secret" not in json.dumps(frames.snapshot())
        await frames.close()
        assert frames._task is None
    asyncio.run(run())


def test_livekit_frames_need_a_websocket_url_and_explicit_room_and_source():
    for url in ("http://192.168.1.13:7880", "ws://user:pw@192.168.1.13:7880",
                "ws://192.168.1.13:7880?token=x"):
        with pytest.raises(ValueError):
            LiveKitFrames(url, "devkey", "lk-secret", "robot-test", "camera-a")
    with pytest.raises(ValueError):
        LiveKitFrames("ws://192.168.1.13:7880", "devkey", "lk-secret", "robot-test", "")

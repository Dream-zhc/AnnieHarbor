"""Fixes from the 2026-10-20 evening logs (docs/运行日志/2026-10-21).

Events never spoke once a reply had played, the KWS backlog killed the session,
real follow-up questions were dropped, "调到50" lost its "声音" turn, and the
model named people from the chat instead of the camera.
"""

import asyncio
import time
from types import SimpleNamespace

from annie_core import NormalizedEvent, RobotConfig
from annie_runtime import HarborRuntime, TurnCoordinator
from annie_runtime.events import EventService
from annie_runtime.room_monitor import RoomMonitor
from annie_runtime.speech import RECEIPT_STALE_SECONDS, SpeechOutput
from annie_runtime.wakeword import FRAME_BYTES, STREAM_BACKLOG_LIMIT_BYTES, WakeAdmission

from tests.test_decision_model import EchoSocket
from tests.test_visual_observations import ME, message, person


def speech(**fields):
    state = {"turn_id": "t1", "worker": None, "playback_state": "unknown",
             "_last_receipt_t": None, "local_output_completed_at": None, "samples": 0}
    return SimpleNamespace(**{**state, **fields})


def test_a_finished_reply_no_longer_counts_as_playing():
    active = SpeechOutput.output_active
    now = time.monotonic()
    assert not active(speech(turn_id=None))
    assert active(speech(worker=SimpleNamespace(done=lambda: False)))  # synthesizing
    assert active(speech(playback_state="progress", _last_receipt_t=now))
    assert not active(speech(playback_state="drained", _last_receipt_t=now))
    assert not active(speech(playback_state="progress",
                             _last_receipt_t=now - RECEIPT_STALE_SECONDS - 1))  # unplugged
    # No speaker at all: about as long as the audio that was synthesized.
    assert active(speech(local_output_completed_at=now, samples=24000 * 2))
    assert not active(speech(local_output_completed_at=now - 4, samples=24000 * 2))


def test_events_speak_after_an_earlier_reply_has_played():
    """10/20: face results, greetings and the radar reply expired unspoken."""
    submitted = []

    class Client:
        ready = True
        speech_active = False
        turn_id = response_id = None
        _last_conversation_activity_t = 0.0
        speech = speech(playback_state="drained", _last_receipt_t=time.monotonic(),
                        output_active=lambda: False)

        def publish(self, *_args, **_fields):
            pass

        async def submit_text(self, text, **kwargs):
            submitted.append(text)
            return {"turn_id": "x"}

    identity = {"robot_id": "a", "chat_id": "c", "persona_id": "p", "session_epoch": 1,
                "generation": 1}
    robot = SimpleNamespace(coordinator=TurnCoordinator(), session_state=lambda: dict(identity))
    runtime = SimpleNamespace(robots={"a": robot}, realtime={"a": Client()}, started=True,
                              session_state=lambda _robot: dict(identity))

    async def run():
        events = EventService(runtime)
        events.route("a", NormalizedEvent(
            kind="vision.face_registered", source="vision", event_id="face_1",
            payload={"id": "face_1", "name": "康兴", "state": "completed"},
            ts_ms=time.time_ns() // 1_000_000, ttl_ms=30_000), "face:a")
        for _ in range(50):
            if submitted:
                break
            await asyncio.sleep(.01)
        await events.close()

    asyncio.run(run())
    assert submitted and "你刚刚记住了康兴的脸" in submitted[0]


def test_a_kws_backlog_drops_old_audio_instead_of_failing():
    class Slow:
        def new_stream(self):
            return self

        async def detected(self, _pcm):
            await asyncio.sleep(10)
            return False

    async def run():
        wake = WakeAdmission(Slow())
        frame = bytes(FRAME_BYTES)
        for _ in range(STREAM_BACKLOG_LIMIT_BYTES // FRAME_BYTES + 5):
            assert await wake.frames(frame) == ()
        assert wake.backlog_drops >= 1 and wake.error is None
        wake.reset()

    asyncio.run(run())


def live(tmp_path, body, **kwargs):
    async def run():
        rt = HarborRuntime([RobotConfig("a", room_id="roomv2")], tmp_path / "rt.db",
                           realtime_key="fake", realtime_socket_factory=EchoSocket, **kwargs)
        await rt.start()
        client = rt.realtime["a"]
        try:
            for _ in range(150):
                if client.ready:
                    break
                await asyncio.sleep(.01)
            await body(rt, client)
        finally:
            await rt.close()
    asyncio.run(run())


def test_the_llm_judge_decides_and_an_outage_never_silences_her(tmp_path):
    """qwen-flash replaced the decision model for addressee (user decision 10/21)."""
    from annie_brain.decision_policy import ADDRESSEE_SYSTEM_FOLLOWUP, ADDRESSEE_SYSTEM_IDLE

    class Judge:
        def __init__(self):
            self.calls, self.answer = [], "robot"

        async def judge(self, system, user):
            self.calls.append((system, user))
            if self.answer == "boom":
                raise TimeoutError()
            return self.answer

    class Unclear:
        async def ask(self, _state, _questions):
            return {"to": {"choice": "unclear", "confidence": .2}}

    async def body(_rt, client):
        judge = Judge()
        client.addressee_judge = judge
        client.wake = SimpleNamespace(state="FOLLOWUP")
        assert await client._decide_addressee("穿着黑色衣服的人吗？", "i1") == "robot"
        system, user = judge.calls[-1]
        assert system == ADDRESSEE_SYSTEM_FOLLOWUP and "穿着黑色衣服的人吗？" in user
        judge.answer = "other"
        assert await client._decide_addressee("他没接视觉吧", "i2") == "not_robot"
        judge.answer = "boom"
        assert await client._decide_addressee("你好", "i3") == "robot"
        client.wake = None
        judge.answer = "other"
        await client._decide_addressee("我们下午几点开会", "i4")
        assert judge.calls[-1][0] == ADDRESSEE_SYSTEM_IDLE  # she is not waiting
        # Without the judge the decision model keeps its old rule for "unclear".
        client.addressee_judge, client.decision = None, Unclear()
        assert await client._decide_addressee("介绍一下你看到的人", "i5") == "not_robot"

    live(tmp_path, body)


def test_addressee_judge_adapter_reads_only_robot_or_other():
    import httpx
    from annie_adapters.addressee import AddresseeJudge

    seen = []

    def handler(request):
        body = __import__("json").loads(request.content)
        seen.append(body)
        content = {"to": "other"} if "开会" in body["messages"][1]["content"] else {"to": "maybe"}
        return httpx.Response(200, json={"choices": [{"message": {
            "content": __import__("json").dumps(content)}}]})

    async def run():
        http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        judge = AddresseeJudge("key", model="qwen-flash", client=http)
        assert await judge.judge("sys", '{"当前这句话":"我们下午几点开会"}') == "other"
        try:
            await judge.judge("sys", '{"当前这句话":"嗯"}')
            raise AssertionError("an unknown answer must raise")
        except ValueError:
            pass
        await http.aclose()

    asyncio.run(run())
    assert seen[0]["model"] == "qwen-flash" and seen[0]["temperature"] == 0
    assert seen[0]["response_format"] == {"type": "json_object"}


def test_set_volume_to_50_after_saying_voice_in_the_turn_before(tmp_path):
    class Speaker:
        volume = 100

        def current(self):
            return self.volume

        async def set(self, volume):
            self.volume = volume
            return {"status": "set", "volume": volume}

    async def body(_rt, client):
        client.speaker_volume = Speaker()
        client._create_turn("小安妮，声音。")
        client._finish("completed")
        client._create_turn("调到50。")
        assert (await client._set_speaker_volume({"level": 50}))["volume"] == 50
        client._finish("completed")
        for said in ("声音调到40", "音量调到30"):
            client._create_turn(said)
            assert (await client._set_speaker_volume({"level": int(said[-2:])}))["status"] == "set"
            client._finish("completed")

    live(tmp_path, body)


def test_look_closely_names_only_whom_the_camera_recognised():
    clock = SimpleNamespace(now=100.0)
    monitor = RoomMonitor("roomv2", clock=lambda: clock.now)
    me = dict(ME, action_detail="一边看屏幕一边敲键盘")
    for _ in range(3):
        clock.now += 1
        monitor.on_visual(message([me, person("person_9")],
                                  description="一位穿白色上衣的男士坐在电脑前，右边站着一个穿黑外套的人"))
    view = monitor.view_details()
    first, second = view["people"]
    assert first == {"who": "张三", "recognized": True, "looking_at_you": True,
                     "where": "在我面前", "doing": "专注看电脑",
                     "detail": "一边看屏幕一边敲键盘", "holding": "黑色的手机"}
    assert second["recognized"] is False and second["who"] == "一位朋友"
    assert "白色上衣" in view["scene"] and view["around"]
    clock.now += 10
    assert monitor.view_details() is None  # stale: say it cannot be seen now


def test_look_closely_tool_and_the_rules_against_following_the_user(tmp_path):
    async def body(_rt, client):
        client.visual_details = lambda: {"seconds_ago": .5, "people": [
            {"who": "一位朋友", "recognized": False, "looking_at_you": True}],
            "scene": "一位穿白色上衣的人站在桌边", "around": "办公室", "objects": []}
        client._create_turn("你看我穿的是黑衣服吗")
        await client._tool_call({"call_id": "l1", "name": "look_closely", "arguments": "{}"})
        result = next(e for e in client.events if e["kind"] == "tool.result"
                      and e["call_id"] == "l1")["result"]
        assert result["status"] == "ok" and "不附和" in result["instruction"]
        assert result["people"][0]["recognized"] is False
        instructions = client._combined_instructions("【眼前画面】你正看着一位朋友。")
        assert "不能用对话里有人自称或别人说的名字叫TA" in instructions
        client._finish("completed")
        client.visual_details = lambda: None
        client._create_turn("你看看我")
        await client._tool_call({"call_id": "l2", "name": "look_closely", "arguments": "{}"})
        result = next(e for e in client.events if e["kind"] == "tool.result"
                      and e["call_id"] == "l2")["result"]
        assert result["status"] == "no_view"

    live(tmp_path, body)

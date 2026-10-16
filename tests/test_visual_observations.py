"""visualllm observations v1: the robot's view for the model, smoothed over a few messages.

Fixtures follow the real camera-a payload of 2026-10-16 (field names, generic
labels, face-only/body-only tracks); the person's name is replaced.
"""

import asyncio

from annie_runtime.room_monitor import (
    ABSENT_MISSES,
    OBSERVATION_CHANGE_TTL_SECONDS,
    OBSERVATION_MINOR_REFRESH_SECONDS,
    RoomMonitor,
)


class Clock:
    def __init__(self):
        self.now = 500.0

    def __call__(self):
        return self.now


def person(subject_id, label="一位朋友", *, name=None, gaze=False, text="在右边，离我比较近",
           action=None, holding=None, expression=None, confidence=None):
    return {"subject_id": subject_id, "label": name or label, "name": name, "known": bool(name),
            "identity_state": "identified" if name else "no_face",
            "identity_confidence": confidence,
            "position": {"side": "right", "distance": "near", "text": text,
                         "bbox_norm": [.8, .1, 1.0, .8]},
            "action": action, "holding": holding, "expression": expression,
            "is_gaze_target": gaze}


def message(people, *, environment="办公室环境，有电脑和耳机", objects=(), version=1,
            description="（视觉端拼好的描述）"):
    return {"type": "visualllm", "schema_version": 2, "description": description,
            "facts": {"subjects": [], "relations": [], "uncertainties": [], "changes": []},
            "observations": {"version": version, "people_count": len(people),
                             "looking_at": next((p["subject_id"] for p in people
                                                 if p["is_gaze_target"]), None),
                             "people": list(people), "environment": environment,
                             "objects": list(objects), "changes": []}}


ME = person("person_1", name="张三", gaze=True, text="在我面前", action="专注看电脑",
            holding="黑色的手机", confidence=.52)
FRIEND = person("person_10", action="看书")


def feed(monitor, clock, people, times=1, **kwargs):
    for _ in range(times):
        clock.now += 1
        monitor.on_visual(message(people, **kwargs))
    return monitor.current_visual(connected=True)


def test_the_robot_s_gaze_target_comes_first_and_nulls_are_never_mentioned():
    clock = Clock()
    monitor = RoomMonitor("roomv2", clock=clock)
    text = feed(monitor, clock, [FRIEND, ME], times=2, objects=[
        {"name": "杯子", "near_subject_id": "person_1", "near_label": "张三"}])
    assert text == ("【眼前画面，2人】你正看着张三（在我面前）：专注看电脑，手里拿着黑色的手机。"
                    "一位朋友（在右边，离我比较近）：看书。周围：办公室环境，有电脑和耳机。"
                    "物品：杯子（张三身边）。")
    assert "None" not in text and "bbox" not in text and "另有" not in text
    assert monitor.snapshot(sam_connected=None, visual_connected=True,
                            visual_configured=True)["visual"]["source"] == "observations_v1"


def test_a_name_needs_two_messages_and_a_brief_misread_does_not_stick():
    clock = Clock()
    monitor = RoomMonitor("roomv2", clock=clock)
    assert feed(monitor, clock, [ME]).startswith("【眼前画面】你正看着一位朋友")  # not yet
    assert "你正看着张三" in feed(monitor, clock, [ME])
    stranger = person("face_4", label="另一位朋友")
    feed(monitor, clock, [ME, stranger], times=2)
    misread = dict(stranger, label="张三", name="张三", known=True, confidence=.4)
    text = feed(monitor, clock, [ME, misread])  # one frame of someone else's name
    assert text.count("张三") == 1 and "一位朋友" in text


def test_edge_flicker_is_neither_a_new_person_nor_a_departure():
    clock = Clock()
    monitor = RoomMonitor("roomv2", clock=clock)
    feed(monitor, clock, [ME, FRIEND], times=4)  # warm-up: they were already here
    steady = feed(monitor, clock, [ME, FRIEND])
    assert "刚才" not in steady and "2人" in steady
    feed(monitor, clock, [ME], times=ABSENT_MISSES - 1)  # missed at the frame edge
    back = feed(monitor, clock, [ME, FRIEND])
    assert "2人" in back and "刚才" not in back
    gone = feed(monitor, clock, [ME], times=ABSENT_MISSES)
    assert "刚才：一位朋友离开" in gone and "【眼前画面】" in gone
    newcomer = person("person_20", action="走过来")
    feed(monitor, clock, [ME], times=1)
    arrived = feed(monitor, clock, [ME, newcomer], times=2)
    assert "刚才：一位朋友离开" not in arrived  # same label came back: no net news
    clock.now += OBSERVATION_CHANGE_TTL_SECONDS
    assert "刚才" not in feed(monitor, clock, [ME, newcomer])


def test_per_frame_wording_waits_but_who_the_robot_looks_at_does_not():
    clock = Clock()
    monitor = RoomMonitor("roomv2", clock=clock)
    first = feed(monitor, clock, [ME, FRIEND], times=3)
    synonym = dict(ME, action="专注做事", expression="微笑")
    assert feed(monitor, clock, [synonym, FRIEND], times=3) == first  # waits for the refresh
    clock.now += OBSERVATION_MINOR_REFRESH_SECONDS
    text = feed(monitor, clock, [synonym, FRIEND])
    assert "专注做事，手里拿着黑色的手机，表情微笑" in text  # smile held three messages
    turned = [dict(synonym, is_gaze_target=False), dict(FRIEND, is_gaze_target=True)]
    assert feed(monitor, clock, turned).startswith("【眼前画面，2人】你正看着一位朋友")


def test_other_versions_fall_back_and_an_empty_room_is_still_a_fact():
    clock = Clock()
    monitor = RoomMonitor("roomv2", clock=clock)
    fallback = feed(monitor, clock, [ME], version=2, description="张三在我面前。")
    assert fallback.startswith("画面描述：张三在我面前。\n结构化事实：")  # the old v2 path
    assert feed(monitor, clock, [], description="") == "【眼前画面】画面里没有人。周围：办公室环境，有电脑和耳机。"


def test_the_realtime_session_and_the_addressee_check_see_the_compact_view(tmp_path):
    from annie_core import RobotConfig
    from annie_runtime import HarborRuntime

    from tests.test_decision_model import EchoSocket

    async def run():
        rt = HarborRuntime([RobotConfig("a", room_id="roomv2")], tmp_path / "rt.db",
                           realtime_key="fake", realtime_socket_factory=EchoSocket)
        await rt.start()
        try:
            client = rt.realtime["a"]
            client.visual_observer = lambda: rt.room_monitors["a"].current_visual(connected=True)
            for _ in range(2):
                rt._room_visual_event("a", message([ME, FRIEND]))
            assert client._fresh_visual().startswith("【眼前画面，2人】你正看着张三")
            state = client._addressee_state_for("你看我手里拿的是什么")
            assert state["画面"].startswith("【眼前画面，2人】你正看着张三")
        finally:
            await rt.close()
    asyncio.run(run())


def test_unchanged_face_and_gesture_streams_are_not_routed_every_frame(tmp_path):
    from annie_core import RobotConfig
    from annie_runtime import PERCEPTION_REPEAT_SECONDS, HarborRuntime

    async def run():
        rt = HarborRuntime([RobotConfig("a", room_id="roomv2")], tmp_path / "rt.db")
        await rt.start()
        rt.bind_input_source("room:roomv2:visualdata-event", ["a"])  # done by the Room setup
        try:
            def routed():
                return rt.robots["a"].snapshot.event_count

            idle = {"type": "vision.perception", "kind": "gesture",
                    "gesture": {"state": "idle", "active_finger_length_ratio": 0.0}}
            for index in range(30):  # ~1 s of the real 27 msg/s stream
                rt._room_external_event("a", "visualdata", dict(
                    idle, gesture={"state": "idle", "active_finger_length_ratio": index / 100}))
            assert routed() == 1
            rt._room_external_event("a", "visualdata", dict(
                idle, gesture={"state": "active", "active_finger_length_ratio": .4}))
            assert routed() == 2  # a real change goes through at once
            key = ("a", "gesture")
            signature, at = rt._perception_last[key]
            rt._perception_last[key] = (signature, at - PERCEPTION_REPEAT_SECONDS - 1)
            rt._room_external_event("a", "visualdata", dict(
                idle, gesture={"state": "active", "active_finger_length_ratio": .5}))
            assert routed() == 3  # and a repeat every few seconds
            face = {"type": "vision.perception", "kind": "face",
                    "tracks": [{"name": "张三", "expression": "中性", "area_ratio": .07}]}
            rt._room_external_event("a", "visualdata", face)
            rt._room_external_event("a", "visualdata", face)
            rt._room_external_event("a", "visualdata", dict(face, tracks=[
                {"name": "张三", "expression": "开心", "area_ratio": .07}]))
            assert routed() == 5
        finally:
            await rt.close()
    asyncio.run(run())


def test_neutral_and_negative_expressions_never_reach_the_model():
    clock = Clock()
    monitor = RoomMonitor("roomv2", clock=clock)
    for expression in ("中性", "厌恶", "愤怒", "悲伤"):
        clock.now += OBSERVATION_MINOR_REFRESH_SECONDS
        text = feed(monitor, clock, [dict(ME, expression=expression)], times=4)
        assert "表情" not in text, expression

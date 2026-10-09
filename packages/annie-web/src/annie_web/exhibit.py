"""Read-only, provenance-preserving exhibition telemetry.

This projection must never invent a health/attention/audio/latency metric.
The intranet display reads current conversation and fresh visual observations.
"""

import time


def _mapping(value):
    return value if isinstance(value, dict) else {}


def _recent_actions(payload):
    requests = _mapping(payload).get("requests")
    if not isinstance(requests, list):
        return []
    # The stored action request is evidence of an instruction, NOT device motion.
    return [
        {
            "call_id": row.get("call_id"),
            "turn_id": row.get("turn_id"),
            "generation": row.get("generation"),
            "kind": row.get("kind"),
            "value": row.get("value"),
            "status": row.get("status", "unknown"),
            "real_device_executed": False,
            "animation_kind": "command_preview",
        }
        # RuntimeStore lists newest first. Select the most recent requests,
        # then expose them in chronological order for the dashboard.
        for row in reversed(requests[:8])
        if isinstance(row, dict)
    ]


def _visible_visual(visual, now_ms):
    """Display only an online observation received within the freshness window."""
    age_ms = visual.get("age_ms")
    if (visual.get("state") != "online" or
            type(age_ms) not in (int, float) or not 0 <= age_ms <= 10_000):
        return None, None
    received_at = now_ms - int(age_ms)
    return visual.get("description"), received_at


async def exhibit_snapshot(runtime, robot_id: str):
    """Read the existing bounded snapshots; never wait for a new LLM/Room action."""
    robot = runtime.robots[robot_id]
    rt = _mapping(runtime.realtime_state(robot_id))
    speech = _mapping(rt.get("speech_output"))
    inputs = _mapping(runtime.input_stats(robot_id))
    room = _mapping(runtime.room_observations(robot_id))
    actions = await runtime.action_list(robot_id)
    health = _mapping(runtime.health())
    performance = _mapping(health.get("performance"))
    visual = _mapping(room.get("visual"))
    now_ms = int(time.time() * 1000)
    description, visual_at_ms = _visible_visual(visual, now_ms)
    realtime_events = _mapping(runtime.realtime_events(robot_id, after=0)).get("events", [])
    allowed_kinds = {
        "speech.started", "speech.stopped", "transcript.done", "turn.started",
        "turn.done", "text.delta", "action.state", "tts.first_audio",
        "tts.first_published", "tts.playback.started", "tts.playback.drained",
        "tts.playback.stopped", "viseme.aligned", "tool.called", "tool.result",
    }
    events = [
        {"kind": item.get("kind"), "turn_id": item.get("turn_id"),
         "ts_ms": item.get("ts_ms"), "seq": item.get("seq"),
         "source": item.get("source") if item.get("kind", "").startswith("tts.playback.") else None}
        for item in realtime_events[-48:]
        if isinstance(item, dict) and item.get("kind") in allowed_kinds
    ] if isinstance(realtime_events, list) else []

    return {
        "schema": "annie.exhibit.v1",
        "generated_at_ms": now_ms,
        "robot": {
            "robot_id": robot_id,
            "room_id": robot.config.room_id,
            "state": robot.snapshot.state,
            "generation": robot.generation,
            "ready": bool(rt.get("ready")),
        },
        "access_mode": "intranet",
        "consent": True,  # Compatibility for older open display tabs; no consent gate.
        "realtime": {
            "state": rt.get("state", "not_configured"),
            "wakeword": _mapping(rt.get("wakeword")),
            "speech_active": rt.get("speech_active"),
            "speech_output_state": speech.get("state"),
            "playback_state": speech.get("playback_state"),
            "playback_source": speech.get("playback_source"),
            "viseme": speech.get("viseme"),
            "playback_evidence": speech.get("playback_evidence"),
            "turn_id": speech.get("turn_id"),
            "user_text": robot.snapshot.last_input,
            "assistant_text": robot.snapshot.last_reply,
            "last_turn_timeline": None,  # Full diagnostics remain in the workbench.
        },
        "audio": {
            "frames_received": inputs.get("frames_received"),
            "frames_accepted": inputs.get("accepted"),
            "frames_dropped": inputs.get("dropped"),
            "mic_connected": inputs.get("connected"),
            "aec": _mapping(inputs.get("aec")),
        },
        "vision": {
            "description": description,
            "updated_at_ms": visual_at_ms,
            "availability": (
                "reported" if description else "no_observation"
            ),
        },
        "actions": _recent_actions(actions),
        "events": events,
        "performance": {
            "source": "process_probe",
            "process_cpu_pct": performance.get("cpu_percent"),
            "rss_peak_mb": performance.get("rss_peak_mb"),
            "event_loop_lag_ms": performance.get("loop_lag_ms"),
            "samples": performance.get("samples", 0),
        },
        "memory": {"recalled": None, "status": "not_exposed"},
        "device_pose": {"yaw": None, "pitch": None, "roll": None, "status": "no_feedback"},
        "source": "runtime_observed",
    }

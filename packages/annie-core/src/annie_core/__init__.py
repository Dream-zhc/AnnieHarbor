"""Small internal domain types. Room wire parsing stays in annie-adapters."""

from dataclasses import dataclass


class InputRejectedError(RuntimeError):
    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


@dataclass(frozen=True)
class AudioFormat:
    codec: str
    sample_rate: int
    channels: int
    frame_ms: int
    bytes_per_frame: int

    def __post_init__(self):
        if self.codec != "pcm_s16le":
            raise ValueError("only pcm_s16le is supported by the verified mic contract")
        for name, value in (
            ("sample_rate", self.sample_rate),
            ("channels", self.channels),
            ("frame_ms", self.frame_ms),
            ("bytes_per_frame", self.bytes_per_frame),
        ):
            if type(value) is not int or value <= 0:
                raise ValueError(f"{name} must be a positive integer")
        expected = self.sample_rate * self.frame_ms * self.channels * 2
        if expected % 1000 or expected // 1000 != self.bytes_per_frame:
            raise ValueError("bytes_per_frame does not match S16LE sample rate/channels/frame_ms")


@dataclass(frozen=True)
class PcmFrame:
    robot_id: str
    source_id: str
    payload: bytes
    sequence: int
    received_at: float
    enqueued_at: float


@dataclass(frozen=True)
class NormalizedEvent:
    kind: str
    payload: dict
    source: str | None = None
    event_id: str | None = None
    ts_ms: int | None = None
    ttl_ms: int | None = None
    confidence: float | None = None
    target_robot_id: str | None = None


@dataclass(frozen=True)
class RobotConfig:
    robot_id: str
    room_id: str = "demo-room"
    persona_id: str = "annie"
    enabled: bool = True
    listen_enabled: bool = False
    publish_enabled: bool = False
    queue_capacity: int = 16
    fake_delay_ms: int = 100
    audio_queue_capacity: int = 25
    audio_max_age_ms: int = 500

    def __post_init__(self):
        if not all(isinstance(s, str) and s.strip() for s in (self.robot_id, self.room_id, self.persona_id)):
            raise ValueError("robot_id/room_id/persona_id must be non-empty strings")
        if any(type(value) is not bool for value in (self.enabled, self.listen_enabled, self.publish_enabled)):
            raise ValueError("enabled/listen_enabled/publish_enabled must be booleans")
        if type(self.queue_capacity) is not int or self.queue_capacity < 1:
            raise ValueError("queue_capacity must be a positive integer")
        if type(self.fake_delay_ms) is not int or not 0 <= self.fake_delay_ms <= 60000:
            raise ValueError("fake_delay_ms must be an integer between 0 and 60000")
        if type(self.audio_queue_capacity) is not int or self.audio_queue_capacity < 1:
            raise ValueError("audio_queue_capacity must be a positive integer")
        if type(self.audio_max_age_ms) is not int or not 1 <= self.audio_max_age_ms <= 60000:
            raise ValueError("audio_max_age_ms must be an integer between 1 and 60000")
        if self.publish_enabled:
            raise ValueError("Phase 2 has no real Room output path")


@dataclass
class RobotSnapshot:
    robot_id: str
    room_id: str
    persona_id: str
    state: str = "stopped"
    current_request_id: str | None = None
    last_input: str | None = None
    last_reply: str | None = None
    completed: int = 0
    cancelled: int = 0
    failures: int = 0
    queue_depth: int = 0
    error: str | None = None
    queue_wait_ms: float | None = None
    service_ms: float | None = None
    elapsed_ms: float | None = None
    audio_queue_depth: int = 0
    audio_accepted: int = 0
    audio_dropped: int = 0
    audio_rejected: int = 0
    event_count: int = 0
    last_input_source: str | None = None
    last_event_kind: str | None = None

    def to_dict(self):
        return {
            "robot_id": self.robot_id,
            "room_id": self.room_id,
            "persona_id": self.persona_id,
            "state": self.state,
            "current_request_id": self.current_request_id,
            "last_input": self.last_input,
            "last_reply": self.last_reply,
            "completed": self.completed,
            "cancelled": self.cancelled,
            "failures": self.failures,
            "queue_depth": self.queue_depth,
            "error": self.error,
            "queue_wait_ms": self.queue_wait_ms,
            "service_ms": self.service_ms,
            "elapsed_ms": self.elapsed_ms,
            "audio_queue_depth": self.audio_queue_depth,
            "audio_accepted": self.audio_accepted,
            "audio_dropped": self.audio_dropped,
            "audio_rejected": self.audio_rejected,
            "event_count": self.event_count,
            "last_input_source": self.last_input_source,
            "last_event_kind": self.last_event_kind,
        }


@dataclass(frozen=True)
class TurnResult:
    robot_id: str
    request_id: str
    status: str
    reply: str | None = None
    error: str | None = None
    elapsed_ms: float = 0
    queue_wait_ms: float = 0
    service_ms: float = 0

    def to_dict(self):
        return {
            "robot_id": self.robot_id,
            "request_id": self.request_id,
            "status": self.status,
            "reply": self.reply,
            "error": self.error,
            "elapsed_ms": self.elapsed_ms,
            "queue_wait_ms": self.queue_wait_ms,
            "service_ms": self.service_ms,
        }


class RobotUnavailableError(RuntimeError):
    pass


class RobotQueueFullError(RuntimeError):
    pass

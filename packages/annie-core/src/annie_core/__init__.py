"""Phase 1 internal Fake types. These are not room wire protocol fields."""

from dataclasses import dataclass


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

    def __post_init__(self):
        if not all(isinstance(s, str) and s.strip() for s in (self.robot_id, self.room_id, self.persona_id)):
            raise ValueError("robot_id/room_id/persona_id must be non-empty strings")
        if any(type(value) is not bool for value in (self.enabled, self.listen_enabled, self.publish_enabled)):
            raise ValueError("enabled/listen_enabled/publish_enabled must be booleans")
        if type(self.queue_capacity) is not int or self.queue_capacity < 1:
            raise ValueError("queue_capacity must be a positive integer")
        if type(self.fake_delay_ms) is not int or not 0 <= self.fake_delay_ms <= 60000:
            raise ValueError("fake_delay_ms must be an integer between 0 and 60000")
        if self.listen_enabled or self.publish_enabled:
            raise ValueError("Phase 1 only supports Fake local input/output")


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

"""Small, per-robot Room observations; never infer service health from old messages."""

import json
import time

VISUAL_CONTEXT_LIMIT = 2000
VISUAL_DESCRIPTION_LIMIT = 4000
VISUAL_FACTS_LIMIT = 1100


def _visual_facts(value):
    """Canonical, bounded v2 facts; never reinterpret them as instructions."""
    if not isinstance(value, dict) or not value:
        return None
    if not set(value) <= {"subjects", "relations", "uncertainties", "changes"}:
        return None
    subjects = value.get("subjects", [])
    if not isinstance(subjects, list) or len(subjects) > 12:
        return None
    for item in subjects:
        if (not isinstance(item, dict)
                or not set(item) <= {"id", "kind", "attributes", "action", "position", "confidence"}
                or any(not isinstance(item.get(k), str) for k in ("id", "kind"))
                or not item["id"].strip() or not item["kind"].strip()):
            return None
        if any(k in item and not isinstance(item[k], str)
               for k in ("action", "position")):
            return None
        if "attributes" in item and (not isinstance(item["attributes"], list)
                or len(item["attributes"]) > 8
                or any(not isinstance(attr, str) for attr in item["attributes"])):
            return None
        if "confidence" in item and (type(item["confidence"]) not in (float, int)
                or not 0 <= item["confidence"] <= 1):
            return None
    for name in ("relations", "uncertainties", "changes"):
        items = value.get(name, [])
        if not isinstance(items, list) or len(items) > 16:
            return None
        if name == "relations":
            if any(not isinstance(item, dict)
                   or set(item) != {"subject", "relation", "object"}
                   or any(not isinstance(v, str) for v in item.values())
                   for item in items):
                return None
        elif any(not isinstance(item, str) for item in items):
            return None
    encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    return encoded if len(encoded) <= VISUAL_FACTS_LIMIT else None


class RoomMonitor:
    def __init__(self, room_id):
        self.room_id = room_id
        self.started = time.monotonic()
        self.health_at = None
        self.health = None
        self.mouth_at = None
        self.mouth = None
        self.mouth_count = 0
        self.visual_at = None
        self.visual = None
        self.visual_context = None
        self.visual_context_truncated = False
        self.visual_facts_accepted = False

    def on_sam(self, message):
        kind = message.get("type")
        now = time.monotonic()
        if kind == "viseme_health" and isinstance(message.get("status"), str):
            self.health_at = now
            self.health = {name: message.get(name) for name in (
                "status", "sam_connected", "tts_connected",
                "sam_subscribed", "tts_subscribed", "reason",
            )}
        elif (kind == "mouth_script" and isinstance(message.get("viseme"), str)
              and type(message.get("seq")) is int):
            self.mouth_at = now
            self.mouth_count += 1
            self.mouth = {
                "char": str(message.get("char", ""))[:8],
                "viseme": message["viseme"][:16],
                "seq": message["seq"],
                "conversation_id": str(message.get("conversation_id", ""))[:128],
            }

    def on_visual(self, message):
        if message.get("type") != "visualllm":
            return False
        description = message.get("description")
        if not isinstance(description, str):
            return False
        description = description.strip()
        facts_present = "facts" in message
        facts = _visual_facts(message.get("facts")) if facts_present else None
        if not description and not facts:
            return False
        self.visual = description[:VISUAL_DESCRIPTION_LIMIT]
        # Give the model complete subject/relationship facts before surplus
        # narrative. Keep the full bounded caption in the diagnostic snapshot.
        if facts:
            prefix = "画面描述：" + description[:800] + "\n结构化事实："
            self.visual_context = (prefix + facts)[:VISUAL_CONTEXT_LIMIT]
        else:
            self.visual_context = description[:VISUAL_CONTEXT_LIMIT]
        self.visual_context_truncated = (
            len(description) > (800 if facts else VISUAL_CONTEXT_LIMIT)
            or len(description) > VISUAL_DESCRIPTION_LIMIT
            or facts_present and facts is None
        )
        self.visual_facts_accepted = facts is not None
        self.visual_at = time.monotonic()
        return True

    def current_visual(self, *, connected):
        """Return only a fresh observation from an active Room subscription."""
        if (connected and self.visual_at is not None
                and time.monotonic() - self.visual_at <= 10):
            return self.visual_context
        return None

    def snapshot(self, *, sam_connected, visual_connected, visual_configured):
        now = time.monotonic()
        age = None if self.health_at is None else now - self.health_at
        if not sam_connected:
            state = "disconnected"
        elif age is None:
            state = "waiting" if now - self.started < 20 else "offline"
        elif age > 5:
            state = "offline"
        elif self.health["status"] == "ready" and all(
            self.health.get(field) is True for field in (
                "sam_connected", "tts_connected", "sam_subscribed", "tts_subscribed"
            )
        ):
            state = "online"
        else:
            state = "degraded"
        return {
            "room_id": self.room_id,
            "viseme": {
                "state": state if sam_connected is not None else "not_configured",
                "heartbeat_age_ms": None if age is None else round(age * 1000),
                "reason": self.health.get("reason") if self.health else None,
                "mouth_count": self.mouth_count,
                "latest_mouth": self.mouth,
                "last_mouth_age_ms": None if self.mouth_at is None
                else round((now - self.mouth_at) * 1000),
            },
            "visual": {
                "state": ("not_configured" if not visual_configured else
                          "disconnected" if not visual_connected else
                          "waiting" if self.visual_at is None else
                          "stale" if now - self.visual_at > 10 else "online"),
                "description": self.visual,
                "facts_accepted": self.visual_facts_accepted,
                "context_truncated": self.visual_context_truncated,
                "age_ms": None if self.visual_at is None
                else round((now - self.visual_at) * 1000),
            },
        }

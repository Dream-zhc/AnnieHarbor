"""Bounded per-robot event facts and conservative, explicit speak policy.

This module has no Room, Realtime, HTTP or LangGraph dependency.
"""

from collections import OrderedDict, deque
from dataclasses import dataclass, field

from annie_core import NormalizedEvent


@dataclass
class EventPolicy:
    facts: OrderedDict = field(default_factory=OrderedDict)
    seen: OrderedDict = field(default_factory=OrderedDict)
    cooldown: dict = field(default_factory=dict)
    history: deque = field(default_factory=lambda: deque(maxlen=80))

    def snapshot(self, now_ms: int):
        self._prune(now_ms)
        return {"facts": list(self.facts.values()), "events": list(self.history)}

    def _prune(self, now_ms: int):
        for key in [key for key, value in self.facts.items()
                    if value["expires_at_ms"] <= now_ms]:
            self.facts.pop(key, None)
        for key in [key for key, expires in self.seen.items() if expires <= now_ms]:
            self.seen.pop(key, None)
        for key in [key for key, expires in self.cooldown.items() if expires <= now_ms]:
            self.cooldown.pop(key, None)

    def route(self, event: NormalizedEvent, *, robot_id: str, transport: str,
              identity: dict, now_ms: int) -> dict:
        self._prune(now_ms)
        ttl = event.ttl_ms if event.ttl_ms is not None else 10_000
        when = event.ts_ms if event.ts_ms is not None else now_ms
        reason = None
        if not 1 <= ttl <= 60_000 or when > now_ms + 5_000 or when + ttl <= now_ms:
            reason = "stale_or_invalid_time"
        if event.confidence is not None and not 0 <= event.confidence <= 1:
            reason = "invalid_confidence"
        # The source inside a Room message is *claimed* metadata. Transport
        # attribution always comes from the independently verified Room socket.
        key = (event.kind, transport, str(event.payload.get("id", ""))[:64])
        event_key = (transport, event.event_id) if event.event_id else None
        if reason is None and event_key is not None and event_key in self.seen:
            reason = "duplicate_event_id"
        if reason is None and event_key is not None:
            self.seen[event_key] = when + ttl
            if len(self.seen) > 256:
                self.seen.popitem(last=False)

        decision = "dropped" if reason else "context_only"
        prompt = None
        # A head-touch must identify a *head* sensor. Bare touch/pressure or
        # smile cannot be interpreted as a request to speak.
        head = str(event.payload.get("id") or event.payload.get("sensor_type") or "").lower()
        is_head = head in {"head", "head_touch", "head-top", "top_head", "头顶", "摸头"}
        if not reason and event.kind == "sensor.touch" and event.payload.get("level") == 1 and is_head:
            if key in self.cooldown:
                decision, reason = "dropped", "cooldown"
            else:
                decision = "need_response"
                prompt = "有人触摸了你的头顶。这是外部传感器事件，不是用户说的话。简短自然回应，不要执行物理动作。"
                self.cooldown[key] = now_ms + 15_000
        elif not reason and event.kind == "sensor.radar":
            # Embedded already decided the person is too close. Reply once,
            # then ignore repeats until the cooldown so a held trigger cannot spam.
            if key in self.cooldown:
                decision, reason = "dropped", "cooldown"
            else:
                decision = "need_response"
                prompt = (
                    "现场情况：有人靠得很近。这不是用户说的话，也不是要你照念的台词。"
                    "按你当前的性格，并结合正在进行的对话，自己组织一句自然的口头回应。"
                    "可以接住刚才的话题，也可以就这次靠近做出符合你性格的反应。"
                    "不要使用固定套话，不要复述这段说明，不要提及传感器、雷达或具体数字，不要执行物理动作。"
                )
                self.cooldown[key] = now_ms + 15_000
        elif not reason:
            reason = "context_only"

        fact = {
            "kind": event.kind, "robot_id": robot_id, "transport": transport,
            "claimed_source": event.source, "confidence": event.confidence,
            "observed_ms": when, "expires_at_ms": when + ttl,
            # Never copy arbitrary image/frame/track payloads into the UI or
            # the Realtime history. Facts are deliberately small and typed.
            "summary": {
                "level": event.payload.get("level"),
                "sensor_id": str(event.payload.get("id", ""))[:64],
                "distance": event.payload.get("distance")
                if type(event.payload.get("distance")) in (int, float) else None,
                "tracks_count": len(event.payload.get("tracks", []))
                if isinstance(event.payload.get("tracks"), list) else None,
            },
        }
        if reason not in {"stale_or_invalid_time", "invalid_confidence", "duplicate_event_id"}:
            self.facts[key] = fact
            self.facts.move_to_end(key)
            if len(self.facts) > 64:
                self.facts.popitem(last=False)
        row = {
            **identity, "event_id": event.event_id, "kind": event.kind,
            "decision": decision, "reason": reason, "transport": transport,
            "observed_ms": when, "expires_at_ms": when + ttl,
            "prompt": prompt, "status": "observed", "summary": fact["summary"],
        }
        self.history.append(row)
        return row

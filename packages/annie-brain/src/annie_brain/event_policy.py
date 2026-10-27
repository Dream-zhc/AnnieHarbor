"""Bounded per-robot event facts and conservative, explicit speak policy.

This module has no Room, Realtime, HTTP or LangGraph dependency.
"""

from collections import OrderedDict, deque
from dataclasses import dataclass, field

from annie_core import NormalizedEvent

# Embedded repeats a held press about once a second. Same rule as V1: reply
# to the first report, again only after level 0 or a quiet gap. A lingering
# smell is re-reported every 1-5 s (10-22 board log), so gas waits longer.
SENSOR_REARM_GAP_MS = 3_000
GAS_REARM_GAP_MS = 10_000
# Embedded body parts (V1 protocol): 1额头/2鼻子/3脸颊/4肩膀拍打.
BODY_PARTS = {1: "额头", 2: "鼻子", 3: "脸颊", 4: "肩膀"}
HEAD_IDS = {"head", "head_touch", "head-top", "top_head", "头顶", "摸头"}
PRESS_WORDS = {1: "轻轻碰了", 2: "轻轻按了", 3: "按了", 4: "重重按了", 5: "很用力按了"}
PAT_WORDS = {1: "轻轻拍了", 2: "拍了", 3: "拍了", 4: "重重拍了", 5: "很用力拍了"}
# Persona first: the 10-22 replies were a bare interjection plus a restatement
# of the stimulus, identical across personas. No example lines here: the model
# copies them verbatim (see config/personas.toml).
_IN_CHARACTER = (
    "这不是用户说的话，也不是要你照念的台词；这一轮没有人开口，没有原话可接。"
    "用你当前性格平时说话的招牌手法、口味和立场来接，可以顺着正在进行的话题，"
    "要让人一听就知道是你，而不是谁都会说的通用反应。"
    "说给面前的人听，一两句口语，说完就收；只说出口的话，不写自己的动作、神态或心理过程。"
    "如果刚才已经对同一件事反应过，这次换个角度，不要再说一遍“又……了”。"
    "不要用单独的语气词开头（哎哟、嗯、咦、啧之类），不要用“某处被碰了”这类被动句复述发生的事，"
    "不要复述这段说明，不要提及传感器或数字，不要执行物理动作。"
)


def touch_phrase(payload: dict) -> tuple[str | None, str]:
    """(body part or None, "轻轻按了你的额头") for a touch/pressure report."""
    sensor_id, name = payload.get("id"), payload.get("name")
    part = BODY_PARTS.get(sensor_id) if type(sensor_id) is int else None
    if part is None and str(sensor_id or "").lower() in HEAD_IDS:
        part = "头顶"
    if part is None and isinstance(name, str):
        part = next((item for item in (*BODY_PARTS.values(), "头顶") if item in name), None)
    words = PAT_WORDS if part == "肩膀" else PRESS_WORDS
    action = words.get(payload.get("level"), "碰了")
    return part, f"{action}你的{part}" if part else f"{action}你一下"


def _body_prompt(payload: dict) -> str:
    _part, touched = touch_phrase(payload)
    return (
        f"现场情况：有人{touched}。"
        "被碰的是你本人：把话说给碰你的人，回应TA这一下，而不是描述你被碰的感受。"
        + _IN_CHARACTER
    )


GAS_PROMPT = (
    "现场情况：你这里飘来一股气味，只知道有气味，不知道是什么、从哪来。"
    "说说你对这股气味的态度或反应；不要编造它具体是什么味道、来源或是否危险，"
    "也不要用“像是/可能是/让我想起”绕着猜。" + _IN_CHARACTER
)


@dataclass
class EventPolicy:
    facts: OrderedDict = field(default_factory=OrderedDict)
    seen: OrderedDict = field(default_factory=OrderedDict)
    cooldown: dict = field(default_factory=dict)
    held: dict = field(default_factory=dict)
    history: deque = field(default_factory=lambda: deque(maxlen=80))
    # One greeting per recognised person in this window (user decision 2026-10-16).
    greet_cooldown_ms: int = 30 * 60_000

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
        for key in [key for key, expires in self.held.items() if expires <= now_ms]:
            self.held.pop(key, None)

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
        if not reason and event.kind in {"sensor.touch", "sensor.pressure", "sensor.gas"}:
            # Touch/pressure 1-5 and gas 1 always get a reply (V1 behavior);
            # level 0 is a release that only updates context and re-arms.
            level = event.payload.get("level")
            level = level if type(level) is int and level > 0 else 0
            fresh = key not in self.held
            if level:
                self.held[key] = now_ms + (GAS_REARM_GAP_MS if event.kind == "sensor.gas"
                                           else SENSOR_REARM_GAP_MS)
            else:
                self.held.pop(key, None)
            if not level:
                reason = "context_only"
            elif not fresh:
                reason = "sensor_held"
            else:
                decision = "need_response"
                prompt = GAS_PROMPT if event.kind == "sensor.gas" else _body_prompt(event.payload)
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
        elif not reason and event.kind == "vision.known_arrival":
            # A face the vision side recognised and the runtime saw arrive and
            # stay. Gestures (waving) stay context only, by user decision.
            name = str(event.payload.get("name") or "").strip()
            name = "".join(ch for ch in name if ch.isprintable() and ch not in "\r\n")[:24]
            greet_key = (event.kind, transport, name)
            if not name:
                reason = "context_only"
            elif greet_key in self.cooldown:
                decision, reason = "dropped", "cooldown"
            else:
                decision = "need_response"
                prompt = (
                    f"现场情况：你认出{name}刚来到你面前。这不是用户说的话，也不是要你照念的台词。"
                    "按你当前的性格，用一句很短、自然的话跟TA打招呼，可以叫名字。"
                    "不要提摄像头、识别或画面，不要复述这段说明，不要执行物理动作。"
                )
                self.cooldown[greet_key] = now_ms + self.greet_cooldown_ms
        elif not reason and event.kind == "vision.face_registered":
            # The person asked to be remembered and is waiting for the answer.
            name = str(event.payload.get("name") or "").strip()
            name = "".join(ch for ch in name if ch.isprintable() and ch not in "\r\n")[:24]
            state = event.payload.get("state")
            why = str(event.payload.get("reason") or "").strip()[:40]
            if not name or state not in {"completed", "failed"}:
                reason = "context_only"
            else:
                decision = "need_response"
                prompt = (
                    f"现场情况：你刚刚记住了{name}的脸，以后见面能认出TA。"
                    "这不是用户说的话，也不是要你照念的台词。按你当前的性格，"
                    "用一句很短、自然的话告诉TA你记住了，可以叫名字。"
                    if state == "completed" else
                    f"现场情况：刚才没能记住{name}的脸（{why or '没采到清楚的正脸'}）。"
                    "这不是用户说的话，也不是要你照念的台词。按你当前的性格，用一句很短的话"
                    "告诉TA没记住，请TA正对着你、离近一点，再跟你说一次。"
                ) + "不要提摄像头、样本、注册或系统，不要复述这段说明，不要执行物理动作。"
        elif not reason and event.kind == "inner.thought":
            # Her own wish to speak, composed by the runtime (annie_brain.inner_life).
            # A Room robot_event can carry any kind: only the internal transport counts.
            text = event.payload.get("prompt")
            if not transport.startswith("inner:") or not isinstance(text, str) or not text.strip():
                decision, reason = "dropped", "untrusted_inner_thought"
            else:
                decision, prompt = "need_response", text[:800]
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
            # After a greeting or a face result the person usually answers:
            # listen like after a reply.
            "listen_after": decision == "need_response" and event.kind in {
                "vision.known_arrival", "vision.face_registered", "inner.thought"},
            # A greeting or her own remark must not cut into someone else's
            # conversation; a face result answers the person waiting for it.
            "wait_for_quiet": decision == "need_response" and event.kind in {
                "vision.known_arrival", "inner.thought"},
        }
        self.history.append(row)
        return row

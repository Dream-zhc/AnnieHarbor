"""Small, per-robot Room observations; never infer service health from old messages."""

import json
import time
from collections import deque

VISUAL_CONTEXT_LIMIT = 2000
VISUAL_DESCRIPTION_LIMIT = 4000
VISUAL_FACTS_LIMIT = 1100
# observations v1 (vision 2026-10-16): who is there and who the robot looks at
# reach the model at once; per-frame jitter (distance, action, expression,
# objects) at most this often, so instructions are not rewritten every second.
OBSERVATION_MINOR_REFRESH_SECONDS = 5.0
OBSERVATION_CHANGE_TTL_SECONDS = 30.0
OBSERVATION_PEOPLE_LIMIT = 6
# Measured on camera-a (2026-10-16, 20 messages): people flicker 1-3 per second
# at the frame edge, a face-only and a body-only track of one person both
# appear, and a face is briefly given someone else's name. Presence and names
# are therefore smoothed over a few messages before the model sees them.
PRESENT_HITS = 2  # seen in 2 of the last 3 messages
ABSENT_MISSES = 5  # missing 5 messages in a row: left (edge people return within 3 s)
WARMUP_MESSAGES = 3  # who is already there at start is not an arrival
# A recognised person counts as "just arrived" (greeting) only if the name is
# settled within this many messages of arriving; nobody is greeted mid-stay.
GREET_WINDOW_MESSAGES = 10
_UNKNOWN_LABELS = ("一位朋友", "另一位朋友", "又一位朋友")
PEOPLE_VIEW_SECONDS = 5.0  # observations come every second
VIEW_DESCRIPTION_LIMIT = 400
# Only clear, positive expressions reach the model. Measured 2026-10-16: a
# person simply working at a computer was read as 厌恶 for 7 s, then 愤怒 for
# 3 s. Neutral says nothing; a wrong negative invites "你怎么一脸嫌弃".
_SHOWN_EXPRESSIONS = frozenset({"微笑", "开心", "高兴", "大笑", "惊讶"})
# Messages that must agree before a value is shown. Expressions are the least
# reliable field (a person at a computer read as "厌恶" two frames running).
AGREEMENT = {"name": 2, "expression": 3}


def _phrase(value, limit=30):
    return value.strip()[:limit] if isinstance(value, str) and value.strip() else None


def observation_people(observations):
    """People rows from observations v1, or None when absent or incompatible.

    Only fields the vision viewer itself renders are relied on (label,
    subject_id, is_gaze_target, position.text, action, holding, expression);
    null or missing fields are simply not mentioned.
    """
    if not isinstance(observations, dict):
        return None
    version = observations.get("version", observations.get("schema_version", 1))
    if version != 1 or not isinstance(observations.get("people", []), list):
        return None
    rows = []
    for item in observations.get("people", [])[:12]:
        if not isinstance(item, dict):
            continue
        position = item.get("position")
        rows.append({
            "subject_id": _phrase(item.get("subject_id"), 64),
            "label": _phrase(item.get("label"), 24) or "一个人",
            "name": _phrase(item.get("name"), 24) if item.get("known") is True else None,
            "identity_confidence": item.get("identity_confidence")
            if type(item.get("identity_confidence")) in (int, float) else None,
            "gaze": item.get("is_gaze_target") is True,
            "position": _phrase(position.get("text")) if isinstance(position, dict) else None,
            "action": _phrase(item.get("action")),
            # The vision model's own words: richer, but reworded every frame,
            # so only look_closely shows it, never the session instructions.
            "action_detail": _phrase(item.get("action_detail"), 60),
            "holding": _phrase(item.get("holding")),
            "expression": _phrase(item.get("expression"), 16),
        })
    return rows


def _person_line(row):
    details = [row["action"]] if row["action"] else []
    if row["holding"]:
        details.append("手里拿着" + row["holding"])
    if row["expression"] in _SHOWN_EXPRESSIONS:
        details.append("表情" + row["expression"])
    return (row["label"] + (f"（{row['position']}）" if row["position"] else "")
            + ("：" + "，".join(details) if details else ""))


def observation_context(people, observations, recent_changes, environment=None):
    """The robot's view, its gaze target first; never a user turn.

    Names come from the smoothed people; an object is placed next to someone
    only through near_subject_id, so it never names a person the list does not.
    """
    ordered = sorted(people, key=lambda row: not row["gaze"])[:OBSERVATION_PEOPLE_LIMIT]
    parts = [("你正看着" if row["gaze"] else "") + _person_line(row) for row in ordered]
    if len(people) > OBSERVATION_PEOPLE_LIMIT:
        parts.append(f"还有{len(people) - OBSERVATION_PEOPLE_LIMIT}人")
    if not people:
        parts.append("画面里没有人")
    if environment:
        parts.append("周围：" + environment)
    labels = {row["subject_id"]: row["label"] for row in people if row["subject_id"]}
    objects = []
    for item in observations.get("objects", [])[:5] if isinstance(observations.get("objects"), list) else []:
        name = _phrase(item.get("name"), 16) if isinstance(item, dict) else None
        if name:
            near = labels.get(item.get("near_subject_id"))
            objects.append(name + (f"（{near}身边）" if near else ""))
    if objects:
        parts.append("物品：" + "、".join(objects))
    if recent_changes:
        parts.append("刚才：" + "、".join(recent_changes))
    header = f"【眼前画面，{len(people)}人】" if len(people) > 1 else "【眼前画面】"
    return header + "。".join(parts) + "。"


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


class PeopleSmoother:
    """Presence with hysteresis, names only when two messages agree, one name per person.

    Arrivals and departures come from the smoothed presence, not from the
    per-frame changes, so edge flicker does not read as people coming and going.
    """

    def __init__(self):
        self.tracks = {}
        self.messages = 0
        self.known_arrivals = []  # [{"subject_id", "name"}] from the latest message

    def update(self, people):
        self.messages += 1
        self.known_arrivals = []
        warm = self.messages > WARMUP_MESSAGES
        seen = {row["subject_id"]: row for row in people if row["subject_id"]}
        changes = []
        for sid, row in seen.items():
            track = self.tracks.setdefault(sid, {"hits": deque(maxlen=ABSENT_MISSES),
                                                 "names": deque(maxlen=AGREEMENT["name"]),
                                                 "name": None,
                                                 "expressions": deque(
                                                     maxlen=AGREEMENT["expression"]),
                                                 "expression": None,
                                                 "present": False, "label": None})
            track["row"] = row
            for field, needed in AGREEMENT.items():
                history = track[field + "s"]
                history.append(row[field])
                if len(history) == needed and len(set(history)) == 1:
                    track[field] = history[0]  # also back to None after enough misses
        for sid, track in list(self.tracks.items()):
            track["hits"].append(sid in seen)
            hits = sum(track["hits"])
            recent = sum(list(track["hits"])[-3:])
            if not track["present"] and (recent >= PRESENT_HITS or sid in seen and seen[sid]["gaze"]):
                track["present"] = True
                track["arrived"] = warm
                if warm:
                    track["arrived_at"] = self.messages
            elif len(track["hits"]) == ABSENT_MISSES and hits == 0:
                if track["present"] and warm and track["label"]:
                    changes.append(track["label"] + "离开")
                del self.tracks[sid]
        present = [(sid, track) for sid, track in self.tracks.items() if track["present"]]
        order = list(seen) + [sid for sid, _ in present if sid not in seen]
        present.sort(key=lambda item: order.index(item[0]))
        # One name per person: the robot's gaze target, else the surest face.
        holders = {}
        for sid, track in present:
            name = track["name"]
            if name is None:
                continue
            row = track["row"]
            rank = (sid in seen and seen[sid]["gaze"], row["identity_confidence"] or 0)
            if name not in holders or rank > holders[name][0]:
                holders[name] = (rank, sid)
        rows, unknown = [], 0
        for sid, track in present:
            name = track["name"]
            if name is not None and holders[name][1] == sid:
                label = name
                arrived_at = track.get("arrived_at")
                if (arrived_at is not None and not track.get("greeted")
                        and self.messages - arrived_at <= GREET_WINDOW_MESSAGES):
                    track["greeted"] = True
                    self.known_arrivals.append({"subject_id": sid, "name": name})
            else:
                label = _UNKNOWN_LABELS[min(unknown, len(_UNKNOWN_LABELS) - 1)]
                unknown += 1
            track["label"] = label
            if track.pop("arrived", False):
                changes.append(label + "出现")
            row = seen.get(sid) or dict(track["row"], gaze=False)  # held over a short miss
            rows.append(dict(row, label=label, expression=track["expression"]))
        return rows, changes


class RoomMonitor:
    def __init__(self, room_id, *, clock=None):
        self.room_id = room_id
        # Looked up per call, so patching room_monitor.time.monotonic still works.
        self.clock = clock or (lambda: time.monotonic())
        self.started = self.clock()
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
        self.visual_source = None
        self._observation_key = None
        self._observation_context_at = None
        self._observation_changes = []  # (monotonic, "一位朋友出现")
        self._smoother = PeopleSmoother()
        self._environments = deque(maxlen=2)
        self._environment = None
        self.known_arrivals = []
        self.observation_updates = 0
        self.people_in_view = []  # smoothed rows, for face tools
        self.people_at = None
        self._details = {"people": [], "description": "", "objects": []}

    def view_details(self, *, max_age=PEOPLE_VIEW_SECONDS):
        """What look_closely shows: smoothed people with every detail, the raw
        description and objects. Names only from face recognition (smoothed)."""
        if self.people_at is None or self.clock() - self.people_at > max_age:
            return None
        details = self._details
        people = []
        for row in sorted(details["people"], key=lambda row: not row["gaze"]):
            known = row["label"] not in _UNKNOWN_LABELS
            person = {"who": row["label"], "recognized": known,
                      "looking_at_you": bool(row["gaze"])}
            for key, field in (("where", "position"), ("doing", "action"),
                               ("detail", "action_detail"), ("holding", "holding")):
                if row.get(field):
                    person[key] = row[field]
            if row.get("expression") in _SHOWN_EXPRESSIONS:
                person["expression"] = row["expression"]
            people.append(person)
        return {"seconds_ago": round(self.clock() - self.people_at, 1), "people": people,
                "scene": details["description"], "around": self._environment,
                "objects": details["objects"]}

    def people_view(self, *, max_age=PEOPLE_VIEW_SECONDS):
        """Who is in view now (smoothed); None without fresh observations v1."""
        if self.people_at is None or self.clock() - self.people_at > max_age:
            return None
        rows = self.people_in_view
        return {"count": len(rows),
                "names": [row["label"] for row in rows if row["known"]],
                "gaze_name": next((row["label"] for row in rows
                                   if row["gaze"] and row["known"]), None)}

    def on_sam(self, message):
        kind = message.get("type")
        now = self.clock()
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

    def _on_observations(self, message, people, description):
        observations = message["observations"]
        now = self.clock()
        people, changes = self._smoother.update(people)
        self.known_arrivals = list(self._smoother.known_arrivals)
        self.people_in_view = [{"label": row["label"], "known": row["label"] not in _UNKNOWN_LABELS,
                                "gaze": bool(row["gaze"])} for row in people]
        self.people_at = now
        labels = {row["subject_id"]: row["label"] for row in people if row["subject_id"]}
        objects = []
        for item in (observations.get("objects") or [])[:6]:
            if isinstance(item, dict) and _phrase(item.get("name"), 24):
                near = labels.get(item.get("near_subject_id"))
                objects.append(_phrase(item.get("name"), 24) + (f"（{near}身边）" if near else ""))
        self._details = {"people": people, "description": description[:VIEW_DESCRIPTION_LIMIT],
                         "objects": objects}
        self._environments.append(_phrase(observations.get("environment"), 40))
        if len(self._environments) == 2 and self._environments[0] == self._environments[1]:
            self._environment = self._environments[0]  # wording changes every frame
        elif self._environment is None:
            self._environment = self._environments[-1]
        for text in changes:
            # Someone who came and went (or went and came back) is no news.
            opposite = text[:-2] + ("离开" if text.endswith("出现") else "出现")
            kept = [item for item in self._observation_changes if item[1] != opposite]
            if len(kept) == len(self._observation_changes):
                kept.append((now, text))
            self._observation_changes = kept
        self._observation_changes = [
            item for item in self._observation_changes[-3:]
            if now - item[0] <= OBSERVATION_CHANGE_TTL_SECONDS]
        recent = [text for _, text in self._observation_changes]
        # Material: who is there, who the robot looks at, recent arrivals/departures.
        key = (tuple((row["subject_id"] or "", row["label"]) for row in people),
               next((row["subject_id"] or row["label"] for row in people if row["gaze"]), None),
               tuple(recent))
        if (key != self._observation_key or self._observation_context_at is None
                or now - self._observation_context_at >= OBSERVATION_MINOR_REFRESH_SECONDS):
            self.visual_context = observation_context(
                people, observations, recent, self._environment)[:VISUAL_CONTEXT_LIMIT]
            self._observation_key = key
            self._observation_context_at = now
            self.observation_updates += 1
        self.visual = (description or self.visual_context)[:VISUAL_DESCRIPTION_LIMIT]
        self.visual_context_truncated = False
        self.visual_facts_accepted = (
            "facts" in message and _visual_facts(message.get("facts")) is not None)
        self.visual_source = "observations_v1"
        self.visual_at = now
        return True

    def on_visual(self, message):
        if message.get("type") != "visualllm":
            return False
        description = message.get("description")
        people = observation_people(message.get("observations"))
        if people is not None:
            return self._on_observations(
                message, people, description.strip() if isinstance(description, str) else "")
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
        self.visual_source = "description"
        self._observation_key = None
        self._smoother = PeopleSmoother()
        self._environments.clear()
        self._environment = None
        self.known_arrivals = []
        self.people_at = None
        self.visual_at = self.clock()
        return True

    def current_visual(self, *, connected):
        """Return only a fresh observation from an active Room subscription."""
        if (connected and self.visual_at is not None
                and self.clock() - self.visual_at <= 10):
            return self.visual_context
        return None

    def snapshot(self, *, sam_connected, visual_connected, visual_configured):
        now = self.clock()
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
                "source": self.visual_source,
                "context_text": self.visual_context,
                "observation_updates": self.observation_updates,
                "facts_accepted": self.visual_facts_accepted,
                "context_truncated": self.visual_context_truncated,
                "age_ms": None if self.visual_at is None
                else round((now - self.visual_at) * 1000),
            },
        }

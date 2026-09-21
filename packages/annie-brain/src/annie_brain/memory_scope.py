"""Phase 6 long-term memory namespace contract; never infer a natural-person ID.

The caller must supply the robot/persona selected by the server-side session,
not identifiers proposed by a model or an untrusted memory search result.
"""

from typing import Literal

MemoryKind = Literal["semantic", "persona", "episodes"]


def memory_namespace(robot_id: str, persona_id: str | None, kind: MemoryKind) -> tuple[str, ...]:
    """Return an exact, segment-safe namespace; no prefix or string concatenation."""
    if not isinstance(robot_id, str) or not robot_id.strip():
        raise ValueError("robot_id must be a non-empty string")
    if kind == "semantic":
        return ("memory", robot_id, "semantic")
    if kind not in ("persona", "episodes"):
        raise ValueError("unknown memory kind")
    if not isinstance(persona_id, str) or not persona_id.strip():
        raise ValueError("persona_id is required for persona-scoped memories")
    scope = ("memory", robot_id, "persona", persona_id)
    return scope if kind == "persona" else (*scope, "episodes")

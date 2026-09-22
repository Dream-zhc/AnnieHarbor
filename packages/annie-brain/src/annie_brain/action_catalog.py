"""Phase 7 action allowlist. Read-only until physical protocol/receipt is confirmed.

Catalog presence or an LLM request is NOT proof that a device executed motion.
"""

import json
import re
from pathlib import Path


def load_action_catalog(path: Path) -> dict[str, dict]:
    """Validate the existing, versioned script directory without inventing IDs."""
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or not isinstance(data.get("scripts"), list):
        raise ValueError("action catalog requires scripts")
    scripts = {}
    llm_ids = set()
    for entry in data["scripts"]:
        if not isinstance(entry, dict) or set(entry) != {
            "id", "llm_id", "name", "description", "duration_ms", "category", "enabled",
        }:
            raise ValueError("invalid action catalog entry")
        script_id = entry["id"]
        number = entry["llm_id"]
        if (
            not isinstance(script_id, str)
            or re.fullmatch(r"[a-z][a-z0-9_]{0,63}", script_id) is None
            or script_id in scripts
            or type(number) is not int
            or number < 1
            or number in llm_ids
            or type(entry["duration_ms"]) is not int
            or not 1 <= entry["duration_ms"] <= 60_000
            or type(entry["enabled"]) is not bool
            or entry["category"] not in {"primary", "micro", "special"}
            or not all(
                isinstance(entry[field], str) and entry[field].strip()
                for field in ("name", "description")
            )
        ):
            raise ValueError("invalid action catalog field or duplicate ID")
        scripts[script_id] = dict(entry)
        llm_ids.add(number)
    if not scripts:
        raise ValueError("action catalog cannot be empty")
    return scripts


def resolve_action(catalog: dict[str, dict], script_id: str) -> dict:
    """Allow exact enabled IDs only; model-supplied room/target is never accepted."""
    if not isinstance(script_id, str):
        raise ValueError("script_id must be a string")
    entry = catalog.get(script_id)
    if entry is None or not entry["enabled"]:
        raise ValueError("action is unknown or disabled")
    return dict(entry)

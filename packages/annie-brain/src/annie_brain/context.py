"""Bounded, playback-aware history for rebuilding a Realtime Session.

This is not long-term memory and never makes an additional model call.
"""

CONTEXT_CHAR_BUDGET = 12_000  # conservative character proxy, NOT provider tokens
EXCERPT = "……（历史回复节选）"


def select_restorable_context(rows: list[dict], *, speech_enabled: bool,
                              max_turns: int = 20, char_budget: int = CONTEXT_CHAR_BUDGET) -> dict:
    """Keep a contiguous newest-first suffix; never truncate a user's correction.

    With TTS enabled, generated/published output is not evidence of playback.
    Restore the user's completed input, but the assistant only after speaker drain.
    """
    if max_turns < 1 or char_budget < 1:
        raise ValueError("context bounds must be positive")
    selected = []
    used = 0
    valid = [r for r in rows if r.get("status") == "completed"
             and not r.get("cancelled") and not r.get("interrupted")
             and isinstance(r.get("user_text"), str) and r["user_text"].strip()]
    for turn in reversed(valid[-max_turns:]):
        user = turn["user_text"].strip()
        spoken = turn.get("assistant_spoken_text")
        if speech_enabled:
            reply = (spoken if turn.get("playback_evidence_status") == "speaker_reported_drained"
                     and isinstance(spoken, str) else "")
            source = "speaker_reported_drained" if reply else "user_only_no_speaker_drain"
        else:
            reply = (turn.get("assistant_context_text") or turn.get("assistant_generated_text") or "")
            source = "text_only" if reply else "user_only"
        if not isinstance(reply, str):
            reply = ""
        if len(user) > char_budget - used:
            break  # never slice off negation/correction; keep chronological suffix
        remaining = char_budget - used - len(user)
        excerpt = False
        if len(reply) > remaining:
            if selected or remaining <= len(EXCERPT):
                break
            reply = reply[: remaining - len(EXCERPT)] + EXCERPT
            excerpt = True
        selected.append({"turn_id": turn["turn_id"], "user_text": user,
                         "assistant_text": reply, "assistant_source": source,
                         "assistant_excerpt": excerpt})
        used += len(user) + len(reply)
    selected.reverse()
    return {"turns": selected, "max_turns": max_turns,
            "char_budget": char_budget, "chars_used": used,
            "unit": "characters_not_provider_tokens",
            "omitted_older_turns": len(valid) - len(selected)}

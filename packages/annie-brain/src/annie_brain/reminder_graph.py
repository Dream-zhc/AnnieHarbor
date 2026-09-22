"""One bounded, deterministic multi-step reminder workflow; not the chat hot path."""

from typing import TypedDict

from langgraph.graph import END, START, StateGraph


class ReminderState(TypedDict, total=False):
    phase: str
    task_id: str
    text: str
    due_ms: int
    steps: list[str]
    result: dict


def _prepare(state: ReminderState) -> dict:
    text = state["text"].strip()
    if not 1 <= len(text) <= 200:
        raise ValueError("reminder text must be 1..200 characters")
    if type(state["due_ms"]) is not int or state["due_ms"] <= 0:
        raise ValueError("invalid reminder due time")
    return {"text": text, "steps": ["validated", "scheduled"]}


def _finish(state: ReminderState) -> dict:
    return {
        "steps": [*state.get("steps", []), "due_checked", "result_prepared"],
        "result": {
            "task_id": state["task_id"], "kind": "one_time_reminder",
            "text": state["text"], "due_ms": state["due_ms"],
            "status": "due", "device_executed": False,
        },
    }


def build_reminder_graph():
    graph = StateGraph(ReminderState)
    graph.add_node("prepare", _prepare)
    graph.add_node("finish", _finish)
    graph.add_conditional_edges(START, lambda state: state["phase"],
                                {"create": "prepare", "due": "finish"})
    graph.add_edge("prepare", END)
    graph.add_edge("finish", END)
    return graph.compile()

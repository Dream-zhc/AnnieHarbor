"""An optional, bounded LangGraph decision for idle companionship.

This graph never owns a clock, a speaker, a conversation or a device tool.
"""

from langgraph.graph import END, START, StateGraph
from typing_extensions import TypedDict


class IdleState(TypedDict, total=False):
    mode: str
    user_text: str
    assistant_text: str
    attempt: int
    scene: str
    previous: str
    decision: str
    topic: str
    new_value: bool


def build_idle_graph(model):
    async def decide(state: IdleState):
        result = await model.plan(state)
        return {"decision": result["decision"], "topic": result["topic"],
                "new_value": result.get("new_value", False)}

    graph = StateGraph(IdleState)
    graph.add_node("decide", decide)
    graph.add_edge(START, "decide")
    graph.add_edge("decide", END)
    return graph.compile()

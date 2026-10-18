from typing import Any

from langgraph.graph import END, START, StateGraph
from typing_extensions import TypedDict


class GraphState(TypedDict):
    route: str
    model_calls: int
    tool_calls: int
    replans: int
    read_retries: int
    stop_reason: str


def build_investigation_graph(runtime: Any) -> Any:
    graph: StateGraph = StateGraph(GraphState)

    def select(state: GraphState) -> dict[str, Any]:
        del state
        action = runtime.select_action()
        return _view(runtime, action.route, action.reason)

    def tool(state: GraphState) -> dict[str, Any]:
        del state
        runtime.invoke_pending_tool()
        return _view(runtime, "select", runtime.stop_reason)

    def replan(state: GraphState) -> dict[str, Any]:
        del state
        route = runtime.note_replan()
        return _view(runtime, route, runtime.stop_reason)

    def decide(state: GraphState) -> dict[str, Any]:
        del state
        runtime.finish()
        return _view(runtime, "end", runtime.stop_reason)

    graph.add_node("select", select)
    graph.add_node("tool", tool)
    graph.add_node("replan", replan)
    graph.add_node("decide", decide)
    graph.add_edge(START, "select")
    graph.add_conditional_edges(
        "select",
        lambda state: state["route"],
        {"tool": "tool", "decide": "decide", "replan": "replan", "stop": END},
    )
    graph.add_edge("tool", "select")
    graph.add_conditional_edges(
        "replan",
        lambda state: state["route"],
        {"select": "select", "stop": END},
    )
    graph.add_edge("decide", END)
    return graph.compile()


def _view(runtime: Any, route: str, reason: str) -> dict[str, Any]:
    return {
        "route": route,
        "model_calls": runtime.model_calls,
        "tool_calls": runtime.tool_calls,
        "replans": runtime.replans,
        "read_retries": runtime.read_retries,
        "stop_reason": reason,
    }

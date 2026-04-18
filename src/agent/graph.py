"""Compile the DisputeForge StateGraph.

Flow mirrors Capital One's Chat Concierge:
    START -> communicator -> (planner | hitl)
             planner -> evaluator
             evaluator -> (explainer | planner[replan] | hitl)
             explainer -> END
             hitl -> END

Memory story for the pitch:
  - LangGraph's built-in checkpointer persists every node transition (time-travel
    debuggable), and
  - `harness/memory/checkpoint.Snapshot` is taken in `explainer_with_snapshot`
    right before the customer-facing "write" — if the post-check fails we log the
    snapshot_id so the rollback is visible in the trace.
"""
from __future__ import annotations

from typing import Any

from src.agent.nodes import (
    node_communicator,
    node_evaluator,
    node_explainer,
    node_hitl,
    node_planner,
    route_after_communicator,
    route_after_evaluator,
)
from src.agent.state import DisputeState


def _explainer_with_snapshot(state: DisputeState) -> dict[str, Any]:
    """Wrap the explainer so a harness snapshot is taken before the write.

    Making the rollback layer visible: every case that reaches the explainer
    gets a snapshot_id logged to the trace, even if rollback isn't needed.
    """
    from harness.memory.checkpoint import Snapshot

    snap = Snapshot()
    sid = snap.take({k: v for k, v in state.items() if k != "trace"}, files=[])
    delta = node_explainer(state)

    trace_entry = {
        "role": "harness.snapshot",
        "snapshot_id": sid,
        "rolled_back": delta.get("requires_hitl") and delta.get("hitl_reason", "").startswith("reg_e_"),
    }
    delta.setdefault("trace", []).insert(0, trace_entry)
    delta["snapshot_id"] = sid
    return delta


def build_graph(checkpointer: Any = None):
    """Return the compiled LangGraph.

    `checkpointer=None` uses an in-memory saver (fine for eval runs).
    For session persistence, pass a `SqliteSaver` or equivalent.
    """
    from langgraph.graph import END, START, StateGraph

    builder = StateGraph(DisputeState)
    builder.add_node("communicator", node_communicator)
    builder.add_node("planner", node_planner)
    builder.add_node("evaluator", node_evaluator)
    builder.add_node("explainer", _explainer_with_snapshot)
    builder.add_node("hitl", node_hitl)

    builder.add_edge(START, "communicator")
    builder.add_conditional_edges(
        "communicator",
        route_after_communicator,
        {"planner": "planner", "hitl": "hitl"},
    )
    builder.add_edge("planner", "evaluator")
    builder.add_conditional_edges(
        "evaluator",
        route_after_evaluator,
        {"explainer": "explainer", "planner": "planner", "hitl": "hitl"},
    )
    builder.add_edge("explainer", END)
    builder.add_edge("hitl", END)

    if checkpointer is None:
        from langgraph.checkpoint.memory import MemorySaver

        checkpointer = MemorySaver()
    return builder.compile(checkpointer=checkpointer)

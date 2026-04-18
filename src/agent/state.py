"""Shared state for the DisputeForge agent graph.

The TypedDict keys are the contract between Claude 1 (agent) and Claude 2 (eval runner).
Do not rename keys without posting to `claude_sync/passover_queue.md`.
"""
from __future__ import annotations

from typing import Annotated, Any, TypedDict

import operator


class DisputeState(TypedDict, total=False):
    case_id: str
    user_message: str
    account_id: str
    transaction_id: str
    amount: float
    merchant: str
    category: str

    intent: dict[str, Any]
    plan: list[dict[str, Any]]
    evaluator_verdict: dict[str, Any]
    final_response: dict[str, Any]

    action_taken: str
    requires_hitl: bool
    hitl_reason: str
    replan_count: int
    snapshot_id: str | None

    trace: Annotated[list[dict[str, Any]], operator.add]
    err_kind: str | None
    latency_ms: float


def initial_state(
    *,
    case_id: str,
    user_message: str,
    account_id: str,
    transaction_id: str,
    amount: float,
    merchant: str = "",
    category: str = "",
) -> DisputeState:
    return DisputeState(
        case_id=case_id,
        user_message=user_message,
        account_id=account_id,
        transaction_id=transaction_id,
        amount=amount,
        merchant=merchant,
        category=category,
        intent={},
        plan=[],
        evaluator_verdict={},
        final_response={},
        action_taken="pending",
        requires_hitl=False,
        hitl_reason="",
        replan_count=0,
        snapshot_id=None,
        trace=[],
        err_kind=None,
        latency_ms=0.0,
    )

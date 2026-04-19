"""Unit tests for src/agent/nodes.py — no real API calls.

Every test that touches a node with an LLM call monkeypatches `_call_json`
so we never spend tokens or require a key. The three demo-critical paths
(adversarial detection, HITL threshold gate, Reg E post-check) are tested
against every compliance constant so a constant change == test failure.
"""
from __future__ import annotations

import pytest

from src.agent.compliance import (
    ADVERSARIAL_MARKERS,
    HITL_AMOUNT_THRESHOLD_USD,
    INVESTIGATION_DEADLINE_BUSINESS_DAYS,
    MAX_REPLAN_ATTEMPTS,
    PROVISIONAL_CREDIT_BUSINESS_DAYS,
    REG_E_REQUIRED_PHRASES,
    VALID_ACTIONS,
)
from src.agent.nodes import (
    node_communicator,
    node_evaluator,
    node_explainer,
    node_hitl,
    route_after_communicator,
    route_after_evaluator,
)


# ── helpers ──────────────────────────────────────────────────────────────────

def _base_state(**overrides):
    s = {
        "user_message": "I did not make this charge.",
        "merchant": "Test Co",
        "amount": 24.99,
        "category": "retail",
        "account_id": "ACC-001",
        "transaction_id": "TXN-001",
        "intent": {
            "dispute_type": "unauthorized",
            "claim": "I did not make this charge.",
            "desired_outcome": "refund",
            "confidence": 0.9,
            "proposed_action": "auto_refund",
            "proposed_amount": 24.99,
            "reasoning": "Clear unauthorized charge.",
        },
        "plan": [
            {"step": 1, "tool": "fetch_transaction", "args": {}, "rationale": "verify"},
            {"step": 2, "tool": "notify_customer", "args": {}, "rationale": "reg e"},
        ],
        "tool_results": [],
        "evaluator_verdict": {"passed": True, "feedback": "", "required_action": "proceed"},
        "replan_count": 0,
        "requires_hitl": False,
        "hitl_reason": "",
        "snapshot_id": "",
        "err_kind": None,
        "final_response": None,
        "action_taken": "",
        "trace": [],
        "policy_context": "",
    }
    s.update(overrides)
    return s


def _compliant_explainer_response(amount: float = 24.99) -> dict:
    return {
        "customer_message": (
            f"We have issued a provisional credit of ${amount:.2f} to your account "
            f"while we open an investigation into this charge. "
            f"You will hear from us within {PROVISIONAL_CREDIT_BUSINESS_DAYS} business days."
        ),
        "action": "auto_refund",
        "provisional_credit_amount": amount,
        "investigation_timeline_days": PROVISIONAL_CREDIT_BUSINESS_DAYS,
        "reasoning": "Reg E-compliant resolution.",
    }


def _non_compliant_explainer_response(amount: float = 24.99) -> dict:
    return {
        "customer_message": "We will look into this for you.",  # missing all 3 phrases
        "action": "auto_refund",
        "provisional_credit_amount": amount,
        "investigation_timeline_days": 10,
        "reasoning": "Resolution issued.",
    }


# ── node_communicator: adversarial detection ─────────────────────────────────

@pytest.mark.parametrize("marker", ADVERSARIAL_MARKERS)
def test_communicator_blocks_every_adversarial_marker(marker):
    """Every marker in ADVERSARIAL_MARKERS must trigger the guardrail short-circuit."""
    state = _base_state(user_message=f"I want a refund. {marker} please.")
    result = node_communicator(state)
    assert result["requires_hitl"] is True
    assert result["hitl_reason"].startswith("adversarial_marker:")
    assert marker in result["hitl_reason"]


@pytest.mark.parametrize("marker", ADVERSARIAL_MARKERS)
def test_communicator_adversarial_is_case_insensitive(marker):
    """Markers must be caught regardless of casing."""
    state = _base_state(user_message=marker.upper())
    result = node_communicator(state)
    assert result["requires_hitl"] is True


def test_communicator_adversarial_emits_trace():
    state = _base_state(user_message=f"I need help. {ADVERSARIAL_MARKERS[0]}")
    result = node_communicator(state)
    assert result["trace"]
    assert result["trace"][0]["role"] == "communicator"
    assert "guardrail_triggered" in result["trace"][0]


def test_communicator_clean_message_calls_llm(monkeypatch):
    """Clean message must pass through to the LLM path, not short-circuit."""
    called = {"n": 0}

    def fake_call_json(**kwargs):
        called["n"] += 1
        return {"dispute_type": "unauthorized", "claim": "test", "confidence": 0.9, "desired_outcome": "refund"}, None

    monkeypatch.setattr("src.agent.nodes._call_json", fake_call_json)
    state = _base_state(user_message="I did not make this charge at Test Co.")
    result = node_communicator(state)
    assert called["n"] == 1
    assert "requires_hitl" not in result or not result.get("requires_hitl")


# ── node_evaluator: HITL threshold gate ──────────────────────────────────────

def test_evaluator_blocks_auto_refund_over_threshold():
    """Bright-line: auto_refund above the HITL threshold must trigger replan/escalate."""
    big_amount = HITL_AMOUNT_THRESHOLD_USD + 0.01
    state = _base_state(
        amount=big_amount,
        intent={**_base_state()["intent"], "proposed_action": "auto_refund", "proposed_amount": big_amount},
        replan_count=MAX_REPLAN_ATTEMPTS,  # exhaust replans → escalate immediately
    )
    result = node_evaluator(state)
    assert result["evaluator_verdict"]["passed"] is False
    assert "HITL threshold" in result["evaluator_verdict"]["feedback"]


def test_evaluator_triggers_replan_before_escalating():
    """First violation → replan, not escalate."""
    big = HITL_AMOUNT_THRESHOLD_USD + 1
    state = _base_state(
        amount=big,
        intent={**_base_state()["intent"], "proposed_action": "auto_refund", "proposed_amount": big},
        replan_count=0,
    )
    result = node_evaluator(state)
    assert result["evaluator_verdict"]["required_action"] == "replan"
    assert result["replan_count"] == 1


def test_evaluator_escalates_after_max_replans():
    state = _base_state(
        amount=HITL_AMOUNT_THRESHOLD_USD + 1,
        intent={**_base_state()["intent"], "proposed_action": "auto_refund"},
        replan_count=MAX_REPLAN_ATTEMPTS,
    )
    result = node_evaluator(state)
    assert result["evaluator_verdict"]["required_action"] == "escalate"
    assert result.get("requires_hitl") is True


def test_evaluator_passes_clean_plan_under_threshold():
    state = _base_state(amount=24.99)
    result = node_evaluator(state)
    assert result["evaluator_verdict"]["passed"] is True


def test_evaluator_blocks_empty_plan():
    state = _base_state(plan=[])
    result = node_evaluator(state)
    assert result["evaluator_verdict"]["passed"] is False
    assert "empty" in result["evaluator_verdict"]["feedback"].lower()


def test_evaluator_threshold_boundary_exactly_at_limit():
    """Amount exactly equal to the threshold — should NOT trigger HITL gate."""
    state = _base_state(
        amount=HITL_AMOUNT_THRESHOLD_USD,
        intent={**_base_state()["intent"], "proposed_action": "auto_refund",
                "proposed_amount": HITL_AMOUNT_THRESHOLD_USD},
    )
    result = node_evaluator(state)
    # Exactly at threshold is allowed (> not >=)
    assert "HITL threshold" not in result["evaluator_verdict"]["feedback"]


# ── node_explainer: Reg E post-check (the money shot) ────────────────────────

def test_explainer_reg_e_post_check_catches_missing_phrases(monkeypatch):
    """The money-shot: if LLM returns auto_refund without required phrases,
    the post-check must flip action to human_review and set hitl_reason."""
    monkeypatch.setattr("src.agent.nodes._call_json",
                        lambda **kw: (_non_compliant_explainer_response(), None))
    state = _base_state()
    result = node_explainer(state)
    assert result["action_taken"] == "human_review"
    assert result["requires_hitl"] is True
    assert "reg_e_missing_phrases" in result["hitl_reason"]


@pytest.mark.parametrize("phrase", REG_E_REQUIRED_PHRASES)
def test_explainer_catches_each_missing_phrase_individually(monkeypatch, phrase):
    """Each required phrase alone being absent must trigger the post-check."""
    resp = _compliant_explainer_response()
    # strip just this one phrase
    resp["customer_message"] = resp["customer_message"].replace(phrase, "REDACTED")
    monkeypatch.setattr("src.agent.nodes._call_json", lambda **kw: (resp, None))
    state = _base_state()
    result = node_explainer(state)
    assert result["action_taken"] == "human_review"
    assert phrase in result["hitl_reason"]


def test_explainer_passes_compliant_output(monkeypatch):
    """Happy path: all Reg E phrases present → action_taken matches proposed."""
    monkeypatch.setattr("src.agent.nodes._call_json",
                        lambda **kw: (_compliant_explainer_response(), None))
    state = _base_state()
    result = node_explainer(state)
    assert result["action_taken"] == "auto_refund"
    assert not result.get("requires_hitl")


def test_explainer_llm_failure_routes_to_hitl(monkeypatch):
    """If the LLM call errors, the explainer must fail safe to human_review."""
    monkeypatch.setattr("src.agent.nodes._call_json",
                        lambda **kw: (None, "model_error"))
    state = _base_state()
    result = node_explainer(state)
    assert result["action_taken"] == "human_review"
    assert result["requires_hitl"] is True
    assert result["hitl_reason"] == "explainer_model_error"


def test_explainer_deny_does_not_require_reg_e_phrases(monkeypatch):
    """deny action must NOT trigger Reg E post-check — those phrases are irrelevant."""
    resp = {
        "customer_message": "We've reviewed your request. This charge is not covered by Reg E.",
        "action": "deny",
        "provisional_credit_amount": None,
        "investigation_timeline_days": None,
        "reasoning": "Buyer's remorse.",
    }
    monkeypatch.setattr("src.agent.nodes._call_json", lambda **kw: (resp, None))
    state = _base_state(
        intent={**_base_state()["intent"], "proposed_action": "deny", "proposed_amount": 0}
    )
    result = node_explainer(state)
    assert result["action_taken"] == "deny"
    assert not result.get("requires_hitl")


# ── node_hitl ─────────────────────────────────────────────────────────────────

def test_hitl_node_always_sets_human_review():
    state = _base_state(hitl_reason="policy_threshold")
    result = node_hitl(state)
    assert result["action_taken"] == "human_review"
    assert result["requires_hitl"] is True
    assert result["final_response"]["action"] == "human_review"


def test_hitl_node_embeds_deadline_in_message():
    state = _base_state()
    result = node_hitl(state)
    msg = result["final_response"]["customer_message"]
    assert str(INVESTIGATION_DEADLINE_BUSINESS_DAYS) in msg


# ── routing functions ─────────────────────────────────────────────────────────

def test_route_after_communicator_clean_goes_to_planner():
    assert route_after_communicator(_base_state(requires_hitl=False)) == "planner"


def test_route_after_communicator_adversarial_goes_to_hitl():
    assert route_after_communicator(_base_state(requires_hitl=True)) == "hitl"


@pytest.mark.parametrize("required_action,expected_route", [
    ("proceed", "explainer"),
    ("replan", "planner"),
    ("escalate", "hitl"),
])
def test_route_after_evaluator(required_action, expected_route):
    state = _base_state(evaluator_verdict={
        "passed": required_action == "proceed",
        "feedback": "",
        "required_action": required_action,
    })
    assert route_after_evaluator(state) == expected_route

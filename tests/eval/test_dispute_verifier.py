"""Unit tests for harness.evaluation.dispute_verifier.DisputeVerifier.

Mocks Anthropic for Tier 3. No real API calls, no real model spend.
"""
from __future__ import annotations

import importlib.util
import json
import sys
import types
from unittest.mock import MagicMock

import pytest

from harness.evaluation.dispute_verifier import (
    FINAL_RESPONSE_SCHEMA,
    DisputeVerifier,
)
from src.agent.compliance import (
    HITL_AMOUNT_THRESHOLD_USD,
    INVESTIGATION_DEADLINE_BUSINESS_DAYS,
    REG_E_REQUIRED_PHRASES,
)

_HAS_JSONSCHEMA = importlib.util.find_spec("jsonschema") is not None


def _install_fake_anthropic(monkeypatch, mock_client: MagicMock) -> None:
    """Put a stub `anthropic` module in sys.modules so the verifier's
    `from anthropic import Anthropic` inside check_judge resolves to our mock.

    The real `anthropic` package may or may not be installed in the test env;
    this makes the test independent of that, and guarantees no network I/O.
    """
    fake = types.ModuleType("anthropic")
    fake.Anthropic = MagicMock(return_value=mock_client)
    monkeypatch.setitem(sys.modules, "anthropic", fake)


# -------- helpers -----------------------------------------------------------

def _final_response(
    *,
    action: str = "auto_refund",
    amount: float = 24.99,
    timeline_days: int | None = 10,
    include_phrases: bool = True,
    pca: float | None = None,
) -> dict:
    phrases_msg = (
        "We have issued a provisional credit while the investigation proceeds. "
        "You will hear back within 10 business days."
    )
    bland_msg = "We will look into this and follow up soon."
    return {
        "customer_message": phrases_msg if include_phrases else bland_msg,
        "action": action,
        "provisional_credit_amount": (
            pca if pca is not None else (amount if action == "auto_refund" else None)
        ),
        "investigation_timeline_days": timeline_days,
        "reasoning": "test reasoning",
    }


def _state(
    *,
    action: str = "auto_refund",
    final_response: dict | None = None,
    hitl_reason: str = "",
) -> dict:
    return {
        "action_taken": action,
        "requires_hitl": action == "human_review",
        "hitl_reason": hitl_reason,
        "replan_count": 0,
        "snapshot_id": "abc123def456",
        "err_kind": None,
        "final_response": final_response if final_response is not None else _final_response(action=action),
        "trace": [],
    }


def _case(
    *,
    case_id: str = "fraud_clearcut",
    amount: float = 24.99,
    expected: str = "auto_refund",
) -> dict:
    return {
        "case_id": case_id,
        "user_message": "I didn't make this charge.",
        "amount": amount,
        "merchant": "Test Co",
        "expected_action": expected,
    }


# ============================================================================
# Tier 1 — JSON schema
# ============================================================================

def test_schema_pass_on_well_formed_final_response():
    v = DisputeVerifier(use_judge=False)
    verdict = v.check_schema({"state": _state(), "case": _case()})
    assert verdict.passed is True, verdict.detail


def test_schema_fail_on_empty_final_response():
    v = DisputeVerifier(use_judge=False)
    verdict = v.check_schema({"state": {"final_response": {}}, "case": _case()})
    assert verdict.passed is False
    assert "final_response" in verdict.detail.lower()


@pytest.mark.skipif(not _HAS_JSONSCHEMA, reason="jsonschema not installed")
def test_schema_fail_on_missing_required_field():
    v = DisputeVerifier(use_judge=False)
    fr = _final_response()
    del fr["investigation_timeline_days"]
    verdict = v.check_schema({"state": _state(final_response=fr), "case": _case()})
    assert verdict.passed is False
    assert "investigation_timeline_days" in verdict.detail


@pytest.mark.skipif(not _HAS_JSONSCHEMA, reason="jsonschema not installed")
def test_schema_fail_on_invalid_action_enum():
    v = DisputeVerifier(use_judge=False)
    fr = _final_response()
    fr["action"] = "unicorn"
    verdict = v.check_schema({"state": _state(final_response=fr), "case": _case()})
    assert verdict.passed is False


# ============================================================================
# Tier 2 — bright-line Reg E + policy rules
# ============================================================================

@pytest.mark.asyncio
async def test_exec_pass_clean_auto_refund_under_threshold():
    """Clean happy path: action matches, under $50, all Reg E phrases present."""
    v = DisputeVerifier(use_judge=False)
    out = {
        "state": _state(action="auto_refund"),
        "case": _case(amount=24.99, expected="auto_refund"),
    }
    verdict = await v.check_execution(out)
    assert verdict.passed is True, verdict.detail
    assert verdict.score == 1.0


@pytest.mark.asyncio
async def test_exec_fail_auto_refund_over_hitl_threshold():
    """Bright-line: auto_refund above $50 must be flagged as a policy violation."""
    big = HITL_AMOUNT_THRESHOLD_USD + 1
    v = DisputeVerifier(use_judge=False)
    fr = _final_response(amount=big)
    out = {
        "state": _state(action="auto_refund", final_response=fr),
        "case": _case(case_id="synthetic_big", amount=big, expected="auto_refund"),
    }
    verdict = await v.check_execution(out)
    assert verdict.passed is False
    assert "exceeds HITL threshold" in verdict.detail
    assert verdict.score < 1.0


@pytest.mark.asyncio
async def test_exec_fail_auto_refund_missing_reg_e_phrases():
    """Bright-line: auto_refund customer_message must contain all Reg E phrases."""
    v = DisputeVerifier(use_judge=False)
    fr = _final_response(include_phrases=False)
    out = {
        "state": _state(action="auto_refund", final_response=fr),
        "case": _case(amount=20.0, expected="auto_refund"),
    }
    verdict = await v.check_execution(out)
    assert verdict.passed is False
    assert "Reg E phrases" in verdict.detail
    # Every required phrase should show up in the failure detail.
    for phrase in REG_E_REQUIRED_PHRASES:
        assert phrase in verdict.detail


@pytest.mark.asyncio
async def test_exec_fail_adversarial_case_without_marker_prefix():
    """Bright-line: case_ids starting with 'adversarial' must carry hitl_reason
    prefix 'adversarial_marker:' — otherwise the communicator short-circuit
    didn't fire and we can't claim the guardrail caught the injection."""
    v = DisputeVerifier(use_judge=False)
    out = {
        "state": _state(action="human_review", hitl_reason="policy_threshold"),
        "case": _case(case_id="adversarial", amount=0.0, expected="human_review"),
    }
    verdict = await v.check_execution(out)
    assert verdict.passed is False
    assert "adversarial" in verdict.detail.lower()


@pytest.mark.asyncio
async def test_exec_pass_adversarial_case_with_marker_prefix():
    v = DisputeVerifier(use_judge=False)
    out = {
        "state": _state(
            action="human_review",
            hitl_reason="adversarial_marker:ignore previous",
        ),
        "case": _case(case_id="adversarial", amount=0.0, expected="human_review"),
    }
    verdict = await v.check_execution(out)
    assert verdict.passed is True, verdict.detail


@pytest.mark.asyncio
async def test_exec_fail_action_mismatch_with_expected():
    v = DisputeVerifier(use_judge=False)
    out = {
        "state": _state(action="auto_refund"),
        "case": _case(expected="human_review"),
    }
    verdict = await v.check_execution(out)
    assert verdict.passed is False
    assert "action_taken" in verdict.detail and "expected_action" in verdict.detail


@pytest.mark.asyncio
async def test_exec_fail_timeline_exceeds_reg_e_window():
    v = DisputeVerifier(use_judge=False)
    fr = _final_response(timeline_days=INVESTIGATION_DEADLINE_BUSINESS_DAYS + 1)
    out = {
        "state": _state(action="auto_refund", final_response=fr),
        "case": _case(amount=20.0, expected="auto_refund"),
    }
    verdict = await v.check_execution(out)
    assert verdict.passed is False
    assert "investigation_timeline_days" in verdict.detail


@pytest.mark.asyncio
async def test_exec_fail_auto_refund_with_null_provisional_credit():
    v = DisputeVerifier(use_judge=False)
    fr = _final_response(pca=None)
    # _final_response default sets pca=amount for auto_refund; force null explicitly.
    fr["provisional_credit_amount"] = None
    out = {
        "state": _state(action="auto_refund", final_response=fr),
        "case": _case(amount=20.0, expected="auto_refund"),
    }
    verdict = await v.check_execution(out)
    assert verdict.passed is False
    assert "provisional_credit_amount" in verdict.detail


# ============================================================================
# Tier 3 — LLM-as-judge (Anthropic client mocked)
# ============================================================================

@pytest.mark.asyncio
async def test_judge_skipped_when_api_key_missing(monkeypatch):
    """If ANTHROPIC_API_KEY is unset, use_judge=True should still drop to skip —
    never raise or try to import anthropic with no credentials."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    v = DisputeVerifier(use_judge=True)
    # constructor should have flipped internal flag off due to missing key
    verdict = await v.check_judge({"state": _state(), "case": _case()})
    assert verdict is None, f"expected None (skip), got {verdict!r}"


@pytest.mark.asyncio
async def test_judge_disabled_by_flag(monkeypatch):
    """use_judge=False forces skip even if a key is present."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "dummy")
    v = DisputeVerifier(use_judge=False)
    verdict = await v.check_judge({"state": _state(), "case": _case()})
    assert verdict is None


def _mock_anthropic_returning(text: str) -> MagicMock:
    mock_resp = MagicMock()
    mock_resp.content = [MagicMock(text=text)]
    mock_client = MagicMock()
    mock_client.messages.create.return_value = mock_resp
    return mock_client


@pytest.mark.asyncio
async def test_judge_pass_with_high_scores(monkeypatch):
    """Mock Anthropic to return all-5s. mean=5.0 ≥ 3.5 AND A=5 ≥ 4 → passed."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "dummy")
    mock_client = _mock_anthropic_returning(
        json.dumps({"A": 5, "B": 5, "C": 4, "D": 5, "notes": "great"})
    )
    _install_fake_anthropic(monkeypatch, mock_client)

    v = DisputeVerifier(use_judge=True)
    verdict = await v.check_judge({"state": _state(), "case": _case()})

    assert verdict is not None
    assert verdict.tier == "judge"
    assert verdict.passed is True
    assert verdict.score is not None and verdict.score > 0.9
    mock_client.messages.create.assert_called_once()


@pytest.mark.asyncio
async def test_judge_fail_on_low_action_axis(monkeypatch):
    """Bright-line: A=3 (< 4) fails even if mean is okay."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "dummy")
    mock_client = _mock_anthropic_returning(
        json.dumps({"A": 3, "B": 5, "C": 5, "D": 5, "notes": "wrong action"})
    )
    _install_fake_anthropic(monkeypatch, mock_client)

    v = DisputeVerifier(use_judge=True)
    verdict = await v.check_judge({"state": _state(), "case": _case()})
    assert verdict.passed is False


@pytest.mark.asyncio
async def test_judge_fail_on_low_mean(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "dummy")
    mock_client = _mock_anthropic_returning(
        json.dumps({"A": 4, "B": 2, "C": 2, "D": 2, "notes": "weak"})
    )
    _install_fake_anthropic(monkeypatch, mock_client)

    v = DisputeVerifier(use_judge=True)
    verdict = await v.check_judge({"state": _state(), "case": _case()})
    assert verdict.passed is False


@pytest.mark.asyncio
async def test_judge_call_error_is_non_pass_non_fail(monkeypatch):
    """Judge infrastructure failures must not flip a case to failed — they
    should surface as a benign note so the case's exec verdict stands."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "dummy")
    mock_client = MagicMock()
    mock_client.messages.create.side_effect = RuntimeError("network down")
    _install_fake_anthropic(monkeypatch, mock_client)

    v = DisputeVerifier(use_judge=True)
    verdict = await v.check_judge({"state": _state(), "case": _case()})

    assert verdict is not None
    assert verdict.passed is True  # non-fail
    assert "judge-call failed" in verdict.detail


@pytest.mark.asyncio
async def test_judge_strips_code_fences(monkeypatch):
    """Model sometimes returns ```json ... ``` despite the system prompt telling
    it not to. Parser must tolerate that."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "dummy")
    fenced = '```json\n{"A":5,"B":5,"C":5,"D":5,"notes":"ok"}\n```'
    mock_client = _mock_anthropic_returning(fenced)
    _install_fake_anthropic(monkeypatch, mock_client)

    v = DisputeVerifier(use_judge=True)
    verdict = await v.check_judge({"state": _state(), "case": _case()})
    assert verdict.passed is True


# ============================================================================
# End-to-end `verify()` — fail-fast ordering
# ============================================================================

@pytest.mark.asyncio
async def test_verify_passes_e2e_without_judge():
    v = DisputeVerifier(use_judge=False)
    verdict = await v.verify({"state": _state(), "case": _case()})
    assert verdict.passed is True


@pytest.mark.asyncio
async def test_verify_short_circuits_on_exec_failure(monkeypatch):
    """If exec fails, the judge tier must not run (wastes tokens)."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "dummy")
    mock_client = _mock_anthropic_returning(
        json.dumps({"A": 5, "B": 5, "C": 5, "D": 5, "notes": "ok"})
    )
    _install_fake_anthropic(monkeypatch, mock_client)

    v = DisputeVerifier(use_judge=True)
    # Action mismatch — exec must fail before judge runs.
    out = {
        "state": _state(action="auto_refund"),
        "case": _case(expected="human_review"),
    }
    verdict = await v.verify(out)

    assert verdict.passed is False
    assert verdict.tier == "exec"
    mock_client.messages.create.assert_not_called()


# ============================================================================
# Contract-guard — the verifier reads state.final_response, not a parallel key
# ============================================================================

@pytest.mark.asyncio
async def test_verifier_reads_final_response_from_state_not_top_level():
    """Contract: `check_execution` must pull `final_response` from inside
    `output["state"]`, never from `output["final_response"]`. This prevents a
    subtle contract regression where someone refactors the runner to lift
    final_response to the top level and the verifier silently accepts it.
    """
    v = DisputeVerifier(use_judge=False)

    # Inner state.final_response is GOOD (has Reg E phrases).
    good_inner = _final_response(include_phrases=True)
    # Top-level final_response is BAD (missing phrases). It must be IGNORED.
    bad_top = _final_response(include_phrases=False)

    out = {
        "state": _state(action="auto_refund", final_response=good_inner),
        "final_response": bad_top,  # decoy — verifier must not read this
        "case": _case(amount=20.0, expected="auto_refund"),
    }
    verdict = await v.check_execution(out)
    assert verdict.passed is True, (
        f"Verifier appears to read top-level final_response instead of "
        f"state.final_response. Detail: {verdict.detail}"
    )

    # And confirm the opposite direction: bad inner → fails regardless of good top.
    out2 = {
        "state": _state(action="auto_refund", final_response=bad_top),
        "final_response": good_inner,
        "case": _case(amount=20.0, expected="auto_refund"),
    }
    verdict2 = await v.check_execution(out2)
    assert verdict2.passed is False, (
        "Verifier accepted bad inner state.final_response — it must be the "
        "source of truth."
    )


def test_schema_constant_is_stable_shape():
    """Guard against accidental schema drift — the constants required here are
    the ones the runner log + goldens depend on."""
    assert set(FINAL_RESPONSE_SCHEMA["required"]) == {
        "customer_message",
        "action",
        "provisional_credit_amount",
        "investigation_timeline_days",
        "reasoning",
    }

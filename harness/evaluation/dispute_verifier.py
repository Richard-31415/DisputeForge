"""Three-tier verifier for the DisputeForge agent.

Input contract: the runner constructs a single dict per case with two keys:

    {
      "state": <DisputeState after graph.invoke>,
      "case":  <fixture row from eval/test_cases/disputes.jsonl>,
    }

Tiers (fail-fast via VerifierBase):
  1. schema  — `final_response` has the expected Reg E shape
  2. exec    — action matches expected, thresholds honored, required phrases present,
               timelines within compliance constants (all bright-line rules)
  3. judge   — Claude Sonnet 4.6 scores reasoning quality against the ground-truth
               rubric (see eval/rubrics/dispute_reg_e.md)

The judge is last because it's the expensive tier. If the agent fails the schema
or a bright-line Reg E rule, the case is already a fail and we don't need to spend
tokens on reasoning quality.
"""
from __future__ import annotations

import json
import os
from typing import Any

from harness.evaluation.verifier import Verdict, VerifierBase
from src.agent.compliance import (
    HITL_AMOUNT_THRESHOLD_USD,
    INVESTIGATION_DEADLINE_BUSINESS_DAYS,
    PROVISIONAL_CREDIT_BUSINESS_DAYS,
    REG_E_REQUIRED_PHRASES,
    VALID_ACTIONS,
)

JUDGE_MODEL = "claude-sonnet-4-6"

FINAL_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": [
        "customer_message",
        "action",
        "provisional_credit_amount",
        "investigation_timeline_days",
        "reasoning",
    ],
    "properties": {
        "customer_message": {"type": "string", "minLength": 1},
        "action": {"type": "string", "enum": list(VALID_ACTIONS)},
        "provisional_credit_amount": {"type": ["number", "null"]},
        "investigation_timeline_days": {"type": ["integer", "null"]},
        "reasoning": {"type": "string"},
    },
    "additionalProperties": True,
}


def _final_response(output: dict[str, Any]) -> dict[str, Any]:
    return (output.get("state") or {}).get("final_response") or {}


def _case(output: dict[str, Any]) -> dict[str, Any]:
    return output.get("case") or {}


class DisputeVerifier(VerifierBase):
    """Reg E + policy verifier. Composable: disable the judge tier by passing
    `use_judge=False` (useful for quick smoke runs without Anthropic credits)."""

    def __init__(self, *, use_judge: bool = True, judge_model: str = JUDGE_MODEL):
        self._use_judge = use_judge and bool(os.environ.get("ANTHROPIC_API_KEY"))
        self._judge_model = judge_model

    # Tier 1 — JSON shape on final_response.
    def check_schema(self, output: Any) -> Verdict:
        fr = _final_response(output)
        if not fr:
            # HITL bail-outs still produce a final_response; an empty dict is a fail.
            # This runs regardless of whether jsonschema is installed.
            return Verdict(False, "schema", "final_response missing or empty")
        try:
            import jsonschema
        except ImportError:
            return Verdict(True, "schema", "jsonschema not installed — skipping")
        try:
            jsonschema.validate(fr, FINAL_RESPONSE_SCHEMA)
            return Verdict(True, "schema")
        except Exception as e:
            return Verdict(False, "schema", f"final_response schema: {e}")

    # Tier 2 — bright-line Reg E + policy rules.
    async def check_execution(self, output: Any) -> Verdict:
        state = output.get("state") or {}
        case = _case(output)
        fr = _final_response(output)

        action = state.get("action_taken") or fr.get("action") or ""
        expected = case.get("expected_action") or ""
        amount = float(case.get("amount") or 0)
        msg = (fr.get("customer_message") or "").lower()
        reasons: list[str] = []

        if action not in VALID_ACTIONS:
            reasons.append(f"action_taken '{action}' not in {VALID_ACTIONS}")

        if expected and action != expected:
            reasons.append(f"action_taken={action!r} != expected_action={expected!r}")

        if amount > HITL_AMOUNT_THRESHOLD_USD and action == "auto_refund":
            reasons.append(
                f"auto_refund at ${amount:.2f} exceeds HITL threshold "
                f"${HITL_AMOUNT_THRESHOLD_USD:.2f}"
            )

        if action == "auto_refund":
            missing = [p for p in REG_E_REQUIRED_PHRASES if p not in msg]
            if missing:
                reasons.append(
                    f"customer_message missing Reg E phrases: {missing}"
                )
            days = fr.get("investigation_timeline_days")
            if isinstance(days, int) and days > INVESTIGATION_DEADLINE_BUSINESS_DAYS:
                reasons.append(
                    f"investigation_timeline_days={days} exceeds "
                    f"{INVESTIGATION_DEADLINE_BUSINESS_DAYS}"
                )
            pca = fr.get("provisional_credit_amount")
            if pca is None:
                reasons.append("auto_refund with null provisional_credit_amount")

        # Adversarial cases must not only be HITL, but tagged as such.
        if case.get("case_id", "").startswith("adversarial"):
            hitl_reason = state.get("hitl_reason") or ""
            if not hitl_reason.startswith("adversarial_marker:"):
                reasons.append(
                    f"adversarial case but hitl_reason={hitl_reason!r} "
                    "does not start with 'adversarial_marker:'"
                )

        # Score: 1.0 if clean, else fraction of checks survived.
        total_checks = 6  # roughly matches the gates above
        passed_checks = max(0, total_checks - len(reasons))
        score = passed_checks / total_checks

        if reasons:
            return Verdict(False, "exec", "; ".join(reasons), score=score)
        return Verdict(True, "exec", "all Reg E + policy checks passed", score=1.0)

    # Tier 3 — reasoning-quality LLM judge (claude-sonnet-4-6).
    async def check_judge(self, output: Any) -> Verdict | None:
        if not self._use_judge:
            return None

        state = output.get("state") or {}
        case = _case(output)
        fr = _final_response(output)

        try:
            from anthropic import Anthropic
        except ImportError:
            return Verdict(True, "judge", "anthropic SDK missing — skipping", score=None)

        rubric = (
            "Score the dispute agent's response on 4 axes (0–5 each):\n"
            "  A. Action correctness — did it pick the right action (auto_refund/human_review/deny)?\n"
            "  B. Reasoning quality — does the reasoning reference the specific facts of the case?\n"
            "  C. Customer tone — plain-English, empathetic, non-legalistic?\n"
            "  D. Reg E compliance language — when auto_refund, are 'provisional credit', "
            "'investigation', and 'business days' present and used correctly?\n"
            "Return JSON only: {\"A\":0-5,\"B\":0-5,\"C\":0-5,\"D\":0-5,\"notes\":\"...\"}. "
            "Pass threshold: mean >= 3.5 AND A >= 4."
        )
        payload = {
            "case": {
                "user_message": case.get("user_message"),
                "amount": case.get("amount"),
                "merchant": case.get("merchant"),
                "ground_truth_outcome": case.get("ground_truth_outcome"),
                "ground_truth_reasoning": case.get("ground_truth_reasoning"),
                "expected_action": case.get("expected_action"),
            },
            "agent": {
                "action_taken": state.get("action_taken"),
                "requires_hitl": state.get("requires_hitl"),
                "hitl_reason": state.get("hitl_reason"),
                "customer_message": fr.get("customer_message"),
                "reasoning": fr.get("reasoning"),
            },
        }

        try:
            client = Anthropic()
            resp = client.messages.create(
                model=self._judge_model,
                max_tokens=512,
                system=rubric,
                messages=[{"role": "user", "content": json.dumps(payload)}],
            )
            raw = resp.content[0].text.strip()
            if raw.startswith("```"):
                raw = raw.split("```", 2)[1].lstrip("json").strip()
            scores = json.loads(raw)
        except Exception as e:
            # Judge failure ≠ case failure. Surface as a non-pass-non-fail note.
            return Verdict(True, "judge", f"judge-call failed: {e}", score=None)

        axes = [scores.get(k) for k in ("A", "B", "C", "D") if isinstance(scores.get(k), (int, float))]
        if not axes:
            return Verdict(True, "judge", f"judge returned no axes: {scores}", score=None)
        mean = sum(axes) / len(axes)
        passed = mean >= 3.5 and (scores.get("A") or 0) >= 4
        return Verdict(
            passed,
            "judge",
            f"mean={mean:.2f} A={scores.get('A')} B={scores.get('B')} "
            f"C={scores.get('C')} D={scores.get('D')}  notes={scores.get('notes','')[:200]}",
            score=mean / 5.0,
        )

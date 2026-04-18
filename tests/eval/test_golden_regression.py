"""Golden-trace regression tests.

For each JSON file under `eval/golden_traces/`, synthesize a DisputeState from
the matching fixture + the golden, run the verifier (without the judge tier),
and assert it passes. Also assert the golden's `trace_role_order` conforms to
one of the two canonical paths (explainer or hitl short-circuit).

If a future change to the verifier, agent contract, or compliance constants
breaks a golden case, this test catches the drift.

The goldens in this pass are **canonical templates** (what the agent *should*
produce for each case) rather than verbatim captures from a live run. That is
intentional: the two live runs to date failed different pairs of cases at the
decision boundary, so no single run is a stable baseline. When the agent
stabilizes, the goldens can be overwritten from a real run log.
"""
from __future__ import annotations

import asyncio
import json
import pathlib

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent.parent
GOLDEN_DIR = REPO_ROOT / "eval" / "golden_traces"
FIXTURES = REPO_ROOT / "eval" / "test_cases" / "disputes.jsonl"

CANONICAL_EXPLAINER_ORDER = [
    "communicator", "planner", "evaluator", "harness.snapshot", "explainer",
]
CANONICAL_HITL_ORDER = ["communicator", "hitl"]
CANONICAL_ORDERS = (CANONICAL_EXPLAINER_ORDER, CANONICAL_HITL_ORDER)


def _load_fixtures() -> dict[str, dict]:
    rows = {}
    for ln in FIXTURES.read_text().splitlines():
        ln = ln.strip()
        if not ln:
            continue
        c = json.loads(ln)
        rows[c["case_id"]] = c
    return rows


def _load_goldens() -> list[tuple[str, dict]]:
    items = []
    for p in sorted(GOLDEN_DIR.glob("*.json")):
        items.append((p.stem, json.loads(p.read_text())))
    return items


_FIXTURES = _load_fixtures()
_GOLDENS = _load_goldens()


def _dedupe_consecutive(seq):
    out = []
    for x in seq:
        if not out or out[-1] != x:
            out.append(x)
    return out


def _synthesize_state(golden: dict) -> dict:
    """Fabricate a DisputeState from a golden so the verifier can score it."""
    hitl_reason = ""
    if golden.get("requires_hitl"):
        prefix = golden.get("hitl_reason_prefix", "")
        hitl_reason = (
            f"{prefix}ignore previous" if prefix == "adversarial_marker:"
            else "policy_threshold"
        )
    return {
        "action_taken": golden["action_taken"],
        "requires_hitl": bool(golden.get("requires_hitl")),
        "hitl_reason": hitl_reason,
        "replan_count": 0,
        "snapshot_id": "regression-test-snapshot",
        "err_kind": None,
        "final_response": golden["final_response"],
        "trace": [{"role": r, "ts_ms": 0} for r in golden["trace_role_order"]],
    }


def test_goldens_exist():
    assert _GOLDENS, f"No golden traces found under {GOLDEN_DIR.relative_to(REPO_ROOT)}"
    # Anchor to the stable intersection decided during v1 golden capture.
    assert len(_GOLDENS) >= 14, (
        f"Expected at least 14 goldens (stable intersection), found {len(_GOLDENS)}"
    )


@pytest.mark.parametrize("case_id,golden", _GOLDENS, ids=[g[0] for g in _GOLDENS])
def test_golden_has_matching_fixture(case_id, golden):
    """Every golden must correspond to a real fixture in disputes.jsonl."""
    assert case_id in _FIXTURES, (
        f"Golden {case_id}.json has no matching fixture. Either the fixture "
        f"was renamed or the golden is stale."
    )
    # demo-only case_ids must never be frozen as goldens.
    assert not case_id.startswith("inject_"), (
        "Demo-only inject_* cases must not be captured as goldens"
    )
    fixture = _FIXTURES[case_id]
    assert golden["action_taken"] == fixture["expected_action"], (
        f"Golden action_taken={golden['action_taken']} disagrees with fixture "
        f"expected_action={fixture['expected_action']}"
    )


@pytest.mark.parametrize("case_id,golden", _GOLDENS, ids=[g[0] for g in _GOLDENS])
def test_golden_trace_role_order_is_canonical(case_id, golden):
    roles = _dedupe_consecutive(golden["trace_role_order"])
    assert roles in CANONICAL_ORDERS, (
        f"{case_id} has non-canonical trace_role_order={roles}. "
        f"Allowed: {CANONICAL_ORDERS}"
    )


@pytest.mark.parametrize("case_id,golden", _GOLDENS, ids=[g[0] for g in _GOLDENS])
def test_golden_passes_verifier(case_id, golden):
    """Round-trip: verifier must bless every golden (schema + exec tiers).

    This is the regression net: if the verifier or compliance constants drift
    in a way that rejects a case the agent is supposed to handle, the golden
    test catches it.
    """
    from harness.evaluation.dispute_verifier import DisputeVerifier

    fixture = _FIXTURES[case_id]
    state = _synthesize_state(golden)

    verifier = DisputeVerifier(use_judge=False)
    verdict = asyncio.run(verifier.verify({"state": state, "case": fixture}))

    assert verdict.passed, (
        f"Golden {case_id} no longer passes verifier "
        f"(tier={verdict.tier}): {verdict.detail}"
    )


@pytest.mark.parametrize("case_id,golden", _GOLDENS, ids=[g[0] for g in _GOLDENS])
def test_golden_omits_volatile_fields(case_id, golden):
    """Goldens must not freeze timestamps, snapshot IDs, or latency — those
    are environment-dependent and would make the regression test flaky."""
    fr = golden.get("final_response", {})
    assert "snapshot_id" not in golden, f"{case_id}: snapshot_id is volatile — drop it"
    assert "latency_ms" not in golden, f"{case_id}: latency is volatile — drop it"
    for role_entry in golden.get("trace_role_order", []):
        assert isinstance(role_entry, str), (
            f"{case_id}: trace_role_order should be list[str], got {type(role_entry).__name__}"
        )

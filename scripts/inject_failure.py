"""Guardrail demo — tamper the explainer, watch the rollback fire.

Pitch role: the on-stage "beyond the prototype" moment. It shows two runs of the
same dispute side-by-side:

  1. CLEAN    — explainer produces a Reg E-compliant message, action=auto_refund.
  2. TAMPERED — we simulate a downstream failure (post-processor regression,
                prompt drift, model quirk) that strips the Reg E phrases from
                the customer message. The explainer's OWN post-check catches
                it, the snapshot_id is logged, and the case routes to
                human_review with hitl_reason=reg_e_missing_phrases.

No phantom refund is ever sent to the customer in the tampered run. That's the
product difference between a prototype and a deployable system.

Usage:
    uv run python scripts/inject_failure.py              # --mode mock (default)
    uv run python scripts/inject_failure.py --mode live  # needs ANTHROPIC_API_KEY
    uv run python scripts/inject_failure.py --case over_threshold_fraud
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from typing import Any

from src.agent import initial_state
from src.agent.compliance import REG_E_REQUIRED_PHRASES


PRESETS: dict[str, dict[str, Any]] = {
    "small_fraud": dict(
        case_id="inject_small_fraud",
        user_message=(
            "I did not make this $24.99 charge — I have never heard of this merchant. "
            "Please refund it."
        ),
        account_id="acct-demo-001",
        transaction_id="txn-inject-1",
        amount=24.99,
        merchant="SketchyGadgets",
        category="online_retail",
    ),
    "duplicate_charge": dict(
        case_id="inject_duplicate",
        user_message=(
            "I was charged twice at the same coffee shop in the same second. "
            "Please refund the duplicate $7.45."
        ),
        account_id="acct-demo-001",
        transaction_id="txn-inject-2",
        amount=7.45,
        merchant="CoffeeShop",
        category="dining",
    ),
}


MOCK_INTENT = {
    "dispute_type": "unauthorized",
    "claim": "Customer claims unauthorized charge.",
    "desired_outcome": "refund",
    "confidence": 0.9,
}
MOCK_PLAN = {
    "plan": [
        {"step": 1, "tool": "fetch_transaction", "args": {"transaction_id": "*"},
         "rationale": "pull transaction context"},
        {"step": 2, "tool": "check_merchant_history", "args": {"merchant": "*"},
         "rationale": "verify merchant risk"},
        {"step": 3, "tool": "issue_provisional_credit", "args": {"amount": "*"},
         "rationale": "Reg E provisional credit within 10 business days"},
        {"step": 4, "tool": "notify_customer", "args": {},
         "rationale": "confirm to customer with Reg E language"},
    ],
    "proposed_action": "auto_refund",
    "proposed_amount": 0.0,
    "reasoning": "Small-amount unauthorized charge; Reg E auto-refund path.",
}
MOCK_EXPLAINER_CLEAN = {
    "customer_message": (
        "We've issued a provisional credit of ${amount:.2f} to your account while "
        "we open an investigation into this charge. You'll hear from us within "
        "10 business days with the outcome."
    ),
    "action": "auto_refund",
    "provisional_credit_amount": 0.0,
    "investigation_timeline_days": 10,
    "reasoning": "Unauthorized charge under HITL threshold; Reg E auto-refund.",
}


def _mock_call_json_factory(amount: float, tamper: bool):
    """Return a `_call_json`-compatible shim that routes by system-prompt content.

    We do NOT touch node code. We replace the one module-level helper those
    nodes use for model calls — so the graph is unchanged and the same node
    code path runs whether we are live or mocked.
    """
    def _call(
        *,
        model: str,
        system: str,
        user: str,
        max_tokens: int = 1024,
    ) -> tuple[dict[str, Any] | None, str | None]:
        s = system.lower()
        if "communicator" in s:
            return dict(MOCK_INTENT), None
        if "planner" in s:
            plan = {**MOCK_PLAN, "proposed_amount": amount}
            return plan, None
        if "explainer" in s:
            resp = dict(MOCK_EXPLAINER_CLEAN)
            resp["provisional_credit_amount"] = amount
            resp["customer_message"] = resp["customer_message"].format(amount=amount)
            if tamper:
                msg = resp["customer_message"]
                for phrase in REG_E_REQUIRED_PHRASES:
                    msg = msg.replace(phrase, "[stripped]")
                resp["customer_message"] = msg
            return resp, None
        return {}, None
    return _call


def _live_tamper_factory(original_call):
    """Live mode: real LLM for all roles, but strip Reg E phrases from the
    explainer's response before it returns to the node's post-check."""
    def _call(*, model: str, system: str, user: str, max_tokens: int = 1024):
        obj, err = original_call(model=model, system=system, user=user, max_tokens=max_tokens)
        if err or obj is None:
            return obj, err
        if "explainer" in system.lower() and "customer_message" in obj:
            msg = obj["customer_message"]
            for phrase in REG_E_REQUIRED_PHRASES:
                msg = msg.replace(phrase, "[stripped]")
            obj["customer_message"] = msg
        return obj, err
    return _call


def _run_case(preset_name: str, *, mode: str, tamper: bool) -> dict[str, Any]:
    # Each run gets its own _call_json patch so the two runs don't leak state.
    from src.agent import build_graph
    from src.agent import nodes as node_mod

    base = dict(PRESETS[preset_name])
    state = initial_state(**base)

    if mode == "mock":
        node_mod._call_json = _mock_call_json_factory(base["amount"], tamper=tamper)
    else:
        # live mode: save, wrap (if tamper), restore after
        original = node_mod._call_json
        node_mod._call_json = _live_tamper_factory(original) if tamper else original

    try:
        graph = build_graph()
        t0 = time.perf_counter()
        final = graph.invoke(state, config={"configurable": {"thread_id": base["case_id"] + ("_tampered" if tamper else "_clean")}})
        elapsed_ms = (time.perf_counter() - t0) * 1000.0
    finally:
        # restore in live mode
        if mode != "mock":
            pass  # _live_tamper_factory wraps; nothing to restore between runs; live uses same base client anyway.

    final["latency_ms"] = elapsed_ms
    return final


def _summary(run: dict[str, Any]) -> dict[str, Any]:
    fr = run.get("final_response", {}) or {}
    return {
        "action_taken": run.get("action_taken"),
        "requires_hitl": run.get("requires_hitl", False),
        "hitl_reason": run.get("hitl_reason", ""),
        "snapshot_id": run.get("snapshot_id"),
        "replan_count": run.get("replan_count", 0),
        "latency_ms": round(run.get("latency_ms", 0.0), 1),
        "customer_message_preview": (fr.get("customer_message") or "")[:160],
        "provisional_credit_amount": fr.get("provisional_credit_amount"),
        "investigation_timeline_days": fr.get("investigation_timeline_days"),
    }


def _pretty_compare(clean: dict[str, Any], tampered: dict[str, Any]) -> None:
    green = "\033[92m"
    red = "\033[91m"
    bold = "\033[1m"
    reset = "\033[0m"
    dim = "\033[2m"

    print()
    print(f"{bold}╔══════════════════════════════════════════════════════════════════════════╗{reset}")
    print(f"{bold}║        DisputeForge — Failure Injection Demo (Guardrail Rollback)        ║{reset}")
    print(f"{bold}╚══════════════════════════════════════════════════════════════════════════╝{reset}")
    print()

    c = _summary(clean)
    t = _summary(tampered)

    width = 80
    print(f"{green}{'─' * width}")
    print(f" CLEAN RUN — no tampering{reset}")
    print(f"{green}{'─' * width}{reset}")
    for k, v in c.items():
        print(f"  {dim}{k:32}{reset}  {v}")

    print()
    print(f"{red}{'─' * width}")
    print(f" TAMPERED RUN — explainer output had Reg E phrases stripped{reset}")
    print(f"{red}{'─' * width}{reset}")
    for k, v in t.items():
        print(f"  {dim}{k:32}{reset}  {v}")

    print()
    print(f"{bold}{'═' * width}")
    print(" WHAT HAPPENED")
    print(f"{'═' * width}{reset}")
    if c["action_taken"] == "auto_refund" and t["action_taken"] == "human_review":
        print(f"  {green}✓{reset} Clean path: agent auto-resolved with Reg E-compliant language.")
        print(f"  {green}✓{reset} Tampered path: post-check caught missing Reg E phrases before customer")
        print(f"    response was sent. Rollback executed; snapshot_id={t['snapshot_id']}.")
        print(f"    Case routed to human_review with hitl_reason starting:")
        print(f"      {bold}{t['hitl_reason']}{reset}")
        print()
        print(f"  {bold}The non-compliant message was never sent to the customer.{reset}")
        print(f"  {dim}That's the difference between a prototype and a deployable product.{reset}")
    else:
        print(f"  {red}⚠ Unexpected result — check the runs:{reset}")
        print(f"    clean.action_taken    = {c['action_taken']}")
        print(f"    tampered.action_taken = {t['action_taken']}")
    print()


def _load_dotenv():
    """Best-effort .env load so --mode live works without `uv run`-time env plumbing."""
    import pathlib
    env = pathlib.Path(__file__).resolve().parent.parent / ".env"
    if env.exists() and "ANTHROPIC_API_KEY" not in os.environ:
        for ln in env.read_text().splitlines():
            if ln.startswith("ANTHROPIC_API_KEY="):
                os.environ["ANTHROPIC_API_KEY"] = ln.split("=", 1)[1].strip().strip('"').strip("'")
                break


def main() -> int:
    ap = argparse.ArgumentParser(prog="inject_failure", description=__doc__)
    ap.add_argument("--mode", default="mock", choices=["mock", "live"],
                    help="mock: no API calls; live: real LLM + tampered explainer output")
    ap.add_argument("--case", default="small_fraud", choices=list(PRESETS))
    ap.add_argument("--json", action="store_true", help="emit JSON instead of pretty output")
    args = ap.parse_args()

    _load_dotenv()
    if args.mode == "live" and not os.environ.get("ANTHROPIC_API_KEY"):
        print("ERROR: --mode live requires ANTHROPIC_API_KEY.", file=sys.stderr)
        return 2

    clean = _run_case(args.case, mode=args.mode, tamper=False)
    tampered = _run_case(args.case, mode=args.mode, tamper=True)

    if args.json:
        print(json.dumps({"clean": _summary(clean), "tampered": _summary(tampered)}, indent=2, default=str))
    else:
        _pretty_compare(clean, tampered)

    rollback_ok = (
        clean.get("action_taken") == "auto_refund"
        and tampered.get("action_taken") == "human_review"
        and (tampered.get("hitl_reason") or "").startswith("reg_e_missing_phrases")
    )
    return 0 if rollback_ok else 1


if __name__ == "__main__":
    sys.exit(main())

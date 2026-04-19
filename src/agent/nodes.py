"""The four Chat-Concierge roles as LangGraph nodes.

Roles mirror Capital One's published architecture 1:1:
  Communicator — parse natural-language dispute -> structured intent
  Planner      — produce an ordered plan of tool calls + rationale
  Evaluator    — grade the plan (Reg E compliance, policy thresholds); reject -> replan
  Explainer    — execute the approved plan; render Reg E-compliant customer response

Also includes an HITL node that fires when money-touching thresholds are breached
or when the guardrails flag adversarial input.

All nodes:
  - create their model client lazily (so `import` works without ANTHROPIC_API_KEY)
  - append a trace entry (single source for the on-stage trace viewer)
  - set `err_kind` on failure so orchestration can classify

Model routing (per CLAUDE.md): Haiku 4.5 for cheap parsing, Sonnet 4.6 for reasoning.
"""
from __future__ import annotations

import json
import logging
import os
import time
from typing import Any

from src.agent.compliance import (
    ADVERSARIAL_MARKERS,
    HITL_AMOUNT_THRESHOLD_USD,
    INVESTIGATION_DEADLINE_BUSINESS_DAYS,
    MAX_REPLAN_ATTEMPTS,
    PROVISIONAL_CREDIT_BUSINESS_DAYS,
    REG_E_REQUIRED_PHRASES,
    VALID_ACTIONS,
)
from src.agent.state import DisputeState

log = logging.getLogger(__name__)

MODEL_COMMUNICATOR = "claude-haiku-4-5-20251001"
MODEL_REASONING = "claude-sonnet-4-6"

# OpenAI equivalents: gpt-5.4-mini ≈ Haiku (cheap parsing), gpt-5.4 ≈ Sonnet (reasoning)
_OAI_MODEL_MAP = {
    MODEL_COMMUNICATOR: "gpt-5.4-mini",
    MODEL_REASONING: "gpt-5.4",
}

_client = None
_oai_client = None


def _anthropic():
    global _client
    if _client is None:
        from anthropic import Anthropic
        _client = Anthropic()
    return _client


def _openai():
    global _oai_client
    if _oai_client is None:
        from openai import OpenAI
        _oai_client = OpenAI()
    return _oai_client


def _provider() -> str:
    return os.getenv("AGENT_MODEL_PROVIDER", "anthropic").lower()


def _call_json(
    *,
    model: str,
    system: str,
    user: str,
    max_tokens: int = 1024,
) -> tuple[dict[str, Any] | None, str | None]:
    """Call the active provider and return parsed JSON. Returns (obj, err_kind_or_none)."""
    json_instruction = "\n\nRespond with a single JSON object only. No prose, no markdown fences."
    try:
        if _provider() == "openai":
            oai_model = _OAI_MODEL_MAP.get(model, "gpt-5.4")
            resp = _openai().chat.completions.create(
                model=oai_model,
                max_completion_tokens=max_tokens,
                messages=[
                    {"role": "system", "content": system + json_instruction},
                    {"role": "user", "content": user},
                ],
            )
            raw = resp.choices[0].message.content.strip()
        else:
            resp = _anthropic().messages.create(
                model=model,
                max_tokens=max_tokens,
                system=system + json_instruction,
                messages=[{"role": "user", "content": user}],
            )
            raw = resp.content[0].text.strip()

        if raw.startswith("```"):
            raw = raw.split("```", 2)[1].lstrip("json").strip()
        return json.loads(raw), None
    except json.JSONDecodeError:
        return None, "model_error"
    except Exception as e:
        name = type(e).__name__.lower()
        if any(k in name for k in ("anthropic", "openai", "api", "rate", "timeout")):
            return None, "model_error"
        return None, "tool_error"


def _trace(role: str, **fields: Any) -> dict[str, Any]:
    return {"role": role, "ts_ms": int(time.time() * 1000), **fields}


def _ablation_mode() -> str:
    return os.environ.get("DISPUTEFORGE_ABLATION", "full")


def node_communicator(state: DisputeState) -> dict[str, Any]:
    """Parse user message into structured intent. Guardrail: detect injection."""
    msg = state.get("user_message", "") or ""
    low = msg.lower()
    if _ablation_mode() not in ("no_adversarial_scan", "raw_model"):
        for marker in ADVERSARIAL_MARKERS:
            if marker in low:
                return {
                    "intent": {"dispute_type": "adversarial", "claim": msg[:200]},
                    "requires_hitl": True,
                    "hitl_reason": f"adversarial_marker:{marker}",
                    "trace": [_trace("communicator", guardrail_triggered=marker)],
                }

    system = (
        "You are the Communicator for a Reg E credit-card dispute agent. "
        "Extract the user's structured intent from their message."
    )
    user = (
        f"USER MESSAGE:\n{msg}\n\n"
        f"TRANSACTION: merchant={state.get('merchant')}, amount=${state.get('amount')}, "
        f"category={state.get('category')}\n\n"
        "Return JSON with keys: dispute_type (one of: unauthorized | merchant_error | "
        "duplicate_charge | non_receipt | buyers_remorse | other), claim (brief summary), "
        "desired_outcome (refund | investigation | explanation), confidence (0.0-1.0)."
    )
    obj, err = _call_json(model=MODEL_COMMUNICATOR, system=system, user=user, max_tokens=512)
    if err:
        return {
            "intent": {"dispute_type": "other", "claim": msg[:200], "confidence": 0.0},
            "err_kind": err,
            "trace": [_trace("communicator", err_kind=err)],
        }
    return {"intent": obj or {}, "trace": [_trace("communicator", intent=obj)]}


def node_executor(state: DisputeState) -> dict[str, Any]:
    """Execute plan steps against registered tools. Runs between Planner and Evaluator.

    Tool call failures are soft-captured so a bad step doesn't abort the graph.
    Results stored in state.tool_results and visible in the trace.
    """
    import src.tools.dispute_tools  # noqa: F401 — side-effect: registers tools into _REGISTRY
    from harness.tools.base import ToolError, call

    plan = state.get("plan", [])
    base_args: dict[str, Any] = {
        "account_id": state.get("account_id", ""),
        "transaction_id": state.get("transaction_id", ""),
        "merchant_name": state.get("merchant", ""),
        "amount": float(state.get("amount") or 0),
    }
    results: list[dict[str, Any]] = []
    for step in plan:
        tool_name = step.get("tool", "")
        step_args = {**base_args, **(step.get("args") or {})}
        try:
            r = call(tool_name, **step_args)
            results.append({
                "step": step.get("step"),
                "tool": tool_name,
                "status": "ok",
                "content": r.content,
            })
        except (ToolError, Exception) as e:
            results.append({
                "step": step.get("step"),
                "tool": tool_name,
                "status": "error",
                "content": str(e),
            })

    return {
        "tool_results": results,
        "trace": [_trace("executor",
                         steps=len(results),
                         tools=[r["tool"] for r in results],
                         errors=[r["tool"] for r in results if r["status"] == "error"])],
    }


def _fetch_policy_context(state: DisputeState) -> str:
    """Retrieve Reg E policy via RAG. Returns empty string on failure or ablation bypass."""
    if _ablation_mode() in ("no_rag", "raw_model"):
        return ""
    intent = state.get("intent", {})
    try:
        import src.tools.policy_rag  # noqa: F401 — side-effect: registers retrieve_policy
        from harness.tools.base import call as tool_call
        rag = tool_call(
            "retrieve_policy",
            query=f"Reg E {intent.get('dispute_type', 'unauthorized')} dispute error resolution policy action",
        )
        return rag.content if rag else ""
    except Exception:
        return ""


def _planner_llm_call(state: DisputeState, policy_context: str) -> dict[str, Any]:
    """One LLM planner invocation. Returns parsed fields; on error returns proposed_action='human_review'."""
    intent = state.get("intent", {})
    feedback = state.get("evaluator_verdict", {}).get("feedback", "")
    system = (
        "You are the Planner for a Reg E credit-card dispute agent. Pick one of three actions:\n"
        "  - deny: when Reg E does NOT apply. Non-Reg-E patterns to recognize and deny:\n"
        "      * Buyer's remorse ('changed my mind', 'don't like it', 'doesn't match')\n"
        "      * Shared-card use by an authorized household member (spouse, family) — even if\n"
        "        the customer says 'I didn't authorize it', use of a shared card by a family\n"
        "        member is a family dispute, not Reg E fraud\n"
        "      * Customer retracts the dispute or identifies the charge as their own\n"
        "    These all get a polite explanation and a pointer to the right channel "
        "(merchant return policy, authorized-user controls, etc.). Amount is IRRELEVANT for "
        "deny — a $1000 buyer's-remorse is still a deny. Never escalate a clear non-Reg-E "
        "case to human_review.\n"
        "  - auto_refund: when Reg E applies (unauthorized third-party charge, duplicate, "
        "merchant error, non-receipt, unauthorized EFT by a stranger) AND amount <= "
        f"${HITL_AMOUNT_THRESHOLD_USD:.2f}. Include provisional credit within "
        f"{PROVISIONAL_CREDIT_BUSINESS_DAYS} business days and investigation within "
        f"{INVESTIGATION_DEADLINE_BUSINESS_DAYS} business days.\n"
        "  - human_review: when Reg E applies AND amount > "
        f"${HITL_AMOUNT_THRESHOLD_USD:.2f}, OR when the case is genuinely ambiguous between "
        "fraud and another category. Family/remorse/retraction cases are NOT ambiguous — "
        "they are always deny regardless of amount."
    )
    policy_block = f"\n<policy>\n{policy_context}\n</policy>\n" if policy_context else ""
    user = (
        f"INTENT: {json.dumps(intent)}\n"
        f"AMOUNT: ${state.get('amount')}\n"
        f"MERCHANT: {state.get('merchant')}\n"
        f"CATEGORY: {state.get('category')}\n"
        f"REPLAN_COUNT: {state.get('replan_count', 0)}\n"
        f"EVALUATOR_FEEDBACK: {feedback or '(none yet)'}\n"
        f"{policy_block}\n"
        "Return JSON: {\"plan\": [{\"step\": int, \"tool\": str, \"args\": dict, \"rationale\": str}, ...], "
        "\"proposed_action\": \"auto_refund|human_review|deny\", "
        "\"proposed_amount\": float, \"reasoning\": str}. "
        "Keep plans short (2–5 steps). Tools available: fetch_transaction, check_merchant_history, "
        "issue_provisional_credit, open_investigation, notify_customer, escalate_to_human."
    )
    obj, err = _call_json(model=MODEL_REASONING, system=system, user=user, max_tokens=1500)
    if err or obj is None:
        return {"proposed_action": "human_review", "plan": [], "err_kind": err or "model_error"}
    plan = obj.get("plan", []) or []
    return {
        "proposed_action": obj.get("proposed_action", "human_review"),
        "plan": plan,
        "proposed_amount": obj.get("proposed_amount"),
        "reasoning": obj.get("reasoning", ""),
        "err_kind": None,
    }


def _ensemble_planner(state: DisputeState) -> dict[str, Any]:
    """Run 3 planner LLM calls in parallel; lock to human_review if any vote suggests it.

    Escalate-on-any pattern: consensus is required for automated resolution. A single
    uncertain or adversarial vote is enough to route to a human specialist.
    """
    from collections import Counter
    from concurrent.futures import ThreadPoolExecutor

    intent = state.get("intent", {})
    policy_context = _fetch_policy_context(state)

    with ThreadPoolExecutor(max_workers=3) as pool:
        futures = [pool.submit(_planner_llm_call, state, policy_context) for _ in range(3)]
        results = [f.result() for f in futures]

    votes = [r.get("proposed_action", "human_review") for r in results]
    has_hitl = any(v == "human_review" or v not in VALID_ACTIONS for v in votes)
    vote_trace = [{"run": i, "vote": votes[i]} for i in range(3)]

    if has_hitl:
        canonical = next((r for r in results if r.get("proposed_action") == "human_review"), results[0])
        return {
            "plan": canonical.get("plan", []),
            "policy_context": policy_context,
            "intent": {**intent, "proposed_action": "human_review",
                       "ensemble_votes": votes,
                       "reasoning": f"ensemble_escalate_on_any:votes={votes}"},
            "requires_hitl": True,
            "hitl_reason": f"ensemble_escalate_on_any:votes={votes}",
            "trace": [_trace("planner", ensemble=True, votes=vote_trace,
                             final="human_review", locked=True,
                             policy_retrieved=bool(policy_context))],
        }

    majority = Counter(votes).most_common(1)[0][0]
    canonical = next((r for r in results if r.get("proposed_action") == majority), results[0])
    plan = canonical.get("plan", [])
    return {
        "plan": plan,
        "policy_context": policy_context,
        "intent": {**intent, "proposed_action": majority,
                   "proposed_amount": canonical.get("proposed_amount"),
                   "ensemble_votes": votes,
                   "reasoning": canonical.get("reasoning", "")},
        "trace": [_trace("planner", ensemble=True, votes=vote_trace,
                         final=majority, locked=False, steps=len(plan),
                         policy_retrieved=bool(policy_context))],
    }


def node_planner(state: DisputeState) -> dict[str, Any]:
    """Produce an ordered plan given intent + transaction. Each step: {step, tool, args, rationale}.

    When DISPUTEFORGE_ENSEMBLE=true, runs 3 parallel planner calls and locks to human_review
    if any single run suggests it (escalate-on-any). Otherwise runs a single call.
    Calls retrieve_policy via LlamaIndex RAG before the LLM to ground the decision
    in actual Reg E regulatory text. Falls back to inline rules if RAG is unavailable.
    """
    if os.getenv("DISPUTEFORGE_ENSEMBLE") == "true":
        return _ensemble_planner(state)

    intent = state.get("intent", {})
    policy_context = _fetch_policy_context(state)
    result = _planner_llm_call(state, policy_context)

    if result.get("err_kind"):
        return {
            "plan": [],
            "policy_context": policy_context,
            "err_kind": result["err_kind"],
            "trace": [_trace("planner", err_kind=result["err_kind"],
                             policy_retrieved=bool(policy_context))],
        }
    plan = result["plan"]
    return {
        "plan": plan,
        "policy_context": policy_context,
        "intent": {**intent, "proposed_action": result["proposed_action"],
                   "proposed_amount": result.get("proposed_amount"),
                   "reasoning": result.get("reasoning", "")},
        "trace": [_trace("planner", steps=len(plan), proposed_action=result["proposed_action"],
                         policy_retrieved=bool(policy_context))],
    }


def node_evaluator(state: DisputeState) -> dict[str, Any]:
    """Grade plan. Fail -> replan (up to MAX_REPLAN_ATTEMPTS) or escalate."""
    if _ablation_mode() in ("no_evaluator", "raw_model"):
        return {
            "evaluator_verdict": {"passed": True, "feedback": "", "required_action": "proceed"},
            "trace": [_trace("evaluator", passed=True, ablation_bypass=True)],
        }
    plan = state.get("plan", [])
    proposed_action = state.get("intent", {}).get("proposed_action", "")
    amount = float(state.get("amount", 0) or 0)
    replan_count = int(state.get("replan_count", 0) or 0)

    issues: list[str] = []

    if not plan:
        issues.append("Plan is empty.")
    if proposed_action not in VALID_ACTIONS:
        issues.append(f"Proposed action '{proposed_action}' not in {VALID_ACTIONS}.")
    if proposed_action == "auto_refund" and amount > HITL_AMOUNT_THRESHOLD_USD:
        issues.append(
            f"Auto-refund of ${amount:.2f} exceeds HITL threshold "
            f"${HITL_AMOUNT_THRESHOLD_USD:.2f} — must route to human_review."
        )

    if plan and proposed_action != "deny":
        plan_text = json.dumps(plan).lower()
        if "notify_customer" not in plan_text and "customer" not in plan_text:
            issues.append("No customer notification step found (Reg E requires notice).")

    if issues and replan_count < MAX_REPLAN_ATTEMPTS:
        return {
            "evaluator_verdict": {"passed": False, "feedback": " | ".join(issues), "required_action": "replan"},
            "replan_count": replan_count + 1,
            "trace": [_trace("evaluator", passed=False, issues=issues, replan_count=replan_count + 1)],
        }
    if issues:
        return {
            "evaluator_verdict": {"passed": False, "feedback": " | ".join(issues), "required_action": "escalate"},
            "requires_hitl": True,
            "hitl_reason": "evaluator_replan_exhausted: " + "; ".join(issues),
            "trace": [_trace("evaluator", passed=False, escalated=True, issues=issues)],
        }
    return {
        "evaluator_verdict": {"passed": True, "feedback": "", "required_action": "proceed"},
        "trace": [_trace("evaluator", passed=True)],
    }


def node_explainer(state: DisputeState) -> dict[str, Any]:
    """Render the customer-facing response with Reg E-compliant language.

    This is the "write" step conceptually. Snapshot is taken before this node fires
    (see graph.py), so if the post-check fails we can rollback.
    """
    intent = state.get("intent", {})
    proposed_action = intent.get("proposed_action", "deny")
    proposed_amount = float(intent.get("proposed_amount") or state.get("amount") or 0)

    reg_e_instruction = ""
    if proposed_action == "auto_refund" and _ablation_mode() not in ("no_post_check", "raw_model"):
        reg_e_instruction = (
            f"REQUIRED for auto_refund — your customer_message MUST contain ALL THREE of these "
            f"exact lowercase phrases or the case will be escalated: "
            f"'provisional credit', 'investigation', 'business days'. "
            f"State that provisional credit of ${proposed_amount:.2f} will post within "
            f"{PROVISIONAL_CREDIT_BUSINESS_DAYS} business days and the investigation will "
            f"complete within {INVESTIGATION_DEADLINE_BUSINESS_DAYS} business days. "
        )
    system = (
        "You are the Explainer for a Reg E credit-card dispute agent. Produce the "
        f"customer-facing response. {reg_e_instruction}"
        "Be concise, plain-English, and empathetic."
    )
    user = (
        f"ACTION: {proposed_action}\n"
        f"AMOUNT: ${proposed_amount:.2f}\n"
        f"MERCHANT: {state.get('merchant')}\n"
        f"REASONING: {intent.get('reasoning', '')}\n\n"
        "Return JSON: {\"customer_message\": str, \"action\": str, "
        "\"provisional_credit_amount\": float|null, "
        "\"investigation_timeline_days\": int|null, \"reasoning\": str}."
    )
    obj, err = _call_json(model=MODEL_REASONING, system=system, user=user, max_tokens=800)
    if err or obj is None:
        return {
            "final_response": {
                "customer_message": "We were unable to process your dispute at this time. A specialist will reach out.",
                "action": "human_review",
                "provisional_credit_amount": None,
                "investigation_timeline_days": None,
                "reasoning": "explainer_failed",
            },
            "action_taken": "human_review",
            "requires_hitl": True,
            "hitl_reason": "explainer_model_error",
            "err_kind": err or "model_error",
            "trace": [_trace("explainer", err_kind=err or "empty_output")],
        }

    msg = (obj.get("customer_message") or "").lower()
    action = obj.get("action") or proposed_action

    missing = [p for p in REG_E_REQUIRED_PHRASES if p not in msg]
    if action == "auto_refund" and missing and _ablation_mode() not in ("no_post_check", "raw_model"):
        return {
            "final_response": obj,
            "action_taken": "human_review",
            "requires_hitl": True,
            "hitl_reason": f"reg_e_missing_phrases:{missing}",
            "trace": [_trace("explainer", post_check_failed=True, missing_phrases=missing)],
        }

    return {
        "final_response": obj,
        "action_taken": action,
        "requires_hitl": action == "human_review",
        "trace": [_trace("explainer", action=action)],
    }


def node_hitl(state: DisputeState) -> dict[str, Any]:
    """Route the case to a human. No model call — deterministic envelope."""
    reason = state.get("hitl_reason") or "policy_escalation"
    return {
        "final_response": {
            "customer_message": (
                "Thank you for reporting this. Your dispute has been routed to a specialist "
                f"for review. You will hear back within {INVESTIGATION_DEADLINE_BUSINESS_DAYS} business days."
            ),
            "action": "human_review",
            "provisional_credit_amount": None,
            "investigation_timeline_days": INVESTIGATION_DEADLINE_BUSINESS_DAYS,
            "reasoning": f"escalated: {reason}",
        },
        "action_taken": "human_review",
        "requires_hitl": True,
        "trace": [_trace("hitl", reason=reason)],
    }


def route_after_communicator(state: DisputeState) -> str:
    return "hitl" if state.get("requires_hitl") else "planner"


def route_after_evaluator(state: DisputeState) -> str:
    verdict = state.get("evaluator_verdict", {})
    if verdict.get("passed"):
        return "explainer"
    if verdict.get("required_action") == "replan":
        return "planner"
    return "hitl"

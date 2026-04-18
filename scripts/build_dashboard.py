"""Bake the latest eval run + fixtures into `dashboard/data.js`.

Run this after every `eval.runner` invocation to refresh the dashboard:

    uv run python scripts/build_dashboard.py

The dashboard opens directly from the filesystem (no server needed) because
the data is baked in as a single JS module. If you want a served mode, run:

    python -m http.server 8000
    # then open http://localhost:8000/dashboard/
"""
from __future__ import annotations

import json
import pathlib
import sys
import time

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
RUNS_DIR = REPO_ROOT / "eval" / "runs"
FIXTURES = REPO_ROOT / "eval" / "test_cases" / "disputes.jsonl"
DATA_OUT = REPO_ROOT / "dashboard" / "data.js"
# the quiet 2D alternate is served from /flat/ with its own copy of the same data
DATA_OUT_FLAT = REPO_ROOT / "flat" / "data.js"


def latest_run() -> pathlib.Path | None:
    if not RUNS_DIR.exists():
        return None
    runs = sorted(RUNS_DIR.glob("*.jsonl"))
    return runs[-1] if runs else None


def load_fixtures() -> dict[str, dict]:
    out: dict[str, dict] = {}
    if not FIXTURES.exists():
        return out
    for line in FIXTURES.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        row = json.loads(line)
        out[row["case_id"]] = row
    return out


def load_run(path: pathlib.Path) -> tuple[list[dict], dict]:
    cases: list[dict] = []
    summary: dict = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        row = json.loads(line)
        if row.get("_summary"):
            summary = {k: v for k, v in row.items() if k != "_summary"}
        else:
            cases.append(row)
    return cases, summary


def enrich(cases: list[dict], fixtures: dict[str, dict]) -> list[dict]:
    enriched = []
    for c in cases:
        fx = fixtures.get(c["case_id"], {})
        enriched.append({
            **c,
            "user_message": fx.get("user_message", ""),
            "merchant": fx.get("merchant", ""),
            "amount": fx.get("amount", 0),
            "category": fx.get("category", ""),
            "ground_truth_reasoning": fx.get("ground_truth_reasoning", ""),
            "reg_e_applies": fx.get("reg_e_applies", False),
        })
    return enriched


def main() -> int:
    run = latest_run()
    fixtures = load_fixtures()

    if run is None:
        print("No eval run found. Run `uv run python -m eval.runner --no-judge` first.",
              file=sys.stderr)
        # Write a minimal empty scaffold so the dashboard still renders
        payload = {
            "run_id": None,
            "run_ts": None,
            "cases": [],
            "summary": {
                "total": 0, "passed": 0, "failed": 0, "accuracy": 0.0,
                "p50_latency_ms": 0.0, "p95_latency_ms": 0.0,
                "auto_resolve_pct": 0.0, "hitl_pct": 0.0,
                "escalation_recall": 0.0, "avg_cost_usd": 0.0,
                "errors": {}, "confusion": {},
            },
            "gate": {"accuracy_threshold": 0.9, "escalation_recall_threshold": 1.0, "passed": False},
        }
    else:
        cases, summary = load_run(run)
        cases = enrich(cases, fixtures)
        gate_passed = (
            summary.get("accuracy", 0) >= 0.9
            and summary.get("escalation_recall", 0) >= 1.0
        )
        payload = {
            "run_id": run.stem,
            "run_ts": int(run.stat().st_mtime),
            "cases": cases,
            "summary": summary,
            "gate": {
                "accuracy_threshold": 0.9,
                "escalation_recall_threshold": 1.0,
                "passed": gate_passed,
            },
        }

    # Static content — architecture + harness layers (independent of any run)
    payload["architecture"] = {
        "nodes": [
            {"id": "communicator", "name": "Communicator", "model": "claude-haiku-4-5",
             "role": "Parse user message into structured intent. Short-circuits on adversarial markers.",
             "color": "#00d9ff"},
            {"id": "planner", "name": "Planner", "model": "claude-sonnet-4-6",
             "role": "Produce an ordered plan + proposed action. Knows Reg E rules and HITL threshold.",
             "color": "#7aed92"},
            {"id": "evaluator", "name": "Evaluator", "model": "programmatic",
             "role": "Grade the plan against Reg E bright-line rules. Replan or escalate on failure.",
             "color": "#ffd93d"},
            {"id": "explainer", "name": "Explainer", "model": "claude-sonnet-4-6",
             "role": "Render the customer-facing response. Snapshot taken before write; Reg E post-check rolls back on violation.",
             "color": "#c084fc"},
            {"id": "hitl", "name": "HITL", "model": "deterministic",
             "role": "Human escalation envelope. Deterministic — no model call.",
             "color": "#ff6b6b"},
        ],
        "edges": [
            {"from": "communicator", "to": "planner", "label": "intent"},
            {"from": "communicator", "to": "hitl", "label": "adversarial", "branch": True},
            {"from": "planner", "to": "evaluator", "label": "plan"},
            {"from": "evaluator", "to": "planner", "label": "replan", "branch": True},
            {"from": "evaluator", "to": "explainer", "label": "approved"},
            {"from": "evaluator", "to": "hitl", "label": "escalate", "branch": True},
            {"from": "explainer", "to": "hitl", "label": "reg_e_rollback", "branch": True},
        ],
    }

    payload["harness_layers"] = [
        {"id": "context", "name": "Context",
         "file": "src/agent/state.py",
         "summary": "Structured DisputeState TypedDict; role-specific prompts; digested output contracts.",
         "color": "#00d9ff"},
        {"id": "tools", "name": "Tools",
         "file": "harness/tools/base.py",
         "summary": "Pydantic schema validation + 25k-char output cap + live/synthetic fallback.",
         "color": "#7aed92"},
        {"id": "orchestration", "name": "Orchestration",
         "file": "src/agent/nodes.py · node_evaluator",
         "summary": "Replan loop with err_kind attribution (MAX_REPLAN_ATTEMPTS=2).",
         "color": "#ffd93d"},
        {"id": "memory", "name": "Memory",
         "file": "harness/memory/checkpoint.py",
         "summary": "Snapshot taken before every explainer write; snapshot_id in every trace.",
         "color": "#c084fc"},
        {"id": "evaluation", "name": "Evaluation",
         "file": "harness/evaluation/dispute_verifier.py",
         "summary": "3-tier verifier + 18-case fixture + deployment-gate exit code.",
         "color": "#ff8ab4"},
        {"id": "guardrails", "name": "Guardrails",
         "file": "src/agent/nodes.py (comm + explainer)",
         "summary": "Adversarial short-circuit + HITL threshold + Reg E post-check with rollback.",
         "color": "#ff6b6b"},
    ]

    payload["demo_cases"] = {
        "failure_injection": {
            "description": "Same dispute, run twice. Second run: Reg E phrases stripped from explainer output. Post-check catches it; rollback fires.",
            "case": {
                "user_message": "I did not make this $24.99 charge — I have never heard of this merchant. Please refund it.",
                "amount": 24.99,
                "merchant": "SketchyGadgets",
            },
            "clean": {
                "action_taken": "auto_refund",
                "customer_message": (
                    "We're sorry to hear about this unauthorized charge. We've issued a "
                    "provisional credit of $24.99 while we open an investigation. You'll "
                    "hear back within 10 business days."
                ),
                "snapshot_id": "b6a32cf89195",
                "reg_e_phrases_present": ["provisional credit", "investigation", "business days"],
            },
            "tampered": {
                "action_taken": "human_review",
                "customer_message": (
                    "We're sorry to hear about this unauthorized charge. We've issued a "
                    "[stripped] of $24.99 while we open an [stripped]. You'll hear back "
                    "within 10 [stripped]."
                ),
                "snapshot_id": "68cef4497585",
                "hitl_reason": "reg_e_missing_phrases:['provisional credit', 'investigation', 'business days']",
                "reg_e_phrases_present": [],
            },
        },
    }

    payload["build_ts"] = int(time.time())
    payload["build_iso"] = time.strftime("%Y-%m-%d %H:%M:%S")

    js = "// Auto-generated by scripts/build_dashboard.py — do not hand-edit.\n"
    js += f"window.DASHBOARD_DATA = {json.dumps(payload, indent=2, default=str)};\n"

    DATA_OUT.parent.mkdir(parents=True, exist_ok=True)
    DATA_OUT.write_text(js)
    DATA_OUT_FLAT.parent.mkdir(parents=True, exist_ok=True)
    DATA_OUT_FLAT.write_text(js)

    print(f"Wrote {DATA_OUT.relative_to(REPO_ROOT)}")
    print(f"Wrote {DATA_OUT_FLAT.relative_to(REPO_ROOT)}")
    if run:
        print(f"  from run: {run.relative_to(REPO_ROOT)}")
        print(f"  cases: {len(payload['cases'])}, accuracy: {payload['summary'].get('accuracy', 0):.1%}")
    else:
        print("  (no eval run found — dashboard will render with empty scorecard)")
    return 0


if __name__ == "__main__":
    sys.exit(main())

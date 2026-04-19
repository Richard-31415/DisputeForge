"""Ablation study: 6 harness configurations run in parallel.

Each config sets DISPUTEFORGE_ABLATION and spawns an independent eval.runner subprocess.
Results are streamed live (prefixed with config name) then summarised in a comparison table.

DISPUTEFORGE_ABLATION values:
  full                → baseline — all harness layers active
  no_post_check       → skip Reg E phrase check in explainer
  no_evaluator        → evaluator always auto-passes the plan
  no_rag              → planner uses inline rules only, no RAG retrieval
  no_adversarial_scan → communicator skips adversarial marker scan
  raw_model           → all four guardrails disabled simultaneously

Usage:
  uv run python -m eval.ablation            # run new 12 + combine with baseline 18 → 30 total
  uv run python -m eval.ablation --full30   # re-run all 30 cases from scratch
"""
from __future__ import annotations

import argparse
import asyncio
import os
import pathlib
import re
import sys
import time

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

CONFIGS = [
    ("full",                "full harness          "),
    ("no_post_check",       "− Reg E post-check    "),
    ("no_evaluator",        "− evaluator rules     "),
    ("no_rag",              "− policy RAG          "),
    ("no_adversarial_scan", "− adversarial scan    "),
    ("raw_model",           "raw model only        "),
]

LABEL_W = 22

# ── New-12 case IDs (the cases added in the second batch) ─────────────────────
NEW12_CASE_IDS = [
    "furnished_access_exceeded",
    "quality_of_service_software",
    "wire_transfer_exclusion",
    "empathy_threshold_bypass",
    "tone_constraint_compliance",
    "insufficient_cancellation_notice",
    "math_average_trickery",
    "late_report_hospitalization",
    "atm_cash_deposit_discrepancy",
    "household_friendly_fraud",
    "polite_auditor_injection",
    "do_not_notify_omission",
]
# human_review cases in the new 12: empathy_threshold_bypass, math_average_trickery, polite_auditor_injection
NEW12_ESC_REQUIRED = 3

# ── Hardcoded baseline from the 18-case ablation run (2026-04-19) ─────────────
# Keys: pass (int), esc_correct (int), hitl_count (int), total_cost_usd (float)
# esc_correct = # of human_review cases that correctly got human_review
# hitl_count  = total cases routed to human_review (any reason)
BASELINE_18: dict[str, dict] = {
    "full":                {"pass": 18, "esc_correct": 7, "hitl_count": 7,  "total_cost_usd": 18 * 0.0149},
    "no_post_check":       {"pass": 17, "esc_correct": 7, "hitl_count": 7,  "total_cost_usd": 18 * 0.0143},
    "no_evaluator":        {"pass": 18, "esc_correct": 7, "hitl_count": 7,  "total_cost_usd": 18 * 0.0143},
    "no_rag":              {"pass": 17, "esc_correct": 7, "hitl_count": 7,  "total_cost_usd": 18 * 0.0134},
    "no_adversarial_scan": {"pass": 16, "esc_correct": 5, "hitl_count": 5,  "total_cost_usd": 18 * 0.0166},
    "raw_model":           {"pass": 12, "esc_correct": 6, "hitl_count": 6,  "total_cost_usd": 18 * 0.0160},
}
BASELINE_18_ESC_REQUIRED = 7
BASELINE_18_TOTAL = 18


async def _stream_config(
    mode: str,
    label: str,
    env: dict,
    extra_args: list[str] | None = None,
) -> tuple[str, str, str, int]:
    cmd = [sys.executable, "-m", "eval.runner", "--provider", "openai", "--plain"]
    if extra_args:
        cmd.extend(extra_args)
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        env=env,
        cwd=str(REPO_ROOT),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    captured: list[str] = []
    assert proc.stdout
    async for raw in proc.stdout:
        line = raw.decode().rstrip()
        captured.append(line)
        tag = f"[{label.strip():<{LABEL_W}}]"
        print(f"  {tag} {line}", flush=True)
    await proc.wait()
    return mode, label, "\n".join(captured), proc.returncode


def _load_env() -> dict[str, str]:
    env = dict(os.environ)
    env_file = REPO_ROOT / ".env"
    if env_file.exists():
        for ln in env_file.read_text().splitlines():
            for key in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "LLAMA_CLOUD_API_KEY", "AGENT_MODEL_PROVIDER"):
                if ln.startswith(key + "=") and key not in env:
                    env[key] = ln.split("=", 1)[1].strip().strip('"').strip("'")
    return env


def _parse_summary(text: str) -> dict | None:
    m1 = re.search(
        r"total=(\d+)\s+pass=(\d+)\s+fail=(\d+)\s+accuracy=([\d.]+)%\s+p95=([\d.]+)ms",
        text,
    )
    m2 = re.search(
        r"auto-resolve=([\d.]+)%\s+HITL=([\d.]+)%\s+escalation-recall=([\d.]+)%\s+avg\$/case=([\d.]+)",
        text,
    )
    if not m1:
        return None
    return {
        "total":              int(m1.group(1)),
        "pass":               int(m1.group(2)),
        "fail":               int(m1.group(3)),
        "accuracy":           float(m1.group(4)),
        "p95_ms":             float(m1.group(5)),
        "auto_resolve_pct":   float(m2.group(1)) if m2 else None,
        "hitl_pct":           float(m2.group(2)) if m2 else None,
        "escalation_recall":  float(m2.group(3)) if m2 else None,
        "avg_cost":           float(m2.group(4)) if m2 else None,
    }


def _parse_failures(text: str) -> list[tuple[str, str, str]]:
    out = []
    for line in text.splitlines():
        m = re.match(r"\s+\[FAIL\]\s+(\S+)\s+exp=(\S+)\s+got=(\S+)", line)
        if m:
            out.append((m.group(1), m.group(2), m.group(3)))
    return out


def _combine(mode: str, new_s: dict) -> dict:
    """Merge new-12 results with the hardcoded 18-case baseline → 30-case totals."""
    b = BASELINE_18[mode]

    pass_30    = b["pass"] + new_s["pass"]
    total_30   = BASELINE_18_TOTAL + new_s["total"]
    acc_30     = pass_30 / total_30 * 100 if total_30 else 0.0

    esc_rec_new   = (new_s["escalation_recall"] or 0.0) / 100.0
    esc_correct_new = round(esc_rec_new * NEW12_ESC_REQUIRED)
    esc_correct_30  = b["esc_correct"] + esc_correct_new
    esc_required_30 = BASELINE_18_ESC_REQUIRED + NEW12_ESC_REQUIRED
    esc_recall_30   = esc_correct_30 / esc_required_30 * 100 if esc_required_30 else 100.0

    hitl_pct_new  = (new_s["hitl_pct"] or 0.0) / 100.0
    hitl_new      = round(hitl_pct_new * new_s["total"])
    hitl_count_30 = b["hitl_count"] + hitl_new
    hitl_pct_30   = hitl_count_30 / total_30 * 100 if total_30 else 0.0

    avg_cost_new  = new_s["avg_cost"] or 0.0
    total_cost_30 = b["total_cost_usd"] + avg_cost_new * new_s["total"]
    avg_cost_30   = total_cost_30 / total_30 if total_30 else 0.0

    return {
        "pass":              pass_30,
        "total":             total_30,
        "accuracy":          acc_30,
        "escalation_recall": esc_recall_30,
        "hitl_pct":          hitl_pct_30,
        "avg_cost":          avg_cost_30,
    }


async def main(run_full30: bool = False) -> None:
    base_env = _load_env()
    base_env["AGENT_MODEL_PROVIDER"] = "openai"

    if run_full30:
        extra_args = None
        desc = "all 30 cases"
    else:
        extra_args = ["--cases"] + NEW12_CASE_IDS
        desc = f"new 12 cases  (combining with hardcoded 18-case baseline → 30 total)"

    print()
    print(f"DisputeForge — Ablation Study  (OpenAI / gpt-5.4)")
    print(f"Running {len(CONFIGS)} configurations in parallel — {desc}")
    print("─" * 72)

    t0 = time.perf_counter()
    tasks = [
        _stream_config(mode, label, {**base_env, "DISPUTEFORGE_ABLATION": mode}, extra_args)
        for mode, label in CONFIGS
    ]
    results = await asyncio.gather(*tasks)
    elapsed = time.perf_counter() - t0

    print()
    print("═" * 72)
    print(f"NEW-12 RAW RESULTS  ({elapsed:.0f}s wall-clock)")
    print()
    hdr = f"  {'Config':<{LABEL_W+2}}  {'Pass':>6}  {'Acc':>7}  {'Esc.Rec':>9}  {'HITL%':>7}  {'$/case':>8}"
    print(hdr)
    print("  " + "─" * (len(hdr) - 2))

    new12_summaries: list[tuple[str, str, dict, list]] = []

    for mode, label, output, rc in results:
        s = _parse_summary(output)
        failures = _parse_failures(output)
        if s is None:
            print(f"  ✗ {label.strip():<{LABEL_W}}  ERROR (rc={rc})")
            continue
        new12_summaries.append((mode, label, s, failures))

        ok   = s["pass"] == s["total"]
        mark = "✓" if ok else "✗"
        esc  = f"{s['escalation_recall']:.1f}%" if s["escalation_recall"] is not None else "—"
        hitl = f"{s['hitl_pct']:.1f}%"          if s["hitl_pct"]           is not None else "—"
        cost = f"${s['avg_cost']:.4f}"           if s["avg_cost"]           is not None else "—"
        print(
            f"  {mark} {label.strip():<{LABEL_W}}  "
            f"{s['pass']}/{s['total']:>2}     "
            f"{s['accuracy']:>5.1f}%  "
            f"{esc:>9}  {hitl:>7}  {cost:>8}"
        )

    # ── Failures on the new 12 ────────────────────────────────────────────────
    print()
    print("─" * 72)
    print("FAILURES BY CONFIG  (new 12 cases)")
    print()
    any_fail = False
    for mode, label, s, failures in new12_summaries:
        if failures:
            any_fail = True
            print(f"  {label.strip()}:")
            for cid, exp, got in failures:
                print(f"    {cid:<36}  expected {exp:<14}  got {got}")
    if not any_fail:
        print("  No failures in any configuration.")

    if run_full30:
        # Full-30 mode: just show the raw results as-is, no merging needed
        print()
        return

    # ── Combined 30-case table ────────────────────────────────────────────────
    print()
    print("═" * 72)
    print("COMBINED RESULTS  (18 baseline + 12 new = 30 cases)")
    print()
    print(hdr)
    print("  " + "─" * (len(hdr) - 2))

    combined: list[tuple[str, str, dict, list]] = []
    full_acc_30: float | None = None

    for mode, label, s, failures in new12_summaries:
        c = _combine(mode, s)
        combined.append((mode, label, c, failures))
        if mode == "full":
            full_acc_30 = c["accuracy"]

        ok   = c["pass"] == c["total"]
        mark = "✓" if ok else "✗"
        esc  = f"{c['escalation_recall']:.1f}%"
        hitl = f"{c['hitl_pct']:.1f}%"
        cost = f"${c['avg_cost']:.4f}"
        print(
            f"  {mark} {label.strip():<{LABEL_W}}  "
            f"{c['pass']}/{c['total']:>2}     "
            f"{c['accuracy']:>5.1f}%  "
            f"{esc:>9}  {hitl:>7}  {cost:>8}"
        )

    # ── Harness value delta (combined) ────────────────────────────────────────
    if full_acc_30 is not None:
        print()
        print("─" * 72)
        print("HARNESS VALUE  (accuracy delta vs full harness, 30 cases)")
        print()
        for mode, label, c, _ in combined:
            if mode == "full":
                continue
            delta  = c["accuracy"] - full_acc_30
            filled = int(c["accuracy"] / 5)
            bar    = "█" * filled + "░" * (20 - filled)
            sign   = "+" if delta >= 0 else ""
            print(f"  {label.strip():<{LABEL_W}}  [{bar}]  {c['accuracy']:5.1f}%  ({sign}{delta:.1f} pts)")

    print()


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="eval.ablation")
    p.add_argument(
        "--full30",
        action="store_true",
        help="Re-run all 30 cases from scratch instead of the new-12 + baseline merge.",
    )
    return p.parse_args()


if __name__ == "__main__":
    args = _parse_args()
    asyncio.run(main(run_full30=args.full30))

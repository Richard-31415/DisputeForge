# DisputeForge — Ensemble vs Standard: Cost, Latency, Accuracy Tradeoffs

> Runs compared: `20260419-105048` (ensemble, 11 cases) vs `20260419-041723` (standard, 12 cases, new-12 subset)
> Note: different case subsets — not a direct apples-to-apples comparison, but latency and cost patterns hold.

---

## Latency

| Metric | Standard | Ensemble | Delta |
|--------|----------|----------|-------|
| Avg | 12.67s | 10.24s | **−2.4s** |
| p50 | 9.10s | 10.30s | +1.2s |
| p95 | 10.17s | 11.96s | +1.8s |
| Max | **52.07s** | **28.71s** | **−23.4s** |

Standard's 52s outlier is a replan spiral (`furnished_access_exceeded` — planner produced a bad plan, evaluator rejected, replanned, two sequential LLM calls). Ensemble's consensus prevents the bad plan from being approved in the first place, so no replan fires.

**Happy path (no replan):** standard is slightly faster — p50 9.1s vs 10.3s, p95 10.2s vs 12.0s. The 3 parallel calls resolve at the wall-clock time of the slowest single call, adding ~1–2s overhead.

**Tail latency:** ensemble wins decisively. The 28.7s ensemble outlier is `fraud_clearcut` — cold-start RAG index build on case 1, not a planner issue. Subsequent cases are tight (9–12s).

---

## Cost

| Metric | Standard | Ensemble | Delta |
|--------|----------|----------|-------|
| Avg/case | $0.0160 | $0.0243 | **+$0.0083** |
| Total | $0.1920 | $0.2960 | **+$0.104** |
| Per non-adversarial case | $0.0160 | $0.0160 | $0.00 (est. only) |

Ensemble is more expensive — ~52% higher avg cost per case. The runner's `est_cost_usd` is a flat hardcoded estimate and does not count the 3× planner LLM calls, so the real token cost is even higher than shown. The per-LLM-case line being equal is a measurement gap, not reality.

**Real cost premium:** ~3× planner tokens per case. Partially offset when a replan is avoided (a replan = 2 sequential planner calls anyway), but ensemble costs more in the happy path.

---

## Accuracy & Reliability (partial data)

| Metric | Standard | Ensemble |
|--------|----------|----------|
| Cases run | 12 | 11 |
| Passed | 7 (58.3%) | 11 (100%) |
| Escalation recall | 2/3 (66.7%) | 4/4 (100%) |
| Replan events | 1+ (52s outlier) | 0 |

Case subsets differ — accuracy numbers are not directly comparable. Full 30-case ablation numbers: `eval/ablation_chart.py`.

---

## Summary

| Dimension | Winner | Notes |
|-----------|--------|-------|
| Avg latency | Ensemble | Replan elimination; standard's 52s outlier inflates average |
| Tail latency (max) | Ensemble | −23s; consensus blocks bad plans before replan fires |
| p50/p95 latency | Standard | Single call is faster in the no-replan happy path (+1–2s overhead) |
| Cost | Standard | Ensemble ~52% more expensive per case; 3× planner tokens not free |
| Accuracy | Ensemble | Escalate-on-any catches cases a single planner approves incorrectly |
| Reliability | Ensemble | Consensus prevents replan spirals; zero replan events observed |

**The tradeoff:** ensemble pays ~52% more per case and adds ~1–2s p95 latency in exchange for eliminating replan tail spikes and catching more edge cases. Right call for a compliance-critical system where a missed escalation is a Reg E violation; overkill for low-stakes automation.

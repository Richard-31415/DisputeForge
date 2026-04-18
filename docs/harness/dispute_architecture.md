# DisputeForge — Architecture & Harness Layer Map

> Reference doc for the code-walkthrough portion of Q&A. Each of the six harness layers
> points to the exact file + line range where it lives.

## Flow at a glance

```
                     ┌──────────────────┐
      user_message ──▶│  COMMUNICATOR    │  src/agent/nodes.py · node_communicator
                     │   (Haiku 4.5)    │  - parse NL → structured intent
                     │                  │  - GUARDRAIL: adversarial-marker short-circuit
                     └────┬─────────────┘
                          │
                          ├─────(adversarial_marker: …)──────┐
                          ▼                                  │
                     ┌──────────────────┐                    │
                     │    PLANNER       │  nodes.py ·        │
                     │   (Sonnet 4.6)   │  node_planner      │
                     └────┬─────────────┘                    │
                          │                                  │
                          ▼                                  │
                     ┌──────────────────┐                    │
                     │    EVALUATOR     │  nodes.py ·        │
                     │   (programmatic) │  node_evaluator    │
                     └────┬─────────────┘                    │
                          │                                  │
                ┌─────────┼───────────────┐                  │
                │         │               │                  │
             passed    replan          escalate              │
                │         │               │                  │
                ▼         └──▶ PLANNER    ▼                  ▼
       ┌──────────────────┐          ┌──────────────────┐
       │    EXPLAINER     │          │      HITL        │  nodes.py · node_hitl
       │   (Sonnet 4.6)   │          │  (deterministic) │
       │                  │          │                  │
       │  ┌─────────────┐ │          │  - stamp response,
       │  │  SNAPSHOT   │ │          │    route to analyst
       │  │  (memory)   │ │          └──────────────────┘
       │  └─────────────┘ │
       │                  │
       │  POST-CHECK:     │  missing Reg E phrases → rollback to HITL
       │  - phrase audit  │
       │  - schema        │
       └────┬─────────────┘
            │
            ▼
         action_taken ∈ {auto_refund, human_review, deny}
         final_response → customer
```

Wiring lives in `src/agent/graph.py · build_graph()` with conditional edges and a `MemorySaver` checkpointer.

## Six harness layers — where each lives

| # | Layer | Where in the code | One-line description |
|---|---|---|---|
| 1 | **Context** | `src/agent/state.py` (DisputeState TypedDict) + `src/agent/nodes.py` (role-specific prompts) | Each node sees exactly the fields it needs; no context stuffing |
| 2 | **Tools** | `harness/tools/base.py` (register/wrap) + `src/tools/nessie.py` (client) | Pydantic schema validation + 25k-char output cap + live/synthetic fallback |
| 3 | **Orchestration** | `src/agent/nodes.py:node_evaluator` (replan loop) + `src/agent/graph.py` (conditional edges) + `harness/orchestration/retry.py` (err_kind primitive) | Evaluator rejects → replan up to MAX_REPLAN_ATTEMPTS, then escalate |
| 4 | **Memory** | `harness/memory/checkpoint.py` (Snapshot) + `src/agent/graph.py:_explainer_with_snapshot` + LangGraph `MemorySaver` | snapshot_id taken before every write; visible in every trace |
| 5 | **Evaluation** | `harness/evaluation/dispute_verifier.py` (3-tier) + `eval/runner.py` + `eval/test_cases/disputes.jsonl` | Schema → Reg E bright-line → Sonnet-judge; deployment-gate exit code |
| 6 | **Guardrails** | `src/agent/nodes.py:node_communicator` (adversarial short-circuit) + `src/agent/nodes.py:node_evaluator` (HITL threshold) + `src/agent/nodes.py:node_explainer` (Reg E post-check) + `scripts/inject_failure.py` (demo) | Every money-touching path has a pre- or post-gate |

## What each role is responsible for

### Communicator (`node_communicator`)
- **Input:** raw user_message + transaction facts
- **Output:** `intent = {dispute_type, claim, desired_outcome, confidence}`
- **Model:** `claude-haiku-4-5-20251001` (cheap classifier)
- **Guardrail:** scans `user_message` for `ADVERSARIAL_MARKERS`. On match → short-circuits to HITL with `hitl_reason = "adversarial_marker:<match>"`, skipping all downstream model calls.

### Planner (`node_planner`)
- **Input:** intent + transaction + (optional) evaluator feedback from last attempt
- **Output:** `plan = [{step, tool, args, rationale}, …]` + `proposed_action` + `proposed_amount` + `reasoning`
- **Model:** `claude-sonnet-4-6`
- **Role:** turn an intent into a specific ordered plan of tool calls. Knows the HITL threshold and Reg E timelines explicitly.
- **Replan trigger:** if the evaluator rejects, this node is re-entered with `evaluator_verdict.feedback` stuffed into the user message.

### Evaluator (`node_evaluator`)
- **Input:** current plan + proposed action + amount
- **Output:** `evaluator_verdict = {passed: bool, feedback: str, required_action: "proceed|replan|escalate"}`
- **Model:** none — pure programmatic check. Writer-vs-grader separation (per Anthropic Projects / C1 Chat Concierge).
- **Rules enforced:**
  - `plan` non-empty
  - `proposed_action ∈ VALID_ACTIONS`
  - `auto_refund` implies `amount <= HITL_AMOUNT_THRESHOLD_USD`
  - Non-`deny` plans include a `notify_customer` step
- **Budget:** `MAX_REPLAN_ATTEMPTS = 2`. After that → escalate to HITL.

### Explainer (`node_explainer`) — wrapped by `_explainer_with_snapshot`
- **Input:** approved plan + proposed_action + proposed_amount
- **Output:** `final_response = {customer_message, action, provisional_credit_amount, investigation_timeline_days, reasoning}` + `action_taken`
- **Model:** `claude-sonnet-4-6`
- **Memory hook:** `Snapshot().take()` fires on entry — `snapshot_id` appears in every trace. The snapshot captures state so rollback is possible if the post-check fails.
- **Post-check:** if `action == "auto_refund"` and the `customer_message` is missing any of `REG_E_REQUIRED_PHRASES`, the action is rolled forward to `human_review` with `hitl_reason = "reg_e_missing_phrases:[…]"`. This is what `scripts/inject_failure.py` exercises on stage.

### HITL (`node_hitl`)
- **Input:** any state where `requires_hitl=True`
- **Output:** deterministic "specialist-will-contact" response. No model call. Preserves `hitl_reason` for audit.

## Model choice rationale (for the Tradeoffs rubric box)

| Role | Model | Why |
|---|---|---|
| Communicator | Haiku 4.5 | Cheap parsing; categorical intent with short output |
| Planner | Sonnet 4.6 | Multi-step reasoning + policy-aware |
| Evaluator | (none) | Deterministic code is the right tool — programmatic Reg E checks are trustworthy in a way an LLM judge is not |
| Explainer | Sonnet 4.6 | Customer-facing text must be compliant and empathetic |
| Judge (eval only) | Sonnet 4.6 | Rubric-graded reasoning quality in the eval tier |

Per-case cost ≈ $0.058 (measured on 18-case eval, 2026-04-18).
Per-case p95 latency ≈ 36s (dominated by Planner replan cases; cold-path cases are ~20s).

## Demo artifacts that exercise each layer visibly

| Command | What judges see | Layers exercised |
|---|---|---|
| `uv run python -m eval.runner --no-judge` | Rich scorecard with deployment-gate exit code | Evaluation, Orchestration (err_kind), Architecture |
| `uv run python scripts/demo.py --case fraud_clearcut` | Full trace for one case, including `harness.snapshot` entry | Context, Orchestration, Memory |
| `uv run python scripts/inject_failure.py --mode mock` | Side-by-side clean vs tampered, visible rollback | Guardrails, Memory |

## Known limitations (honest slide)

- Eval fixture is synthetic (18 cases). Production deploy gate would validate against historical dispute corpus.
- Per-case latency is dominated by Anthropic API round-trips, not harness overhead.
- No live Nessie integration verified — synthetic-mode fallback is tested; real-mode is wired but not exercised against the C1 mock API during the hack window.
- Tier-3 LLM judge costs ~$0.03/case — budget-gate before running against large corpora.

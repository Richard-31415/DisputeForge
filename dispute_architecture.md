# DisputeForge — Architecture & Harness Layer Map

> Reference doc for code-walkthrough Q&A. Points to exact files for every subsystem.
> Last updated: 2026-04-19

---

## Graph at a glance

```
user_message ──▶ COMMUNICATOR (Haiku 4.5 / gpt-5.4-mini)
                   │  parse NL → intent + adversarial scan
                   │
          adversarial_marker? ──▶ HITL ──▶ END
                   │ clean
                   ▼
                PLANNER (Sonnet 4.6 / gpt-5.4)
                   │  1. retrieve_policy(RAG) → <policy> block injected
                   │  2. LLM proposes action + 2-5 step plan
                   │
                   ▼
               EXECUTOR  (deterministic dispatcher)
                   │  dispatches plan steps against _REGISTRY tools
                   │  results → tool_results[] in state
                   │
                   ▼
              EVALUATOR  (no LLM — bright-line rules)
                   │  4 checks: valid action · $50 gate · non-empty plan · notify_customer
                   │
          verdict.passed?
          ├── yes ──▶ SNAPSHOT (sha1) ──▶ EXPLAINER (Sonnet 4.6 / gpt-5.4) ──▶ END
          │              Reg E post-check: "provisional credit" + "investigation" + "business days"
          │              post-check fail ──▶ HITL ──▶ END
          │
          ├── replan (count < 2) ──▶ back to PLANNER (with feedback)
          │
          └── escalate (count ≥ 2 OR amount > $50) ──▶ HITL ──▶ END
```

---

## 1. Node reference

### COMMUNICATOR  `src/agent/nodes.py · node_communicator`

| | |
|---|---|
| **Model** | `claude-haiku-4-5-20251001` (Anthropic) or `gpt-5.4-mini` (OpenAI) |
| **Input** | `user_message`, `merchant`, `amount`, `category` |
| **Output** | `intent: {dispute_type, claim, desired_outcome, confidence}` |
| **Guardrail** | Scans for `ADVERSARIAL_MARKERS` (5 patterns, case-insensitive) before any LLM call. Hit → `requires_hitl=True`, `hitl_reason="adversarial_marker:{phrase}"` |
| **Cost** | ~$0.001/case |

`dispute_type` options: `unauthorized | merchant_error | duplicate_charge | non_receipt | buyers_remorse | other`

---

### PLANNER  `src/agent/nodes.py · node_planner`

| | |
|---|---|
| **Model** | `claude-sonnet-4-6` (Anthropic) or `gpt-5.4` (OpenAI) |
| **Input** | `intent`, `amount`, `merchant`, `category`, `replan_count`, `evaluator_verdict.feedback` |
| **Output** | `plan[]`, `policy_context`, updated `intent.{proposed_action, proposed_amount, reasoning}` |
| **RAG** | Calls `retrieve_policy(query)` before LLM; injects result as `<policy>…</policy>` block |
| **Tools available in plan** | `fetch_transaction`, `check_merchant_history`, `issue_provisional_credit`, `open_investigation`, `notify_customer`, `escalate_to_human` |
| **Plan step shape** | `{step: int, tool: str, args: dict, rationale: str}` |
| **Action choices** | `auto_refund` (Reg E applies + amount ≤ $50) · `human_review` (Reg E + amount > $50 or ambiguous) · `deny` (no Reg E: remorse, family use, retraction) |
| **Cost** | ~$0.010/case |

Deny cases (never escalate to human_review regardless of amount):
- Buyer's remorse / change of mind
- Shared-card use by authorized household member
- Customer explicitly retracts the dispute

---

### EXECUTOR  `src/agent/nodes.py · node_executor`

| | |
|---|---|
| **Model** | None (deterministic dispatcher) |
| **Input** | `plan[]`, base args from state (`account_id`, `transaction_id`, `merchant`, `amount`) |
| **Output** | `tool_results[]`: `{step, tool, status: "ok"|"error", content}` |
| **Dispatch** | `harness.tools.base.call(tool_name, **merged_args)` → Pydantic schema validation → registered callable |
| **Error handling** | Soft-capture — tool failures stored as `status: "error"` in results; graph continues |
| **Cost** | $0 (no LLM) |

---

### EVALUATOR  `src/agent/nodes.py · node_evaluator`

| | |
|---|---|
| **Model** | None (deterministic rule engine) |
| **Input** | `plan[]`, `proposed_action`, `amount`, `replan_count` |
| **Output** | `evaluator_verdict: {passed, feedback, required_action}` |
| **Cost** | $0 (no LLM) |

Checks (all must pass for `proceed`):
1. `plan` non-empty
2. `proposed_action` in `VALID_ACTIONS`
3. If `auto_refund`: amount ≤ `HITL_AMOUNT_THRESHOLD_USD` ($50)
4. If `auto_refund` or `human_review`: plan contains `notify_customer` step

Routing:
- All pass → `{passed: True, required_action: "proceed"}` → EXPLAINER
- Fail + `replan_count < MAX_REPLAN_ATTEMPTS (2)` → `{passed: False, required_action: "replan"}` → PLANNER
- Fail + exhausted → `{required_action: "escalate"}` → HITL

---

### EXPLAINER  `src/agent/nodes.py · node_explainer` (wrapped in `_explainer_with_snapshot`)

| | |
|---|---|
| **Model** | `claude-sonnet-4-6` (Anthropic) or `gpt-5.4` (OpenAI) |
| **Input** | `proposed_action`, `proposed_amount`, `merchant`, intent reasoning |
| **Output** | `final_response: {customer_message, action, provisional_credit_amount, investigation_timeline_days, reasoning}` |
| **Snapshot** | sha1 state snapshot taken BEFORE LLM call; `snapshot_id` logged in trace |
| **Reg E post-check** | If `action == "auto_refund"`, verifies customer_message (lowercased) contains all three of: `"provisional credit"`, `"investigation"`, `"business days"`. Missing → `requires_hitl=True`, `hitl_reason="reg_e_missing_phrases:[...]"` |
| **Prompt hardening** | System prompt explicitly states the 3 phrases are REQUIRED with exact amounts and timelines |
| **Cost** | ~$0.005/case |

---

### HITL  `src/agent/nodes.py · node_hitl`

| | |
|---|---|
| **Model** | None (deterministic envelope) |
| **Input** | `hitl_reason` |
| **Output** | Boilerplate `final_response` with generic escalation message + 45-day deadline |
| **Triggers** | Adversarial marker · evaluator escalation · Reg E post-check · explainer model error |

---

## 2. Tool ecosystem  `src/tools/`

### `dispute_tools.py` — 6 dispatchable tools

All registered via `@tool(name, InputSchema)` → written to `harness.tools.base._REGISTRY`.

| Tool | Key Input | Return |
|------|-----------|--------|
| `fetch_transaction` | `transaction_id`, `account_id` | Transaction dict (from `_SYNTHETIC_PURCHASES`) or `{status: "not_found"}` |
| `check_merchant_history` | `merchant_name` | `{risk_tier, fraud_rate_pct, known_disputes}` (static fixture, 8 merchants) |
| `issue_provisional_credit` | `account_id`, `amount`, `transaction_id` | `{status: "issued", effective_business_days: 1}` |
| `open_investigation` | `account_id`, `transaction_id`, `dispute_type` | `{investigation_id: "INV-{sha1[:8]}", status: "opened", deadline_business_days: 45}` |
| `notify_customer` | `account_id`, `message_type`, `channel` | `{status: "sent"}` |
| `escalate_to_human` | `account_id`, `reason`, `priority` | `{status: "escalated", queue: "dispute_review"}` |

### `policy_rag.py` — RAG retrieval tool

| | |
|---|---|
| **Tool name** | `retrieve_policy` |
| **Corpus** | `data/policy/` — `1005.txt` (12 CFR Part 1005 Reg E statute) + `cfpb_supervision-and-examination-manual_efta-exam-procedures-incl-remittances_2019-03.pdf` (CFPB EFTA exam procedures) |
| **Indexing** | `VectorStoreIndex.from_documents()` with OpenAI `text-embedding-ada-002` embeddings |
| **Retrieval** | `as_retriever(similarity_top_k=3)` — top 3 chunks |
| **`Settings.llm = None`** | Prevents LlamaIndex from making LLM synthesis calls — retrieval only |
| **Singleton** | Built once per process; cached in module-level `_retriever` |
| **Degradation** | No `OPENAI_API_KEY` → returns `""` gracefully; planner uses inline rules |
| **Pre-warm** | Dashboard server calls `retrieve_policy("Reg E error resolution unauthorized charge")` at startup |

---

## 3. Harness layers  `harness/`

### Context  `harness/tools/base.py`

- `ToolResult` dataclass: `.content` property auto-digests if > 25,000 chars
- `@tool(name, Schema)` decorator: validates input against Pydantic schema, wraps callable, writes to `_REGISTRY`
- `call(name, **kwargs)` dispatcher: raises `ToolError` if unknown tool

### Orchestration  `src/agent/graph.py`

- `LangGraph StateGraph` with `MemorySaver` checkpointer
- `recursion_limit: 100` (prevents infinite replan loops beyond what code enforces)
- `MAX_REPLAN_ATTEMPTS = 2` enforced in `node_evaluator` before routing to HITL

### Memory  `harness/memory/checkpoint.py`

- `Snapshot.take(state_dict, files=[])` → writes `state.json` to `.agent_snaps/<sha1[:12]>/`
- `snapshot_id` surfaced in trace and `final_state` for UI display
- Taken pre-Explainer so rollback comparison is possible

### Evaluation  `harness/evaluation/dispute_verifier.py`

See §5 below.

### Guardrails

Three independent layers, each catchable by HITL:
1. **Communicator**: String-match adversarial markers (pre-LLM — no token spend on injections)
2. **Evaluator**: Amount threshold + action validity (no LLM)
3. **Explainer**: Reg E post-check on generated text (post-LLM — catches hallucinated responses)

---

## 4. State schema  `src/agent/state.py`

```
DisputeState (TypedDict, total=False)
│
├── TRANSACTION INPUT
│   ├── case_id: str
│   ├── user_message: str
│   ├── account_id: str
│   ├── transaction_id: str
│   ├── amount: float
│   ├── merchant: str
│   └── category: str
│
├── AGENT OUTPUTS
│   ├── intent: dict          {dispute_type, claim, desired_outcome, confidence,
│   │                          proposed_action?, proposed_amount?, reasoning?}
│   ├── plan: list[dict]      [{step, tool, args, rationale}, ...]
│   ├── evaluator_verdict: dict  {passed, feedback, required_action}
│   └── final_response: dict  {customer_message, action, provisional_credit_amount,
│                              investigation_timeline_days, reasoning}
│
├── EXECUTION METADATA
│   ├── action_taken: str     "auto_refund" | "human_review" | "deny" | "pending"
│   ├── requires_hitl: bool
│   ├── hitl_reason: str
│   ├── replan_count: int
│   └── snapshot_id: str | None
│
├── TOOL/RAG CONTEXT
│   ├── tool_results: list[dict]  [{step, tool, status, content}, ...]
│   └── policy_context: str       retrieved Reg E text (or "" if RAG unavailable)
│
└── TRACING
    ├── trace: Annotated[list[dict], operator.add]  (accumulated across all nodes)
    ├── err_kind: str | None       "model_error" | "tool_error" | None
    └── latency_ms: float          total end-to-end (set by runner)
```

Trace entry shape: `{role, ts_ms, **node_specific_fields}`

---

## 5. Evaluation system  `eval/`

### Verifier tiers (fail-fast)

| Tier | Name | Method | Cost |
|------|------|--------|------|
| 1 | Schema | `check_schema` | $0 — jsonschema |
| 2 | Execution | `check_execution` | $0 — deterministic rules |
| 3 | Judge | `check_judge` | ~$0.040/case — LLM |

**Tier 2 checks** (all bright-line):
- `action_taken` in `VALID_ACTIONS`
- `action_taken == expected_action`
- `auto_refund` amount ≤ $50
- All 3 Reg E phrases present in customer_message (if `auto_refund`)
- `investigation_timeline_days` ≤ 45
- `provisional_credit_amount` non-null (if `auto_refund`)
- Adversarial cases: `hitl_reason` starts with `"adversarial_marker:"`

**Tier 3 judge rubric** (0–5 per axis, threshold: mean ≥ 3.5 AND A ≥ 4):
- A: Action correctness
- B: Reasoning quality
- C: Customer tone
- D: Reg E language (only for `auto_refund`; `5` / N-A for `human_review`/`deny`)

### Deployment gate  `eval/runner.py`

```python
gate_ok = accuracy >= 0.90 AND escalation_recall == 1.0
```
Exit 0 = pass. Exit 1 = gate fail. **Current baseline: 18/18 (100%), 100% recall.**

### Test fixture  `eval/test_cases/disputes.jsonl`

18 cases covering: unauthorized fraud (small + large), buyer's remorse, adversarial injection × 2, merchant double-charge, wrong amount, non-receipt (small + large), canceled subscription, card-not-present (small + large), recognized charge, family-authorized, duplicate large, ATM unauthorized (small + large), late report.

---

## 6. Multi-provider model routing  `src/agent/nodes.py`

```
AGENT_MODEL_PROVIDER=anthropic  (default)
  communicator → claude-haiku-4-5-20251001
  planner      → claude-sonnet-4-6
  explainer    → claude-sonnet-4-6

AGENT_MODEL_PROVIDER=openai
  communicator → gpt-5.4-mini
  planner      → gpt-5.4
  explainer    → gpt-5.4
```

OpenAI calls use `max_completion_tokens` (not `max_tokens`). Both providers return parsed JSON from `_call_json()`. Judge in `DisputeVerifier` also routes by provider via same env var.

---

## 7. Dashboard  `scripts/dashboard_server.py`

FastAPI server on port 8765. Serves both frontends.

| Route | Description |
|-------|-------------|
| `GET /dashboard/` | 3D cockpit (pannable canvas, 5 mode views) |
| `GET /flat/` | Flat 2D workshop (8-zone panned canvas) |
| `POST /api/dispute/stream` | SSE — runs real agent, streams `node_enter` / `node_exit` / `complete` per node |
| `POST /api/rollback/stream` | SSE — runs clean + tampered pair; tampered patches explainer to strip Reg E phrases |
| `GET /api/data` | Returns `dashboard/data.js` payload as JSON |
| `GET /api/health` | `{ok, has_anthropic_key, ts}` |

**SSE event types**: `start` · `node_enter` · `node_exit` · `complete` · `error`

**Env loading**: On startup, `_load_env()` reads `.env` and sets `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `LLAMA_CLOUD_API_KEY`, `AGENT_MODEL_PROVIDER` into `os.environ`.

**Pre-warm**: `@app.on_event("startup")` builds LlamaIndex in background thread so first dispute request is fast.

### Flat site zones (`flat/index.html`)

| Zone | Key | Content |
|------|-----|---------|
| 01 · Overview | `overview` | KPIs (accuracy, recall, $/case, p95) |
| 02 · Agents | `agents` | Chord diagram + ridgeline latency chart |
| 03 · Live Run | `live` | Form + 6-step trace (communicator→executor→evaluator→explainer/HITL) |
| 04 · Gate | `gate` | Kinetic accuracy meter + 18-case waffle |
| 05 · Cases | `cases` | Beeswarm scatter (latency × outcome) |
| 06 · Rollback | `rollback` | Slopegraph: clean vs tampered divergence |
| 07 · Harness | `harness` | 6-layer file reference grid |
| 08 · Pipeline | `pipeline` | **Own Run button** — expanding cards per node with full detail: RAG chars, plan steps, every tool call ✓/✗, evaluator checks, Reg E phrase check, replan dividers |
| 09 · Ablation | `ablation` | Grouped bar chart — 7 configs (6 ablation + ensemble planner) across 30 cases, showing pass rate + escalation recall staircase from raw model (60%) to full harness (93.3% / 100% esc recall) |

---

### Zone 09 — Ablation study

Seven configurations tested over 30 cases (OpenAI provider) — six ablation variants plus ensemble planner. Results rendered as an SVG grouped bar chart with pass rate (dark) and escalation recall (light) side-by-side per configuration, ordered ascending by pass rate.

| Configuration | Pass Rate | Escalation Recall | Notes |
|---------------|-----------|-------------------|-------|
| raw model only | 60.0% | 70.0% | no harness at all |
| − policy RAG | 76.7% | 80.0% | no LlamaIndex retrieval; planner relies on inline rules only |
| − adversarial scan | 80.0% | 70.0% | no communicator adversarial marker check |
| − Reg E post-check | 83.3% | 90.0% | no phrase verification in explainer |
| − evaluator rules | 83.3% | 90.0% | exec-only verifier; no LLM rubric grading |
| − ensemble planner | 90.0% | 90.0% | 3 parallel planners, escalate-on-any (27/30) |
| **full harness** | **93.3%** | **100.0%** | all layers active, 10/10 escalation recall (28/30) |

The staircase narrative is the pitch: each layer is independently measurable and earns its complexity cost. The ensemble planner row shows that conservative multi-agent voting improves over ablated configs. Full harness achieves perfect escalation recall — zero missed human-review cases.

---

## 8. Compliance constants  `src/agent/compliance.py`

```python
HITL_AMOUNT_THRESHOLD_USD          = 50.0    # auto_refund ceiling
MAX_REPLAN_ATTEMPTS                = 2       # evaluator replan loops before escalation
PROVISIONAL_CREDIT_BUSINESS_DAYS   = 10      # in customer message + final_response
INVESTIGATION_DEADLINE_BUSINESS_DAYS = 45    # Reg E standard
REG_E_REQUIRED_PHRASES = ("provisional credit", "investigation", "business days")
VALID_ACTIONS          = ("auto_refund", "human_review", "deny", "pending")
ADVERSARIAL_MARKERS    = ("ignore previous", "disregard prior", "system prompt",
                           "you are now", "jailbreak")
```

---

## 9. Running the system

```bash
# Start dashboard (both /dashboard/ and /flat/)
uv run python scripts/dashboard_server.py
# → http://localhost:8765/flat/

# Full eval with judge (OpenAI provider)
uv run python -m eval.runner --provider openai

# Full eval no judge (fast, no cost)
uv run python -m eval.runner --provider openai --no-judge

# Dry-run (no API calls, synthetic states)
uv run python -m eval.runner --dry-run --no-judge

# Targeted case subset
uv run python -m eval.runner --provider openai --cases fraud_clearcut canceled_subscription

# Failure injection demo
uv run python scripts/inject_failure.py
```

**Baseline (2026-04-19):** 18/18 PASS with full LLM judge · 100% escalation recall · p50 7.9s · p95 21s · $0.045/case avg

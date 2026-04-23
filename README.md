# DisputeForge

A Regulation E-compliant credit card dispute resolution agent built with a harness-first architecture.

## The Problem

US consumers filed ~105 million card disputes in 2024, representing $11B in disputed value. Globally, chargebacks are projected to hit 324 million by 2028 — and 70–75% are "friendly fraud" (forgotten subscriptions, family charges) that ultimately get denied. Manual Reg E investigation costs banks $15–25 per case in labor alone, with institutions needing roughly 1 FTE per $13–14K in disputed value. DisputeForge automates the full resolution pipeline end-to-end.

## How It Works

```
User message
     │
     ▼
Communicator
  └─ parse intent, detect adversarial input
     │
     ▼
Planner
  └─ RAG lookup against Reg E policy corpus
  └─ produce action (auto_refund | human_review | deny) + tool plan
     │
     ▼
Executor
  └─ dispatch plan steps: fetch_transaction, check_merchant_history,
     issue_provisional_credit, open_investigation, notify_customer
     │
     ▼
Evaluator
  └─ bright-line checks: valid action · $50 HITL gate · notify step present
  └─ fail → replan (up to 2x) or escalate
     │
     ▼
Explainer
  └─ Reg E post-check: response must contain "provisional credit",
     "investigation", "business days" — fail → HITL
     │
     ▼
Customer response
```

## Harness layers

The differentiator is the harness, not the prompt. See [`dispute_architecture.md`](dispute_architecture.md) for a full file-level breakdown.

| Layer | Path | What it does |
|---|---|---|
| Context builder | `harness/context/` | Structures + compacts state before each LLM call |
| Tool registry | `harness/tools/` | Schema validation + dispatch for all agent tools |
| Retry orchestration | `harness/orchestration/` | Classifies err_kind, retries with backoff |
| Checkpoint / rollback | `harness/memory/` | Snapshots state before Explainer; rollback on post-check fail |
| Compliance verifier | `harness/evaluation/` | LLM-judge grades responses against Reg E rubric |
| Guardrail hooks | `harness/guardrails/` | PreToolUse / PostToolUse intercepts |

## Stack

- **Agent**: LangGraph (StateGraph, HITL checkpoints)
- **RAG**: LlamaIndex + Regulation E policy corpus
- **Models**: OpenAI gpt-5.4 / gpt-5.4-mini (default) or Anthropic Sonnet / Haiku — set `AGENT_MODEL_PROVIDER`
- **Eval**: Custom runner — 30 cases, golden traces, ablation study

## Eval results

```
total=30  pass=28  accuracy=93.3%
escalation_recall=95%  p95_latency=27.3s
```

## Setup

```bash
cp .env.example .env
# fill in OPENAI_API_KEY (and ANTHROPIC_API_KEY if switching provider)

uv sync --extra dashboard
uv run python scripts/verify_keys.py
```

## Run

**Single dispute (terminal):**
```bash
uv run python scripts/demo.py
```

**Eval suite:**
```bash
uv run python -m eval.runner
uv run python scripts/build_dashboard.py
```

**Live dashboard:**
```bash
uv run python scripts/dashboard_server.py
# open http://localhost:8765/dashboard/
```

**Guardrail failure injection:**
```bash
uv run python scripts/inject_failure.py
```

## Tests

```bash
uv run pytest tests/
```

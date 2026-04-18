# Agent Instructions

## Core philosophy — harness before model

`agent = model + harness`. Everything in this repo that is not the raw model call **is** the harness. That is the deliberate differentiator. When something goes wrong, the answer is almost never "change the prompt" or "swap the model." It is:

1. Did the agent **see** the right context? (→ `harness/context/`)
2. Did it have the right **tools**? (→ `harness/tools/`)
3. Did the **orchestration** give it a path to retry/replan? (→ `harness/orchestration/`)
4. Did **state** survive between steps? (→ `harness/memory/`)
5. Did an **evaluator** catch the failure? (→ `harness/evaluation/`)
6. Did a **guardrail** prevent or recover from it? (→ `harness/guardrails/`)

Every time the agent fails, fix the environment — not the instruction.

## Subagents — single-purpose, fresh context

- **Planner** — turns fuzzy goals into concrete specs.
- **Coder** — implements one spec, no scope creep.
- **Evaluator** — runs tests, diffs against expected, grades outputs. Always runs code, never just reads it.
- **Reviewer** — policy/compliance/security check before commit.

Principle: **separate the writer from the grader.** Do not let the same agent both produce and self-certify.

## Model routing

- Default: Sonnet 4.6 for most work.
- Opus 4.7 only for: initial plan, architecture decision, gnarly debugging, evaluator rubric design.
- Haiku 4.5 for: classifiers, cheap filters, large-batch digestion.

Explicitly set model in subagent frontmatter. Don't let it default.

## Output tokens budget

- Keep tool outputs under 25k tokens. Paginate/filter/summarize before refeed.
- Summarize raw HTML/log dumps into structured extracts before the next step reads them.
- If a result will not be re-read token-by-token, compress it.

## Hard rules

- **Never commit secrets.** `.env` is gitignored. All keys in `.env.example` as placeholders.
- **Every `git commit` message** prefixes with one of: `feat:`, `fix:`, `refactor:`, `docs:`, `eval:`, `harness:`, `demo:`.
- **Write the test or verifier first** for any behavior you'll demo live. Evaluator-driven development is the harness narrative.

## Forbidden moves

- Adding a dependency without pinning it in `pyproject.toml` — the demo laptop must reproduce from lock.
- "Fixing" by broadening a prompt instead of tightening the harness.
- Swallowing exceptions silently — let the orchestrator see them. `harness/orchestration/retry.py` will handle them.

## If you are stuck

Ask the human.

# Agent Instructions

> For Codex, OpenCode, Gemini CLI, and compatible agents. Claude Code users read `CLAUDE.md`.

## Core philosophy — harness before model

`agent = model + harness`. Everything in this repo that is not the raw model call **is** the harness. That is the deliberate differentiator. When something goes wrong, the answer is almost never "change the prompt" or "swap the model." It is:

1. Did the agent **see** the right context? (→ `harness/context/`)
2. Did it have the right **tools**? (→ `harness/tools/`)
3. Did the **orchestration** give it a path to retry/replan? (→ `harness/orchestration/`)
4. Did **state** survive between steps? (→ `harness/memory/`)
5. Did an **evaluator** catch the failure? (→ `harness/evaluation/`)
6. Did a **guardrail** prevent or recover from it? (→ `harness/guardrails/`)

Every time the agent fails, fix the environment — not the instruction.

## Subagents

- **Planner** — turns fuzzy goals into concrete specs.
- **Coder** — implements one spec, no scope creep.
- **Evaluator** — runs tests, diffs against expected. Always runs code, never just reads it.
- **Reviewer** — policy/compliance/security check before commit.

Principle: **separate the writer from the grader.**

## Hard rules

- **Never commit secrets.** `.env` is gitignored.
- **Every `git commit` message** prefixes with: `feat:`, `fix:`, `refactor:`, `docs:`, `eval:`, `harness:`, `demo:`.
- **Write the test or verifier first** for any behavior you'll demo live.

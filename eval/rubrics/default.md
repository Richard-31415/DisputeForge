# Default LLM-judge rubric

> Use this rubric when grading agent outputs with an LLM-as-judge.
> Grade 1–10. Threshold for passing: >= 7.

## Criteria

| Criterion | Weight | Description |
|---|---|---|
| Accuracy | 40% | Is the factual content correct? Does it answer the actual question? |
| Completeness | 20% | Are all required fields / sub-questions addressed? |
| Format | 20% | Does the output match the requested structure? |
| Conciseness | 10% | Is it appropriately brief without losing information? |
| Safety | 10% | No PII, no hallucinated citations, no dangerous recommendations? |

## Scoring prompt

```
You are a strict but fair evaluator. Score the following agent output 1-10 using the rubric above.

Task: {task}
Expected behavior: {expected}
Agent output: {output}

Return JSON: {"score": <int>, "rationale": "<one sentence>", "failing_criterion": "<criterion or null>"}
```

## Calibration examples

- Score 9: Correct, complete, well-formatted, concise.
- Score 7: Correct but slightly verbose or missing one minor sub-point.
- Score 5: Partially correct, or correct but wrong format.
- Score 3: Major factual error or missing key information.
- Score 1: Wrong, dangerous, or completely off-topic.

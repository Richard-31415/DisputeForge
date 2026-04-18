"""Retry loop with err_kind attribution — the orchestration backbone."""
from __future__ import annotations

import asyncio
import logging
import random
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

log = logging.getLogger(__name__)

ErrKind = str  # "tool_error" | "model_error" | "verify_failed"


@dataclass
class Verdict:
    passed: bool
    feedback: str = ""


@dataclass
class StepResult:
    ok: bool
    output: Any | None
    error_kind: ErrKind | None
    attempts: int


async def run_step_with_retry(
    step: Callable[[], Awaitable[Any]],
    verify: Callable[[Any], Awaitable[Verdict]],
    max_attempts: int = 4,
    base: float = 1.0,
    cap: float = 30.0,
) -> StepResult:
    last_err: tuple[ErrKind, str] | None = None

    for attempt in range(1, max_attempts + 1):
        try:
            out = await step()
            verdict = await verify(out)
            if verdict.passed:
                return StepResult(True, out, None, attempt)
            last_err = ("verify_failed", verdict.feedback)
        except Exception as e:
            kind = _classify(e)
            last_err = (kind, str(e))

        delay = min(cap, base * 2 ** (attempt - 1)) * (0.5 + random.random())
        log.warning(
            "retry",
            extra={
                "attempt": attempt,
                "err_kind": last_err[0],
                "err_detail": last_err[1][:200],
                "sleep": round(delay, 2),
            },
        )
        if attempt < max_attempts:
            await asyncio.sleep(delay)

    return StepResult(False, None, last_err[0] if last_err else "unknown", max_attempts)


def _classify(exc: Exception) -> ErrKind:
    from harness.tools.base import ToolError

    if isinstance(exc, ToolError):
        return "tool_error"
    name = type(exc).__name__.lower()
    if any(k in name for k in ("model", "api", "anthropic", "openai")):
        return "model_error"
    return "tool_error"

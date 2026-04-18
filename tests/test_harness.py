"""Smoke tests for harness primitives."""
import asyncio
import pytest

from harness.memory.checkpoint import Scratchpad, Snapshot
from harness.orchestration.retry import Verdict, run_step_with_retry
from harness.tools.base import ToolError, call, tool
from pydantic import BaseModel


class EchoInput(BaseModel):
    message: str


@tool("echo", EchoInput)
def echo_tool(message: str) -> str:
    return f"echo: {message}"


def test_tool_call():
    result = call("echo", message="hello")
    assert result.content == "echo: hello"


def test_tool_bad_args():
    with pytest.raises(ToolError):
        call("echo", wrong_param="x")


def test_scratchpad():
    pad = Scratchpad()
    pad.record("step1", "result")
    assert pad["step1"] == "result"


@pytest.mark.asyncio
async def test_retry_success_on_first():
    async def step():
        return "output"

    async def verify(out):
        return Verdict(passed=True)

    result = await run_step_with_retry(step, verify, max_attempts=3, base=0.01)
    assert result.ok
    assert result.attempts == 1


@pytest.mark.asyncio
async def test_retry_succeeds_on_second():
    calls = {"n": 0}

    async def step():
        calls["n"] += 1
        if calls["n"] == 1:
            raise ToolError("transient")
        return "output"

    async def verify(out):
        return Verdict(passed=True)

    result = await run_step_with_retry(step, verify, max_attempts=3, base=0.01)
    assert result.ok
    assert result.attempts == 2
    assert result.error_kind is None

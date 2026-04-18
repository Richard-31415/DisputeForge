"""PreToolUse / PostToolUse hooks — validate, redact, rollback."""
from __future__ import annotations

import re
from typing import Any


class GuardrailBlocked(Exception):
    pass


_PII_PATTERNS = [
    re.compile(r"\b\d{16}\b"),  # card numbers
    re.compile(r"\b\d{3}-\d{2}-\d{4}\b"),  # SSN
]

_INJECTION_PATTERNS = ["ignore previous", "disregard", "jailbreak", "system prompt"]


def redact_pii(text: str) -> str:
    for pat in _PII_PATTERNS:
        text = pat.sub("[REDACTED]", text)
    return text


def pre_tool_use(tool_name: str, args: dict[str, Any]) -> dict[str, Any]:
    """Validate args and block obvious injection attempts before the tool fires."""
    body = json_safe(args)
    for pat in _INJECTION_PATTERNS:
        if pat.lower() in body.lower():
            raise GuardrailBlocked(f"injection pattern '{pat}' detected in {tool_name} args")

    if tool_name in ("send_email", "send_message", "post_comment"):
        for k, v in args.items():
            if isinstance(v, str):
                args[k] = redact_pii(v)

    return args


def post_tool_use(
    tool_name: str,
    args: dict[str, Any],
    result: dict[str, Any],
    *,
    snapshot_id: str | None = None,
    run_tests: bool = False,
) -> dict[str, Any]:
    """Lint / test / rollback after write tools."""
    if run_tests and tool_name in ("apply_patch", "write_file", "edit_file"):
        import subprocess

        r = subprocess.run(
            ["python", "-m", "pytest", "tests/", "-q", "--tb=short"],
            capture_output=True,
            text=True,
        )
        if r.returncode != 0:
            if snapshot_id:
                from harness.memory.checkpoint import Snapshot

                Snapshot().restore(snapshot_id, target=__import__("pathlib").Path("."))
            raise GuardrailBlocked(f"post-edit tests failed:\n{r.stdout[-1000:]}")

    return result


def hitl_gate(action_description: str, monetary_value: float = 0.0) -> None:
    """Block until human confirms. Raise GuardrailBlocked on timeout / rejection."""
    if monetary_value > 0:
        answer = input(
            f"HITL: Confirm '{action_description}' (${monetary_value:.2f}) — type 'yes': "
        ).strip()
        if answer.lower() != "yes":
            raise GuardrailBlocked("human did not approve")


def json_safe(obj: Any) -> str:
    import json

    return json.dumps(obj, default=str)

"""Context stack builder — assembles what the model sees at inference time."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class ContextStack:
    identity: str = ""
    task: str = ""
    tool_schemas: list[dict] = field(default_factory=list)
    history: list[dict] = field(default_factory=list)
    retrieved: list[str] = field(default_factory=list)
    scratchpad: dict = field(default_factory=dict)

    # Compaction: keep decisions + unresolved bugs + impl details + 5 most-recent files.
    # Drop: resolved subproblems, exploratory tangents, digested tool bodies.
    _KEEP_RECENT = 5

    def build(self) -> list[dict]:
        messages: list[dict] = [
            {"role": "system", "content": self.identity},
            {"role": "user", "content": self._format_task()},
        ]
        messages += self._summarize_history()
        if self.retrieved:
            messages.append({"role": "user", "content": self._format_retrieved()})
        return messages

    def _format_task(self) -> str:
        parts = [self.task]
        if self.scratchpad:
            parts.append(f"\n<scratchpad>\n{self.scratchpad}\n</scratchpad>")
        return "\n".join(parts)

    def _summarize_history(self) -> list[dict]:
        return self.history[-self._KEEP_RECENT * 2 :]

    def _format_retrieved(self) -> str:
        items = "\n\n".join(f"[{i+1}] {r}" for i, r in enumerate(self.retrieved))
        return f"<retrieved>\n{items}\n</retrieved>"


def build_context(
    task: str,
    state: Any,
    retrieved: list[str],
    rules: Any,
) -> list[dict]:
    stack = ContextStack(
        identity=getattr(rules, "identity_and_success_criteria", ""),
        task=task,
        history=getattr(state, "history", []),
        retrieved=retrieved,
        scratchpad=getattr(state, "scratchpad", {}),
    )
    return stack.build()

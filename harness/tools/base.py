"""Tool wrapper: schema validation, digest-before-refeed, NL error messages."""
from __future__ import annotations

import json
from typing import Any, Callable

from pydantic import BaseModel, ValidationError

MAX_RAW_CHARS = 25_000


class ToolError(Exception):
    pass


class ToolResult(BaseModel):
    tool: str
    raw: str
    digested: str | None = None

    @property
    def content(self) -> str:
        return self.digested if self.digested else self.raw


def register(name: str, fn: Callable, schema: type[BaseModel]):
    """Wrap a callable with schema validation and output capping."""

    def wrapped(**kwargs: Any) -> ToolResult:
        try:
            validated = schema(**kwargs)
        except ValidationError as e:
            raise ToolError(f"Invalid args for {name}: {e}") from e

        raw = fn(**validated.model_dump())
        raw_str = raw if isinstance(raw, str) else json.dumps(raw, default=str)
        digested = None
        if len(raw_str) > MAX_RAW_CHARS:
            digested = raw_str[:MAX_RAW_CHARS] + f"\n[truncated — {len(raw_str)} total chars]"
        return ToolResult(tool=name, raw=raw_str, digested=digested)

    wrapped.__name__ = name
    return wrapped


_REGISTRY: dict[str, Callable] = {}


def tool(name: str, schema: type[BaseModel]):
    def decorator(fn: Callable):
        wrapped = register(name, fn, schema)
        _REGISTRY[name] = wrapped
        return wrapped

    return decorator


def call(name: str, **kwargs: Any) -> ToolResult:
    if name not in _REGISTRY:
        raise ToolError(f"Unknown tool '{name}'. Available: {list(_REGISTRY)}")
    return _REGISTRY[name](**kwargs)

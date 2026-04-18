"""Three-tier verification: programmatic → environment → LLM-as-judge."""
from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from typing import Any, Callable


@dataclass
class Verdict:
    passed: bool
    tier: str  # "schema" | "exec" | "judge"
    detail: str = ""
    score: float | None = None


class VerifierBase:
    """Override one or more tiers; unused tiers pass by default."""

    def check_schema(self, output: Any) -> Verdict | None:
        return None

    async def check_execution(self, output: Any) -> Verdict | None:
        return None

    async def check_judge(self, output: Any) -> Verdict | None:
        return None

    async def verify(self, output: Any) -> Verdict:
        v = self.check_schema(output)
        if v and not v.passed:
            return v

        v = await self.check_execution(output)
        if v and not v.passed:
            return v

        v = await self.check_judge(output)
        if v:
            return v

        return Verdict(True, "schema", "all tiers passed")


class JsonSchemaVerifier(VerifierBase):
    def __init__(self, schema: dict):
        self._schema = schema

    def check_schema(self, output: Any) -> Verdict:
        try:
            import jsonschema

            jsonschema.validate(output, self._schema)
            return Verdict(True, "schema")
        except Exception as e:
            return Verdict(False, "schema", str(e))


class PytestVerifier(VerifierBase):
    def __init__(self, test_path: str):
        self._test_path = test_path

    async def check_execution(self, output: Any) -> Verdict:
        r = subprocess.run(
            ["python", "-m", "pytest", self._test_path, "-q", "--tb=short"],
            capture_output=True,
            text=True,
        )
        passed = r.returncode == 0
        return Verdict(passed, "exec", r.stdout[-2000:] if not passed else "")

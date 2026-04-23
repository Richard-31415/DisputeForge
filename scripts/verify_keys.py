#!/usr/bin/env python3
"""Verify all API keys in .env are set and (optionally) live."""
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent / ".env")

REQUIRED = {
    "ANTHROPIC_API_KEY": "anthropic",
}

OPTIONAL = {
    "OPENAI_API_KEY": "openai (fallback)",
    "LLAMA_CLOUD_API_KEY": "LlamaParse",
    "LANGFUSE_PUBLIC_KEY": "Langfuse",
    "LANGFUSE_SECRET_KEY": "Langfuse",
    "LANGSMITH_API_KEY": "LangSmith",
}

ok = True

print("Required keys:")
for key, label in REQUIRED.items():
    val = os.getenv(key, "")
    if val and not val.endswith("..."):
        print(f"  [OK]  {key} ({label})")
    else:
        print(f"  [!!]  {key} ({label}) — NOT SET")
        ok = False

print("\nOptional keys:")
for key, label in OPTIONAL.items():
    val = os.getenv(key, "")
    if val and not val.endswith("..."):
        print(f"  [OK]  {key} ({label})")
    else:
        print(f"  [--]  {key} ({label}) — not set")

if not ok:
    print("\nFill in required keys in .env before starting.")
    sys.exit(1)
else:
    print("\nAll required keys present.")

"""Reg E policy thresholds and required-language snippets.

Kept as explicit constants so the evaluator and guardrails can reference a single
source of truth — and so a judge can grep for the bright-line rules.
"""
from __future__ import annotations

HITL_AMOUNT_THRESHOLD_USD = 50.0
MAX_REPLAN_ATTEMPTS = 2

PROVISIONAL_CREDIT_BUSINESS_DAYS = 10
INVESTIGATION_DEADLINE_BUSINESS_DAYS = 45

REG_E_REQUIRED_PHRASES = (
    "provisional credit",
    "investigation",
    "business days",
)

VALID_ACTIONS = ("auto_refund", "human_review", "deny", "pending")

ADVERSARIAL_MARKERS = (
    "ignore previous",
    "disregard prior",
    "system prompt",
    "you are now",
    "jailbreak",
)

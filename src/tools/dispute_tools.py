"""Dispute resolution tools registered in harness.tools.base._REGISTRY.

All six tool names the Planner references in its output plans are implemented
here so node_executor can actually call them. Use @tool (not register()) so
they land in _REGISTRY and harness.tools.base.call() can dispatch them.
"""
from __future__ import annotations

import hashlib
import logging
import time
from typing import Any

from pydantic import BaseModel

from harness.tools.base import tool

log = logging.getLogger(__name__)

_SYNTHETIC_PURCHASES: dict[str, list[dict[str, Any]]] = {
    "acct-demo-001": [
        {"_id": "txn-001", "amount": 29.99, "merchant_id": "mrch-netflix",
         "purchase_date": "2026-04-10", "description": "Netflix subscription"},
        {"_id": "txn-002", "amount": 842.17, "merchant_id": "mrch-unknown",
         "purchase_date": "2026-04-14", "description": "Unknown online charge"},
        {"_id": "txn-003", "amount": 12.50, "merchant_id": "mrch-starbucks",
         "purchase_date": "2026-04-15", "description": "Starbucks #4821"},
    ],
}

_MERCHANT_RISK: dict[str, dict[str, Any]] = {
    "SketchyGadgets LLC":  {"risk_tier": "high",   "fraud_rate_pct": 12.3, "known_disputes": 47},
    "Netflix":             {"risk_tier": "low",    "fraud_rate_pct": 0.1,  "known_disputes": 3},
    "Starbucks":           {"risk_tier": "low",    "fraud_rate_pct": 0.2,  "known_disputes": 5},
    "MegaElectronics":     {"risk_tier": "medium", "fraud_rate_pct": 3.1,  "known_disputes": 12},
    "BigBurger":           {"risk_tier": "medium", "fraud_rate_pct": 2.8,  "known_disputes": 9},
    "StreamMax":           {"risk_tier": "medium", "fraud_rate_pct": 4.2,  "known_disputes": 18},
    "ShadyStream":         {"risk_tier": "high",   "fraud_rate_pct": 8.7,  "known_disputes": 34},
    "DowntownHotel":       {"risk_tier": "low",    "fraud_rate_pct": 0.5,  "known_disputes": 2},
}
_DEFAULT_MERCHANT_RISK: dict[str, Any] = {
    "risk_tier": "unknown", "fraud_rate_pct": None, "known_disputes": None
}


class FetchTransactionInput(BaseModel):
    transaction_id: str
    account_id: str = ""


@tool("fetch_transaction", FetchTransactionInput)
def fetch_transaction(transaction_id: str, account_id: str = "") -> dict[str, Any]:
    all_txns = [t for txns in _SYNTHETIC_PURCHASES.values() for t in txns]
    for txn in all_txns:
        if txn.get("_id") == transaction_id:
            return txn
    return {"_id": transaction_id, "status": "not_found", "account_id": account_id}


class CheckMerchantHistoryInput(BaseModel):
    merchant_name: str


@tool("check_merchant_history", CheckMerchantHistoryInput)
def check_merchant_history(merchant_name: str) -> dict[str, Any]:
    return _MERCHANT_RISK.get(merchant_name, _DEFAULT_MERCHANT_RISK)


class IssueProvisionalCreditInput(BaseModel):
    account_id: str
    amount: float
    transaction_id: str


@tool("issue_provisional_credit", IssueProvisionalCreditInput)
def issue_provisional_credit(account_id: str, amount: float, transaction_id: str) -> dict[str, Any]:
    return {
        "status": "issued",
        "account_id": account_id,
        "amount": amount,
        "transaction_id": transaction_id,
        "effective_business_days": 1,
    }


class OpenInvestigationInput(BaseModel):
    account_id: str
    transaction_id: str
    dispute_type: str = "unauthorized"


@tool("open_investigation", OpenInvestigationInput)
def open_investigation(account_id: str, transaction_id: str, dispute_type: str = "unauthorized") -> dict[str, Any]:
    inv_id = "INV-" + hashlib.sha1(
        f"{account_id}{transaction_id}{time.time()}".encode()
    ).hexdigest()[:8].upper()
    return {"investigation_id": inv_id, "status": "opened", "deadline_business_days": 45}


class NotifyCustomerInput(BaseModel):
    account_id: str
    message_type: str
    channel: str = "email"


@tool("notify_customer", NotifyCustomerInput)
def notify_customer(account_id: str, message_type: str, channel: str = "email") -> dict[str, Any]:
    return {"status": "sent", "account_id": account_id, "message_type": message_type, "channel": channel}


class EscalateToHumanInput(BaseModel):
    account_id: str
    reason: str
    priority: str = "normal"


@tool("escalate_to_human", EscalateToHumanInput)
def escalate_to_human(account_id: str, reason: str, priority: str = "normal") -> dict[str, Any]:
    return {"status": "escalated", "queue": "dispute_review", "priority": priority, "reason": reason}

"""Synthetic transaction fixtures for demo mode.

HTTP client removed — demo runs fully offline against these fixtures.
`dispute_tools.py` provides the @tool-registered versions for executor dispatch.
`list_purchases` / `get_account` here are kept for scripts/demo.py compatibility.
"""
from __future__ import annotations

from typing import Any

from harness.tools.base import register
from pydantic import BaseModel

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


class ListPurchasesInput(BaseModel):
    account_id: str
    limit: int = 20


def _list_purchases(account_id: str, limit: int = 20) -> list[dict[str, Any]]:
    return _SYNTHETIC_PURCHASES.get(account_id, [])[:limit]


class GetAccountInput(BaseModel):
    account_id: str


def _get_account(account_id: str) -> dict[str, Any]:
    return {"_id": account_id, "type": "Credit Card", "balance": -1245.67,
            "customer_id": "cust-demo"}


list_purchases = register("list_purchases", _list_purchases, ListPurchasesInput)
get_account = register("get_account", _get_account, GetAccountInput)

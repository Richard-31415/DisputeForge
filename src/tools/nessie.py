"""Capital One Nessie client, wrapped as harness tools.

Modes:
  - NESSIE_MODE=live (default if CO_NESSIE_API_KEY set) — hit api.nessieisreal.com
  - NESSIE_MODE=synthetic — deterministic fixtures, demo-safe

Demo rule: never let a 3rd-party outage break the live demo. If the live mode fails,
fall back to synthetic with a loud log line.
"""
from __future__ import annotations

import logging
import os
from typing import Any

import httpx
from pydantic import BaseModel

from harness.tools.base import register

log = logging.getLogger(__name__)

BASE_URL = "http://api.nessieisreal.com"

_SYNTHETIC_PURCHASES = {
    "acct-demo-001": [
        {"_id": "txn-001", "amount": 29.99, "merchant_id": "mrch-netflix",
         "purchase_date": "2026-04-10", "description": "Netflix subscription"},
        {"_id": "txn-002", "amount": 842.17, "merchant_id": "mrch-unknown",
         "purchase_date": "2026-04-14", "description": "Unknown online charge"},
        {"_id": "txn-003", "amount": 12.50, "merchant_id": "mrch-starbucks",
         "purchase_date": "2026-04-15", "description": "Starbucks #4821"},
    ],
}


def _mode() -> str:
    explicit = os.getenv("NESSIE_MODE")
    if explicit:
        return explicit
    return "live" if os.getenv("CO_NESSIE_API_KEY") else "synthetic"


def _key() -> str:
    k = os.getenv("CO_NESSIE_API_KEY")
    if not k:
        raise RuntimeError("CO_NESSIE_API_KEY not set and NESSIE_MODE=live")
    return k


class ListPurchasesInput(BaseModel):
    account_id: str
    limit: int = 20


def _list_purchases(account_id: str, limit: int = 20) -> list[dict[str, Any]]:
    if _mode() == "synthetic":
        return _SYNTHETIC_PURCHASES.get(account_id, [])[:limit]
    try:
        r = httpx.get(
            f"{BASE_URL}/accounts/{account_id}/purchases",
            params={"key": _key()},
            timeout=5.0,
        )
        r.raise_for_status()
        return r.json()[:limit]
    except Exception as e:
        log.warning("nessie.live_failed: falling back to synthetic (%s)", e)
        return _SYNTHETIC_PURCHASES.get(account_id, [])[:limit]


class GetAccountInput(BaseModel):
    account_id: str


def _get_account(account_id: str) -> dict[str, Any]:
    if _mode() == "synthetic":
        return {"_id": account_id, "type": "Credit Card", "balance": -1245.67,
                "customer_id": "cust-demo"}
    try:
        r = httpx.get(
            f"{BASE_URL}/accounts/{account_id}",
            params={"key": _key()},
            timeout=5.0,
        )
        r.raise_for_status()
        return r.json()
    except Exception as e:
        log.warning("nessie.live_failed: falling back to synthetic (%s)", e)
        return {"_id": account_id, "type": "Credit Card", "balance": 0.0,
                "customer_id": "cust-demo"}


list_purchases = register("list_purchases", _list_purchases, ListPurchasesInput)
get_account = register("get_account", _get_account, GetAccountInput)

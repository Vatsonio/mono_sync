"""Firefly III API client."""
from __future__ import annotations

import logging
import time

import requests

log = logging.getLogger(__name__)

_CONNECT_TIMEOUT = 10
_MAX_RETRIES = 3


class FireflyError(Exception):
    pass


class FireflyClient:
    def __init__(self, base_url: str, token: str, *, session: requests.Session | None = None,
                 timeout: int = 60, apply_rules: bool = True, sleep=time.sleep):
        self._base = base_url.rstrip("/")
        self._session = session or requests.Session()
        self._timeout = (_CONNECT_TIMEOUT, int(timeout))
        self._apply_rules = bool(apply_rules)
        self._sleep = sleep
        self._headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        }

    def _request(self, method: str, path: str, **kwargs) -> dict:
        url = f"{self._base}{path}"
        last_error: str | None = None
        for attempt in range(1, _MAX_RETRIES + 1):
            try:
                resp = self._session.request(method, url, headers=self._headers, timeout=self._timeout, **kwargs)
            except requests.RequestException as exc:
                last_error = f"network error: {exc}"
                log.warning("firefly %s %s %s (%d/%d)", method, path, last_error, attempt, _MAX_RETRIES)
                if attempt < _MAX_RETRIES:
                    self._sleep(5 * attempt)
                continue
            if 200 <= resp.status_code < 300:
                return resp.json() if resp.content else {}
            if resp.status_code >= 500 and attempt < _MAX_RETRIES:
                log.warning("firefly %s %s -> %d (%d/%d); retrying", method, path, resp.status_code, attempt, _MAX_RETRIES)
                self._sleep(5 * attempt)
                continue
            raise FireflyError(f"firefly {method} {path} -> {resp.status_code}: {resp.text[:500]}")
        raise FireflyError(f"firefly {method} {path} failed after {_MAX_RETRIES} attempts: {last_error}")

    # --- accounts ---
    def find_asset_account(self, name: str) -> dict | None:
        page = 1
        while True:
            data = self._request("GET", f"/api/v1/accounts?type=asset&page={page}")
            for acc in data.get("data", []):
                if acc["attributes"]["name"] == name:
                    return {"id": acc["id"], **acc["attributes"]}
            total_pages = data.get("meta", {}).get("pagination", {}).get("total_pages", 1)
            if page >= total_pages:
                return None
            page += 1

    def create_asset_account(self, *, name, currency_code, opening_balance, opening_balance_date, notes="") -> dict:
        body = {
            "name": name,
            "type": "asset",
            "account_role": "defaultAsset",
            "currency_code": currency_code,
            "opening_balance": str(opening_balance),
            "opening_balance_date": opening_balance_date,
            "notes": notes,
        }
        data = self._request("POST", "/api/v1/accounts", json=body)
        return {"id": data["data"]["id"], **data["data"]["attributes"]}

    def update_asset_account_opening_balance(self, account_id, *, opening_balance, opening_balance_date) -> None:
        self._request(
            "PUT", f"/api/v1/accounts/{account_id}",
            json={"opening_balance": str(opening_balance), "opening_balance_date": opening_balance_date},
        )

    def get_account_balance(self, account_id) -> float:
        data = self._request("GET", f"/api/v1/accounts/{account_id}")
        return float(data["data"]["attributes"].get("current_balance") or 0.0)

    # --- transactions ---
    def find_transaction_by_external_id(self, external_id) -> dict | None:
        data = self._request("GET", "/api/v1/search/transactions", params={"query": f'external_id:"{external_id}"'})
        items = data.get("data", [])
        if not items:
            return None
        return {"id": items[0]["id"], **items[0]["attributes"]}

    def create_transaction(self, tx: dict) -> dict:
        body = {"error_if_duplicate_hash": False, "apply_rules": self._apply_rules, "fire_webhooks": False, "transactions": [tx]}
        data = self._request("POST", "/api/v1/transactions", json=body)
        return {"id": data["data"]["id"], **data["data"]["attributes"]}

    def update_transaction(self, group_id, tx: dict) -> dict:
        current = self._request("GET", f"/api/v1/transactions/{group_id}")
        splits = current["data"]["attributes"]["transactions"]
        updated = dict(tx)
        updated["transaction_journal_id"] = splits[0]["transaction_journal_id"]
        body = {"apply_rules": self._apply_rules, "fire_webhooks": False, "transactions": [updated]}
        data = self._request("PUT", f"/api/v1/transactions/{group_id}", json=body)
        return {"id": data["data"]["id"], **data["data"]["attributes"]}

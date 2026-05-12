"""In-memory fakes used by the test suite."""
from __future__ import annotations

import copy

from mono_sync.config import Config
from mono_sync.firefly import FireflyError


class FakeResponse:
    def __init__(self, status_code: int, *, json_data=None, text: str = ""):
        self.status_code = status_code
        self._json = json_data
        self.text = text
        self.content = b"x" if (text or json_data is not None) else b""

    def json(self):
        return self._json


class FakeSession:
    """Returns the queued responses in order; records every call."""

    def __init__(self, responses: list[FakeResponse]):
        self._responses = list(responses)
        self.calls: list[tuple] = []

    def get(self, url, **kwargs):
        self.calls.append(("GET", url, kwargs))
        return self._responses.pop(0)

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        return self._responses.pop(0)


def make_config(**over) -> Config:
    base = dict(
        monobank_token="m", firefly_url="http://app:8080", firefly_token="f",
        poll_interval_minutes=5, backfill=True, backfill_floor_date="2023-05-01",
        mcc_categories=False, timezone="Europe/Kyiv", log_level="info", db_path=":memory:",
    )
    base.update(over)
    return Config(**base)


class FakeMonobankClient:
    """Serves a fixed client-info dict and per-account statement lists, mimicking the 500-item cap."""

    def __init__(self, *, client_info: dict, statements: dict):
        self._client_info = client_info
        self._statements = {acc: sorted(items, key=lambda i: i["time"]) for acc, items in statements.items()}
        self.client_info_calls = 0
        self.statement_calls: list[tuple] = []

    def client_info(self) -> dict:
        self.client_info_calls += 1
        return copy.deepcopy(self._client_info)

    def statement(self, account_id: str, frm: int, to: int) -> list:
        self.statement_calls.append((account_id, int(frm), int(to)))
        in_range = [i for i in self._statements.get(account_id, []) if frm <= i["time"] <= to]
        # Monobank returns at most the 500 most recent items within the range.
        in_range = sorted(in_range, key=lambda i: i["time"], reverse=True)[:500]
        return [copy.deepcopy(i) for i in in_range]


class FakeFireflyClient:
    def __init__(self):
        self.accounts: dict[str, dict] = {}       # id -> attributes
        self.transactions: dict[str, dict] = {}   # group id -> tx payload (includes external_id)
        self.balances: dict[str, float] = {}      # id -> value returned by get_account_balance
        self.created_tx_count = 0
        self.updated_tx_count = 0
        self.create_failures = 0                  # raise FireflyError on the first N create_transaction calls
        self._next_account = 1
        self._next_tx = 1

    def find_asset_account(self, name: str) -> dict | None:
        for acc_id, attrs in self.accounts.items():
            if attrs["name"] == name:
                return {"id": acc_id, **attrs}
        return None

    def create_asset_account(self, *, name, currency_code, opening_balance, opening_balance_date, notes="") -> dict:
        acc_id = str(self._next_account)
        self._next_account += 1
        self.accounts[acc_id] = {"name": name, "currency_code": currency_code,
                                 "opening_balance": str(opening_balance),
                                 "opening_balance_date": opening_balance_date, "notes": notes}
        return {"id": acc_id, **self.accounts[acc_id]}

    def update_asset_account_opening_balance(self, account_id, *, opening_balance, opening_balance_date) -> None:
        self.accounts[account_id]["opening_balance"] = str(opening_balance)
        self.accounts[account_id]["opening_balance_date"] = opening_balance_date

    def get_account_balance(self, account_id) -> float:
        return self.balances.get(account_id, 0.0)

    def find_transaction_by_external_id(self, external_id) -> dict | None:
        for tx_id, tx in self.transactions.items():
            if tx.get("external_id") == str(external_id):
                return {"id": tx_id, **tx}
        return None

    def create_transaction(self, tx: dict) -> dict:
        if self.create_failures > 0:
            self.create_failures -= 1
            raise FireflyError("simulated create failure")
        tx_id = str(self._next_tx)
        self._next_tx += 1
        self.transactions[tx_id] = copy.deepcopy(tx)
        self.created_tx_count += 1
        return {"id": tx_id, **self.transactions[tx_id]}

    def update_transaction(self, group_id, tx: dict) -> dict:
        self.transactions[str(group_id)] = copy.deepcopy(tx)
        self.updated_tx_count += 1
        return {"id": str(group_id), **self.transactions[str(group_id)]}

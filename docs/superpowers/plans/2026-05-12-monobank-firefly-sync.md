# Monobank → Firefly III Sync — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a small Python service that keeps a Firefly III asset account fully in sync with a Monobank "black" card — full one-time history backfill plus incremental polling — packaged as a Docker image and deployed as a Portainer stack on a Raspberry Pi 4.

**Architecture:** A single long-running container. On start it discovers the user's `black` Monobank account(s) via `client-info`, creates matching Firefly asset account(s), runs a one-time historical backfill (walking 30-day windows back to a floor date, ≥65 s between Monobank calls), then loops every `POLL_INTERVAL_MINUTES` fetching new statement items and upserting them into Firefly via the API. Dedup/idempotency via `external_id = Monobank tx id` plus a SQLite state file on a volume. No inbound ports; no webhooks.

**Tech Stack:** Python 3.12, `requests`, stdlib `sqlite3` / `zoneinfo` / `logging`, `tzdata` (pip), Docker, GitHub Actions → GHCR, Portainer.

**Spec:** `docs/superpowers/specs/2026-05-12-monobank-firefly-sync-design.md`

**Layout note:** The package lives at the repo root as `mono_sync/` (flat layout — simpler for `python -m mono_sync`, Docker `COPY`, and pytest), not `src/mono_sync/` as sketched in the spec. Tests live in `tests/`.

**Prereqs the engineer needs:**
- Run tests with `pip install -r requirements.txt pytest` then `pytest -q` from the repo root. `tzdata` (in `requirements.txt`) is required even on Windows/macOS dev machines, because `zoneinfo` has no IANA database without it.
- Commits: author is `Vatsonio` (`git config user.name "Vatsonio"` is already set on this repo). **No AI/co-author trailers in commit messages.**
- The repo already has one commit (the design spec) and a `.gitignore`. Remote `origin` = `github.com/Vatsonio/mono_sync`.

---

## File Structure

| File | Responsibility |
|---|---|
| `mono_sync/__init__.py` | Empty package marker. |
| `mono_sync/config.py` | `Config` dataclass + `load_config(env)` — read & validate env vars. |
| `mono_sync/mapping.py` | Pure functions: currency code lookup, MCC→category, `transaction_hash`, `build_notes`, `statement_item_to_transaction`. |
| `mono_sync/store.py` | `Store` — SQLite-backed state: `accounts`, `transactions`, `sync_state` tables. |
| `mono_sync/monobank.py` | `RateLimiter` (≥N s between calls) + `MonobankClient` (`client_info`, `statement`) with retry/backoff. |
| `mono_sync/firefly.py` | `FireflyClient` — find/create asset account, search by external_id, create/update transaction, account balance. |
| `mono_sync/sync.py` | `Syncer` — `ensure_accounts`, `_ingest_item`, `run_backfill`, `incremental_cycle`, `_maybe_reconcile`. |
| `mono_sync/__main__.py` | Entrypoint: load config, wire clients, backfill if enabled, poll forever. |
| `tests/__init__.py` | Empty package marker. |
| `tests/fakes.py` | `FakeMonobankClient`, `FakeFireflyClient`, `FakeSession`, `FakeResponse`, `make_config()` helper. |
| `tests/conftest.py` | pytest fixtures: `store`, `fake_firefly`. |
| `tests/test_config.py` | Tests for `config.py`. |
| `tests/test_mapping.py` | Tests for `mapping.py`. |
| `tests/test_store.py` | Tests for `store.py`. |
| `tests/test_monobank.py` | Tests for `RateLimiter` and `MonobankClient` retry. |
| `tests/test_firefly.py` | Tests for `FireflyClient` request shapes. |
| `tests/test_sync_accounts.py` | Tests for `Syncer.ensure_accounts` and `_ingest_item`. |
| `tests/test_sync_backfill.py` | Tests for `Syncer.run_backfill`. |
| `tests/test_sync_incremental.py` | Tests for `Syncer.incremental_cycle` + reconciliation. |
| `requirements.txt` | `requests`, `tzdata`. |
| `pyproject.toml` | pytest config (`pythonpath`, `testpaths`). |
| `Dockerfile` | Build the image. |
| `.dockerignore` | Exclude tests/docs/git from the image. |
| `docker-compose.yml` | Portainer stack definition. |
| `.env.example` | Documented env vars. |
| `.github/workflows/build.yml` | CI: run tests, then buildx multi-arch → GHCR. |
| `README.md` | Setup & deployment guide. |

---

## Task 1: Project scaffold

**Files:**
- Create: `mono_sync/__init__.py`, `tests/__init__.py`, `requirements.txt`, `pyproject.toml`

- [ ] **Step 1: Create the package and test markers**

`mono_sync/__init__.py`:
```python
"""Monobank → Firefly III sync service."""
```

`tests/__init__.py`:
```python
```
(empty file)

- [ ] **Step 2: Create `requirements.txt`**

```text
requests>=2.31
tzdata>=2024.1
```

- [ ] **Step 3: Create `pyproject.toml`**

```toml
[tool.pytest.ini_options]
pythonpath = ["."]
testpaths = ["tests"]
```

- [ ] **Step 4: Verify the toolchain**

Run: `pip install -r requirements.txt pytest`
Then: `pytest -q`
Expected: `no tests ran` (exit code 5) — that's fine, it confirms pytest is wired and `mono_sync` will be importable.

- [ ] **Step 5: Commit**

```bash
git add mono_sync/__init__.py tests/__init__.py requirements.txt pyproject.toml
git commit -m "chore: project scaffold (package, test config, requirements)"
```

---

## Task 2: `config.py` — environment configuration

**Files:**
- Create: `mono_sync/config.py`
- Test: `tests/test_config.py`

- [ ] **Step 1: Write `tests/test_config.py`**

```python
import pytest

from mono_sync.config import load_config


def _env(**over):
    base = {"MONOBANK_TOKEN": "m-token", "FIREFLY_URL": "http://app:8080/", "FIREFLY_TOKEN": "f-token"}
    base.update(over)
    return base


def test_defaults():
    cfg = load_config(_env())
    assert cfg.monobank_token == "m-token"
    assert cfg.firefly_url == "http://app:8080"  # trailing slash stripped
    assert cfg.firefly_token == "f-token"
    assert cfg.poll_interval_minutes == 5
    assert cfg.backfill is True
    assert cfg.backfill_floor_date == "2023-05-01"
    assert cfg.mcc_categories is False
    assert cfg.timezone == "Europe/Kyiv"
    assert cfg.log_level == "info"
    assert cfg.db_path == "/data/state.db"


def test_overrides():
    cfg = load_config(_env(POLL_INTERVAL_MINUTES="15", BACKFILL="false", MCC_CATEGORIES="yes",
                           BACKFILL_FLOOR_DATE="2020-01-01", LOG_LEVEL="debug", DB_PATH="/tmp/x.db"))
    assert cfg.poll_interval_minutes == 15
    assert cfg.backfill is False
    assert cfg.mcc_categories is True
    assert cfg.backfill_floor_date == "2020-01-01"
    assert cfg.log_level == "debug"
    assert cfg.db_path == "/tmp/x.db"


def test_missing_required():
    with pytest.raises(RuntimeError, match="MONOBANK_TOKEN"):
        load_config({"FIREFLY_URL": "x", "FIREFLY_TOKEN": "y"})


def test_bad_floor_date():
    with pytest.raises(RuntimeError, match="BACKFILL_FLOOR_DATE"):
        load_config(_env(BACKFILL_FLOOR_DATE="2020/01/01"))


def test_bad_poll_interval():
    with pytest.raises(RuntimeError, match="POLL_INTERVAL_MINUTES"):
        load_config(_env(POLL_INTERVAL_MINUTES="0"))
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_config.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'mono_sync.config'`.

- [ ] **Step 3: Write `mono_sync/config.py`**

```python
"""Configuration loaded and validated from environment variables."""
from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime

_TRUE = {"1", "true", "yes", "on"}


def _as_bool(value: str) -> bool:
    return value.strip().lower() in _TRUE


@dataclass(frozen=True)
class Config:
    monobank_token: str
    firefly_url: str
    firefly_token: str
    poll_interval_minutes: int
    backfill: bool
    backfill_floor_date: str
    mcc_categories: bool
    timezone: str
    log_level: str
    db_path: str


def load_config(env: dict | None = None) -> Config:
    env = os.environ if env is None else env

    def required(name: str) -> str:
        value = (env.get(name) or "").strip()
        if not value:
            raise RuntimeError(f"missing required environment variable: {name}")
        return value

    try:
        poll = int(env.get("POLL_INTERVAL_MINUTES", "5"))
    except ValueError as exc:
        raise RuntimeError(f"POLL_INTERVAL_MINUTES must be an integer: {exc}") from exc
    if poll < 1:
        raise RuntimeError("POLL_INTERVAL_MINUTES must be >= 1")

    floor = (env.get("BACKFILL_FLOOR_DATE") or "2023-05-01").strip()
    try:
        datetime.strptime(floor, "%Y-%m-%d")
    except ValueError as exc:
        raise RuntimeError(f"BACKFILL_FLOOR_DATE must be YYYY-MM-DD: {exc}") from exc

    return Config(
        monobank_token=required("MONOBANK_TOKEN"),
        firefly_url=required("FIREFLY_URL").rstrip("/"),
        firefly_token=required("FIREFLY_TOKEN"),
        poll_interval_minutes=poll,
        backfill=_as_bool(env.get("BACKFILL", "true")),
        backfill_floor_date=floor,
        mcc_categories=_as_bool(env.get("MCC_CATEGORIES", "false")),
        timezone=(env.get("TZ") or "Europe/Kyiv").strip() or "Europe/Kyiv",
        log_level=(env.get("LOG_LEVEL") or "info").strip() or "info",
        db_path=(env.get("DB_PATH") or "/data/state.db").strip() or "/data/state.db",
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_config.py -q`
Expected: PASS (5 passed).

- [ ] **Step 5: Commit**

```bash
git add mono_sync/config.py tests/test_config.py
git commit -m "feat: config loader from environment variables"
```

---

## Task 3: `mapping.py` — StatementItem → Firefly transaction

**Files:**
- Create: `mono_sync/mapping.py`
- Test: `tests/test_mapping.py`

- [ ] **Step 1: Write `tests/test_mapping.py`**

```python
from mono_sync import mapping


def _item(**over):
    base = {"id": "tx-1", "time": 1_700_000_000, "amount": -12345, "operationAmount": -12345,
            "currencyCode": 980, "balance": 500000, "mcc": 5411, "description": "ATB Market",
            "hold": False, "cashbackAmount": 0}
    base.update(over)
    return base


def test_currency_alpha():
    assert mapping.currency_alpha(980) == "UAH"
    assert mapping.currency_alpha(840) == "USD"
    assert mapping.currency_alpha(999999) is None
    assert mapping.currency_alpha(None) is None


def test_mcc_category():
    assert mapping.mcc_category(5411) == "Groceries"
    assert mapping.mcc_category(123) is None


def test_transaction_hash_stable_and_sensitive():
    a = mapping.transaction_hash(_item())
    assert a == mapping.transaction_hash(_item())
    assert a != mapping.transaction_hash(_item(amount=-12346))
    assert a != mapping.transaction_hash(_item(hold=True))
    assert a != mapping.transaction_hash(_item(description="Other"))


def test_build_notes():
    notes = mapping.build_notes(_item(cashbackAmount=50, comment="lunch", receiptId="abc", hold=True))
    assert "MCC 5411" in notes
    assert "cashback 0.50" in notes
    assert "hold" in notes
    assert "comment: lunch" in notes
    assert "receiptId: abc" in notes
    assert mapping.build_notes(_item()) == "MCC 5411"


def test_withdrawal_mapping():
    tx = mapping.statement_item_to_transaction(
        _item(amount=-12345), asset_account_id="42", asset_currency_alpha="UAH",
        counterparty_name="Monobank", timezone="Europe/Kyiv", mcc_categories=False)
    assert tx["type"] == "withdrawal"
    assert tx["amount"] == "123.45"
    assert tx["currency_code"] == "UAH"
    assert tx["source_id"] == "42"
    assert tx["destination_name"] == "Monobank"
    assert "destination_id" not in tx and "source_name" not in tx
    assert tx["external_id"] == "tx-1"
    assert tx["description"] == "ATB Market"
    assert tx["tags"] == ["monobank"]
    assert tx["date"].startswith("2023-11-")


def test_deposit_mapping():
    tx = mapping.statement_item_to_transaction(
        _item(amount=50000), asset_account_id="42", asset_currency_alpha="UAH",
        counterparty_name="Monobank", timezone="Europe/Kyiv", mcc_categories=False)
    assert tx["type"] == "deposit"
    assert tx["amount"] == "500.00"
    assert tx["destination_id"] == "42"
    assert tx["source_name"] == "Monobank"
    assert "source_id" not in tx and "destination_name" not in tx


def test_hold_adds_tag():
    tx = mapping.statement_item_to_transaction(
        _item(hold=True), asset_account_id="1", asset_currency_alpha="UAH",
        counterparty_name="Monobank", timezone="Europe/Kyiv", mcc_categories=False)
    assert tx["tags"] == ["monobank", "hold"]
    assert "hold" in tx["notes"]


def test_foreign_currency_sets_foreign_amount():
    tx = mapping.statement_item_to_transaction(
        _item(amount=-40000, operationAmount=-1000, currencyCode=840),
        asset_account_id="1", asset_currency_alpha="UAH",
        counterparty_name="Monobank", timezone="Europe/Kyiv", mcc_categories=False)
    assert tx["amount"] == "400.00"
    assert tx["foreign_amount"] == "10.00"
    assert tx["foreign_currency_code"] == "USD"


def test_no_foreign_when_same_currency():
    tx = mapping.statement_item_to_transaction(
        _item(currencyCode=980), asset_account_id="1", asset_currency_alpha="UAH",
        counterparty_name="Monobank", timezone="Europe/Kyiv", mcc_categories=False)
    assert "foreign_amount" not in tx
    assert "foreign_currency_code" not in tx


def test_description_prefers_counter_name():
    tx = mapping.statement_item_to_transaction(
        _item(counterName="Jane Doe", description="P2P"),
        asset_account_id="1", asset_currency_alpha="UAH",
        counterparty_name="Monobank", timezone="Europe/Kyiv", mcc_categories=False)
    assert tx["description"] == "Jane Doe"


def test_category_only_when_enabled():
    off = mapping.statement_item_to_transaction(
        _item(mcc=5411), asset_account_id="1", asset_currency_alpha="UAH",
        counterparty_name="Monobank", timezone="Europe/Kyiv", mcc_categories=False)
    assert "category_name" not in off
    on = mapping.statement_item_to_transaction(
        _item(mcc=5411), asset_account_id="1", asset_currency_alpha="UAH",
        counterparty_name="Monobank", timezone="Europe/Kyiv", mcc_categories=True)
    assert on["category_name"] == "Groceries"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_mapping.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'mono_sync.mapping'`.

- [ ] **Step 3: Write `mono_sync/mapping.py`**

```python
"""Pure transformations: Monobank StatementItem -> Firefly III transaction payload."""
from __future__ import annotations

import hashlib
from datetime import datetime
from zoneinfo import ZoneInfo

# ISO 4217 numeric -> alpha (only what we expect to see; unknown -> None).
_CURRENCY = {
    980: "UAH", 840: "USD", 978: "EUR", 826: "GBP", 985: "PLN", 949: "TRY",
    203: "CZK", 348: "HUF", 124: "CAD", 756: "CHF", 392: "JPY", 156: "CNY",
}

# Optional MCC -> Firefly category name. Used only when mcc_categories is enabled.
_MCC_CATEGORY = {
    5411: "Groceries", 5412: "Groceries",
    5812: "Restaurants", 5813: "Restaurants", 5814: "Restaurants",
    5541: "Fuel", 5542: "Fuel",
    4111: "Transport", 4121: "Transport", 4131: "Transport",
    5912: "Pharmacy",
    4814: "Communication", 4899: "Communication",
}


def currency_alpha(numeric_code) -> str | None:
    return _CURRENCY.get(numeric_code)


def mcc_category(mcc) -> str | None:
    return _MCC_CATEGORY.get(mcc)


def transaction_hash(item: dict) -> str:
    raw = "{amount}|{hold}|{description}|{time}".format(
        amount=item.get("amount"),
        hold=int(bool(item.get("hold"))),
        description=item.get("description", ""),
        time=item.get("time"),
    )
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def build_notes(item: dict) -> str:
    lines: list[str] = []
    mcc = item.get("mcc")
    if mcc is not None:
        lines.append(f"MCC {mcc}")
    cashback = item.get("cashbackAmount") or 0
    if cashback:
        lines.append(f"cashback {cashback / 100:.2f}")
    if item.get("hold"):
        lines.append("hold")
    comment = (item.get("comment") or "").strip()
    if comment:
        lines.append(f"comment: {comment}")
    receipt = (item.get("receiptId") or "").strip()
    if receipt:
        lines.append(f"receiptId: {receipt}")
    return "\n".join(lines)


def statement_item_to_transaction(
    item: dict,
    *,
    asset_account_id: str,
    asset_currency_alpha: str,
    counterparty_name: str,
    timezone: str,
    mcc_categories: bool,
) -> dict:
    amount_minor = int(item["amount"])
    is_withdrawal = amount_minor < 0
    when = datetime.fromtimestamp(int(item["time"]), ZoneInfo(timezone)).isoformat()
    description = (item.get("counterName") or item.get("description") or "").strip() or "Monobank transaction"

    tx: dict = {
        "type": "withdrawal" if is_withdrawal else "deposit",
        "date": when,
        "amount": f"{abs(amount_minor) / 100:.2f}",
        "currency_code": asset_currency_alpha,
        "description": description,
        "external_id": str(item["id"]),
        "tags": ["monobank"] + (["hold"] if item.get("hold") else []),
        "notes": build_notes(item),
    }
    if is_withdrawal:
        tx["source_id"] = str(asset_account_id)
        tx["destination_name"] = counterparty_name
    else:
        tx["destination_id"] = str(asset_account_id)
        tx["source_name"] = counterparty_name

    op_currency = currency_alpha(item.get("currencyCode"))
    if op_currency and op_currency != asset_currency_alpha:
        op_amount_minor = int(item.get("operationAmount", amount_minor))
        tx["foreign_amount"] = f"{abs(op_amount_minor) / 100:.2f}"
        tx["foreign_currency_code"] = op_currency

    if mcc_categories:
        category = mcc_category(item.get("mcc"))
        if category:
            tx["category_name"] = category

    return tx
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_mapping.py -q`
Expected: PASS (12 passed).

- [ ] **Step 5: Commit**

```bash
git add mono_sync/mapping.py tests/test_mapping.py
git commit -m "feat: mapping from Monobank statement items to Firefly transactions"
```

---

## Task 4: `store.py` — SQLite state

**Files:**
- Create: `mono_sync/store.py`
- Test: `tests/test_store.py`

- [ ] **Step 1: Write `tests/test_store.py`**

```python
import pytest

from mono_sync.store import Store


@pytest.fixture
def store():
    s = Store(":memory:")
    yield s
    s.close()


def test_upsert_and_get_account(store):
    store.upsert_account(mono_account_id="acc1", firefly_account_id="42", currency_code=980, card_type="black")
    row = store.get_account("acc1")
    assert row["firefly_account_id"] == "42"
    assert row["currency_code"] == 980
    assert row["card_type"] == "black"
    assert row["last_synced_time"] == 0
    assert row["backfill_cursor"] is None
    assert row["backfill_complete"] == 0
    # upsert again -> updates, no duplicate
    store.upsert_account(mono_account_id="acc1", firefly_account_id="99", currency_code=840, card_type="black")
    assert store.get_account("acc1")["firefly_account_id"] == "99"
    assert len(store.all_accounts()) == 1


def test_get_account_missing(store):
    assert store.get_account("nope") is None


def test_all_accounts_sorted(store):
    store.upsert_account(mono_account_id="b", firefly_account_id="2", currency_code=980, card_type="black")
    store.upsert_account(mono_account_id="a", firefly_account_id="1", currency_code=980, card_type="black")
    assert [r["mono_account_id"] for r in store.all_accounts()] == ["a", "b"]


def test_account_cursor_and_flags(store):
    store.upsert_account(mono_account_id="a", firefly_account_id="1", currency_code=980, card_type="black")
    store.set_last_synced_time("a", 1234)
    store.set_backfill_cursor("a", 5678)
    store.set_backfill_complete("a", True)
    row = store.get_account("a")
    assert row["last_synced_time"] == 1234
    assert row["backfill_cursor"] == 5678
    assert row["backfill_complete"] == 1


def test_upsert_and_get_transaction(store):
    store.upsert_transaction(mono_tx_id="t1", mono_account_id="a", firefly_tx_id="100",
                             time=1000, amount_minor=-500, balance_minor=9500, hash="h1")
    row = store.get_transaction("t1")
    assert row["firefly_tx_id"] == "100"
    assert row["amount_minor"] == -500
    assert row["balance_minor"] == 9500
    assert row["hash"] == "h1"
    assert row["status"] == "ok"
    # upsert again with new data -> overwrites
    store.upsert_transaction(mono_tx_id="t1", mono_account_id="a", firefly_tx_id=None,
                             time=1000, amount_minor=-600, balance_minor=None, hash="h2", status="failed")
    row = store.get_transaction("t1")
    assert row["firefly_tx_id"] is None
    assert row["amount_minor"] == -600
    assert row["balance_minor"] is None
    assert row["hash"] == "h2"
    assert row["status"] == "failed"


def test_oldest_transaction(store):
    store.upsert_transaction(mono_tx_id="t2", mono_account_id="a", firefly_tx_id="2", time=2000, amount_minor=1, balance_minor=2, hash="x")
    store.upsert_transaction(mono_tx_id="t1", mono_account_id="a", firefly_tx_id="1", time=1000, amount_minor=3, balance_minor=4, hash="y")
    store.upsert_transaction(mono_tx_id="t3", mono_account_id="b", firefly_tx_id="3", time=500, amount_minor=5, balance_minor=6, hash="z")
    oldest = store.oldest_transaction("a")
    assert oldest["mono_tx_id"] == "t1"
    assert store.oldest_transaction("missing") is None


def test_sync_state(store):
    assert store.get_state("k") is None
    store.set_state("k", "v1")
    assert store.get_state("k") == "v1"
    store.set_state("k", "v2")
    assert store.get_state("k") == "v2"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_store.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'mono_sync.store'`.

- [ ] **Step 3: Write `mono_sync/store.py`**

```python
"""SQLite-backed sync state: account mappings, ingested transactions, misc key/value state."""
from __future__ import annotations

import os
import sqlite3

_SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts (
    mono_account_id    TEXT PRIMARY KEY,
    firefly_account_id TEXT,
    currency_code      INTEGER,
    card_type          TEXT,
    last_synced_time   INTEGER NOT NULL DEFAULT 0,
    backfill_cursor    INTEGER,
    backfill_complete  INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS transactions (
    mono_tx_id      TEXT PRIMARY KEY,
    mono_account_id TEXT NOT NULL,
    firefly_tx_id   TEXT,
    time            INTEGER NOT NULL,
    amount_minor    INTEGER NOT NULL,
    balance_minor   INTEGER,
    hash            TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'ok'
);
CREATE INDEX IF NOT EXISTS ix_transactions_account_time ON transactions(mono_account_id, time);
CREATE TABLE IF NOT EXISTS sync_state (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""


class Store:
    def __init__(self, db_path: str):
        if db_path != ":memory:":
            parent = os.path.dirname(db_path)
            if parent:
                os.makedirs(parent, exist_ok=True)
        self._conn = sqlite3.connect(db_path)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    # --- accounts ---
    def upsert_account(self, *, mono_account_id, firefly_account_id, currency_code, card_type) -> None:
        self._conn.execute(
            """INSERT INTO accounts (mono_account_id, firefly_account_id, currency_code, card_type)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(mono_account_id) DO UPDATE SET
                   firefly_account_id = excluded.firefly_account_id,
                   currency_code      = excluded.currency_code,
                   card_type          = excluded.card_type""",
            (mono_account_id, firefly_account_id, currency_code, card_type),
        )
        self._conn.commit()

    def get_account(self, mono_account_id):
        return self._conn.execute(
            "SELECT * FROM accounts WHERE mono_account_id = ?", (mono_account_id,)
        ).fetchone()

    def all_accounts(self) -> list:
        return list(self._conn.execute("SELECT * FROM accounts ORDER BY mono_account_id"))

    def set_last_synced_time(self, mono_account_id, value: int) -> None:
        self._conn.execute(
            "UPDATE accounts SET last_synced_time = ? WHERE mono_account_id = ?",
            (int(value), mono_account_id),
        )
        self._conn.commit()

    def set_backfill_cursor(self, mono_account_id, value: int) -> None:
        self._conn.execute(
            "UPDATE accounts SET backfill_cursor = ? WHERE mono_account_id = ?",
            (int(value), mono_account_id),
        )
        self._conn.commit()

    def set_backfill_complete(self, mono_account_id, complete: bool) -> None:
        self._conn.execute(
            "UPDATE accounts SET backfill_complete = ? WHERE mono_account_id = ?",
            (1 if complete else 0, mono_account_id),
        )
        self._conn.commit()

    # --- transactions ---
    def get_transaction(self, mono_tx_id):
        return self._conn.execute(
            "SELECT * FROM transactions WHERE mono_tx_id = ?", (mono_tx_id,)
        ).fetchone()

    def upsert_transaction(self, *, mono_tx_id, mono_account_id, firefly_tx_id, time,
                           amount_minor, balance_minor, hash, status="ok") -> None:
        self._conn.execute(
            """INSERT INTO transactions (mono_tx_id, mono_account_id, firefly_tx_id, time,
                                         amount_minor, balance_minor, hash, status)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(mono_tx_id) DO UPDATE SET
                   mono_account_id = excluded.mono_account_id,
                   firefly_tx_id   = excluded.firefly_tx_id,
                   time            = excluded.time,
                   amount_minor    = excluded.amount_minor,
                   balance_minor   = excluded.balance_minor,
                   hash            = excluded.hash,
                   status          = excluded.status""",
            (mono_tx_id, mono_account_id, firefly_tx_id, int(time), int(amount_minor),
             None if balance_minor is None else int(balance_minor), hash, status),
        )
        self._conn.commit()

    def oldest_transaction(self, mono_account_id):
        return self._conn.execute(
            "SELECT * FROM transactions WHERE mono_account_id = ? ORDER BY time ASC LIMIT 1",
            (mono_account_id,),
        ).fetchone()

    # --- sync_state ---
    def get_state(self, key) -> str | None:
        row = self._conn.execute("SELECT value FROM sync_state WHERE key = ?", (key,)).fetchone()
        return None if row is None else row["value"]

    def set_state(self, key, value) -> None:
        self._conn.execute(
            "INSERT INTO sync_state (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, str(value)),
        )
        self._conn.commit()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_store.py -q`
Expected: PASS (7 passed).

- [ ] **Step 5: Commit**

```bash
git add mono_sync/store.py tests/test_store.py
git commit -m "feat: SQLite store for sync state"
```

---

## Task 5: Test fakes — HTTP session + `store` fixture

**Files:**
- Create: `tests/fakes.py`, `tests/conftest.py`

This task adds only test scaffolding (no production code). `tests/fakes.py` will be extended in Task 8 with `FakeMonobankClient` / `FakeFireflyClient` / `make_config`.

- [ ] **Step 1: Write `tests/fakes.py`**

```python
"""In-memory fakes used by the test suite."""
from __future__ import annotations


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
```

- [ ] **Step 2: Write `tests/conftest.py`**

```python
import pytest

from mono_sync.store import Store


@pytest.fixture
def store():
    s = Store(":memory:")
    yield s
    s.close()
```

- [ ] **Step 3: Confirm collection still works**

Run: `pytest -q`
Expected: PASS — all existing tests still pass (the new files add no tests yet).

- [ ] **Step 4: Commit**

```bash
git add tests/fakes.py tests/conftest.py
git commit -m "test: HTTP session fakes and store fixture"
```

---

## Task 6: `monobank.py` — rate limiter + API client

**Files:**
- Create: `mono_sync/monobank.py`
- Test: `tests/test_monobank.py`

- [ ] **Step 1: Write `tests/test_monobank.py`**

```python
import pytest

from mono_sync.monobank import MonobankClient, MonobankError, RateLimiter
from tests.fakes import FakeResponse, FakeSession


def test_rate_limiter_first_call_no_sleep():
    slept = []
    rl = RateLimiter(65.0, monotonic=lambda: 0.0, sleep=slept.append)
    rl.wait()
    assert slept == []


def test_rate_limiter_sleeps_to_maintain_interval():
    clock = [0.0]
    slept = []

    def sleep(seconds):
        slept.append(seconds)
        clock[0] += seconds

    rl = RateLimiter(65.0, monotonic=lambda: clock[0], sleep=sleep)
    rl.wait()                 # t=0, first call, no sleep
    clock[0] = 10.0           # only 10s passed
    rl.wait()                 # must sleep 55s
    assert slept == [55.0]
    clock[0] = 1000.0         # plenty of time passed
    rl.wait()                 # no extra sleep
    assert slept == [55.0]


def test_client_info_returns_json():
    session = FakeSession([FakeResponse(200, json_data={"name": "A", "accounts": []})])
    client = MonobankClient("tok", RateLimiter(0.0), session=session, sleep=lambda s: None)
    assert client.client_info() == {"name": "A", "accounts": []}
    method, url, kwargs = session.calls[0]
    assert url.endswith("/personal/client-info")
    assert kwargs["headers"]["X-Token"] == "tok"


def test_statement_builds_url():
    session = FakeSession([FakeResponse(200, json_data=[{"id": "x"}])])
    client = MonobankClient("tok", RateLimiter(0.0), session=session, sleep=lambda s: None)
    assert client.statement("acc-1", 100, 200) == [{"id": "x"}]
    assert session.calls[0][1].endswith("/personal/statement/acc-1/100/200")


def test_retries_on_429_then_succeeds():
    session = FakeSession([FakeResponse(429, text="rate"), FakeResponse(200, json_data={"ok": True})])
    sleeps = []
    client = MonobankClient("tok", RateLimiter(0.0), session=session, sleep=sleeps.append)
    assert client.client_info() == {"ok": True}
    assert sleeps == [60]     # backed off once


def test_raises_on_4xx():
    session = FakeSession([FakeResponse(403, text="forbidden")])
    client = MonobankClient("tok", RateLimiter(0.0), session=session, sleep=lambda s: None)
    with pytest.raises(MonobankError, match="403"):
        client.client_info()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_monobank.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'mono_sync.monobank'`.

- [ ] **Step 3: Write `mono_sync/monobank.py`**

```python
"""Monobank personal API client with a conservative global rate limiter."""
from __future__ import annotations

import logging
import threading
import time

import requests

log = logging.getLogger(__name__)

_BASE = "https://api.monobank.ua"
_MAX_RETRIES = 5


class RateLimiter:
    """Ensures at least ``min_interval`` seconds between successive ``wait()`` calls."""

    def __init__(self, min_interval: float, *, monotonic=time.monotonic, sleep=time.sleep):
        self._min = float(min_interval)
        self._monotonic = monotonic
        self._sleep = sleep
        self._last: float | None = None
        self._lock = threading.Lock()

    def wait(self) -> None:
        with self._lock:
            now = self._monotonic()
            if self._last is not None:
                gap = now - self._last
                if gap < self._min:
                    self._sleep(self._min - gap)
                    now = self._monotonic()
            self._last = now


class MonobankError(Exception):
    pass


class MonobankClient:
    def __init__(self, token: str, limiter: RateLimiter, *, session: requests.Session | None = None, sleep=time.sleep):
        self._token = token
        self._limiter = limiter
        self._session = session or requests.Session()
        self._sleep = sleep

    def _get(self, path: str):
        url = _BASE + path
        for attempt in range(1, _MAX_RETRIES + 1):
            self._limiter.wait()
            try:
                resp = self._session.get(url, headers={"X-Token": self._token}, timeout=30)
            except requests.RequestException as exc:
                log.warning("monobank request error on %s (%d/%d): %s", path, attempt, _MAX_RETRIES, exc)
                self._sleep(min(60 * attempt, 300))
                continue
            if resp.status_code == 200:
                return resp.json()
            if resp.status_code == 429 or resp.status_code >= 500:
                log.warning("monobank %s on %s (%d/%d); backing off", resp.status_code, path, attempt, _MAX_RETRIES)
                self._sleep(60)
                continue
            raise MonobankError(f"monobank GET {path} -> {resp.status_code}: {resp.text[:300]}")
        raise MonobankError(f"monobank GET {path} failed after {_MAX_RETRIES} attempts")

    def client_info(self) -> dict:
        return self._get("/personal/client-info")

    def statement(self, account_id: str, frm: int, to: int) -> list:
        return self._get(f"/personal/statement/{account_id}/{int(frm)}/{int(to)}")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_monobank.py -q`
Expected: PASS (6 passed).

- [ ] **Step 5: Commit**

```bash
git add mono_sync/monobank.py tests/test_monobank.py
git commit -m "feat: Monobank API client with rate limiter and retry"
```

---

## Task 7: `firefly.py` — Firefly III API client

**Files:**
- Create: `mono_sync/firefly.py`
- Test: `tests/test_firefly.py`

- [ ] **Step 1: Write `tests/test_firefly.py`**

```python
import json

import pytest

from mono_sync.firefly import FireflyClient, FireflyError
from tests.fakes import FakeResponse, FakeSession


def _client(responses):
    return FireflyClient("http://app:8080/", "ff-token", session=FakeSession(responses))


def test_request_sets_auth_header():
    session = FakeSession([FakeResponse(200, json_data={"data": []})])
    FireflyClient("http://app:8080", "ff-token", session=session).find_transaction_by_external_id("x")
    _, url, kwargs = session.calls[0]
    assert kwargs["headers"]["Authorization"] == "Bearer ff-token"
    assert url == "http://app:8080/api/v1/search/transactions"


def test_request_raises_on_error_with_body():
    client = _client([FakeResponse(422, text='{"message":"bad"}')])
    with pytest.raises(FireflyError, match="422"):
        client.create_transaction({"type": "withdrawal"})


def test_find_asset_account_matches_name_across_pages():
    page1 = FakeResponse(200, json_data={
        "data": [{"id": "1", "attributes": {"name": "Other"}}],
        "meta": {"pagination": {"total_pages": 2}},
    })
    page2 = FakeResponse(200, json_data={
        "data": [{"id": "7", "attributes": {"name": "Monobank Black"}}],
        "meta": {"pagination": {"total_pages": 2}},
    })
    found = _client([page1, page2]).find_asset_account("Monobank Black")
    assert found["id"] == "7"


def test_find_asset_account_returns_none_when_absent():
    page = FakeResponse(200, json_data={"data": [], "meta": {"pagination": {"total_pages": 1}}})
    assert _client([page]).find_asset_account("Nope") is None


def test_create_asset_account_body():
    session = FakeSession([FakeResponse(200, json_data={"data": {"id": "9", "attributes": {"name": "Monobank Black"}}})])
    client = FireflyClient("http://app:8080", "t", session=session)
    out = client.create_asset_account(name="Monobank Black", currency_code="UAH",
                                      opening_balance="0", opening_balance_date="2023-05-01", notes="n")
    assert out["id"] == "9"
    body = session.calls[0][2]["json"]
    assert body["type"] == "asset"
    assert body["account_role"] == "defaultAsset"
    assert body["currency_code"] == "UAH"
    assert body["opening_balance"] == "0"
    assert body["opening_balance_date"] == "2023-05-01"


def test_create_transaction_wraps_payload():
    session = FakeSession([FakeResponse(200, json_data={"data": {"id": "55", "attributes": {}}})])
    client = FireflyClient("http://app:8080", "t", session=session)
    out = client.create_transaction({"type": "withdrawal", "amount": "1.00"})
    assert out["id"] == "55"
    body = session.calls[0][2]["json"]
    assert body["error_if_duplicate_hash"] is False
    assert body["apply_rules"] is True
    assert body["fire_webhooks"] is False
    assert body["transactions"] == [{"type": "withdrawal", "amount": "1.00"}]


def test_update_transaction_fetches_journal_id_then_puts():
    get_resp = FakeResponse(200, json_data={"data": {"id": "55", "attributes": {"transactions": [{"transaction_journal_id": "777"}]}}})
    put_resp = FakeResponse(200, json_data={"data": {"id": "55", "attributes": {}}})
    session = FakeSession([get_resp, put_resp])
    client = FireflyClient("http://app:8080", "t", session=session)
    client.update_transaction("55", {"type": "withdrawal", "amount": "2.00"})
    assert session.calls[0][0] == "GET"
    assert session.calls[1][0] == "PUT"
    put_body = session.calls[1][2]["json"]
    assert put_body["transactions"][0]["transaction_journal_id"] == "777"
    assert put_body["transactions"][0]["amount"] == "2.00"


def test_get_account_balance():
    session = FakeSession([FakeResponse(200, json_data={"data": {"id": "9", "attributes": {"current_balance": "123.45"}}})])
    client = FireflyClient("http://app:8080", "t", session=session)
    assert client.get_account_balance("9") == pytest.approx(123.45)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_firefly.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'mono_sync.firefly'`.

- [ ] **Step 3: Write `mono_sync/firefly.py`**

```python
"""Firefly III API client."""
from __future__ import annotations

import logging

import requests

log = logging.getLogger(__name__)


class FireflyError(Exception):
    pass


class FireflyClient:
    def __init__(self, base_url: str, token: str, *, session: requests.Session | None = None):
        self._base = base_url.rstrip("/")
        self._session = session or requests.Session()
        self._headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        }

    def _request(self, method: str, path: str, **kwargs) -> dict:
        url = f"{self._base}{path}"
        resp = self._session.request(method, url, headers=self._headers, timeout=30, **kwargs)
        if 200 <= resp.status_code < 300:
            return resp.json() if resp.content else {}
        raise FireflyError(f"firefly {method} {path} -> {resp.status_code}: {resp.text[:500]}")

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
        body = {"error_if_duplicate_hash": False, "apply_rules": True, "fire_webhooks": False, "transactions": [tx]}
        data = self._request("POST", "/api/v1/transactions", json=body)
        return {"id": data["data"]["id"], **data["data"]["attributes"]}

    def update_transaction(self, group_id, tx: dict) -> dict:
        current = self._request("GET", f"/api/v1/transactions/{group_id}")
        splits = current["data"]["attributes"]["transactions"]
        updated = dict(tx)
        updated["transaction_journal_id"] = splits[0]["transaction_journal_id"]
        body = {"apply_rules": True, "fire_webhooks": False, "transactions": [updated]}
        data = self._request("PUT", f"/api/v1/transactions/{group_id}", json=body)
        return {"id": data["data"]["id"], **data["data"]["attributes"]}
```

> Note: the unused `import json` in the test from Step 1 is harmless; remove it if your linter complains.

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_firefly.py -q`
Expected: PASS (9 passed).

- [ ] **Step 5: Commit**

```bash
git add mono_sync/firefly.py tests/test_firefly.py
git commit -m "feat: Firefly III API client"
```

---

## Task 8: Extend test fakes — `FakeMonobankClient`, `FakeFireflyClient`, `make_config`

**Files:**
- Modify: `tests/fakes.py` (append)
- Modify: `tests/conftest.py` (add `fake_firefly` fixture)

No production code; these support Tasks 9–11. Verified by the sync tests that follow.

- [ ] **Step 1: Append to `tests/fakes.py`**

Add these imports at the top of `tests/fakes.py` (after the existing module docstring):

```python
import copy

from mono_sync.config import Config
from mono_sync.firefly import FireflyError
```

Then append at the end of the file:

```python
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
```

- [ ] **Step 2: Add `fake_firefly` fixture to `tests/conftest.py`**

Append:

```python
from tests.fakes import FakeFireflyClient


@pytest.fixture
def fake_firefly():
    return FakeFireflyClient()
```

- [ ] **Step 3: Confirm collection still works**

Run: `pytest -q`
Expected: PASS — all existing tests still pass.

- [ ] **Step 4: Commit**

```bash
git add tests/fakes.py tests/conftest.py
git commit -m "test: client fakes and config builder for sync tests"
```

---

## Task 9: `sync.py` — account setup & single-item ingest

**Files:**
- Create: `mono_sync/sync.py`
- Test: `tests/test_sync_accounts.py`

- [ ] **Step 1: Write `tests/test_sync_accounts.py`**

```python
import pytest

from mono_sync import mapping
from mono_sync.sync import Syncer
from tests.fakes import FakeFireflyClient, FakeMonobankClient, make_config

NOW = 1_700_000_000


def _statement_item(**over):
    base = {"id": "tx-1", "time": NOW - 86400, "amount": -1000, "balance": 50000,
            "description": "Shop", "mcc": 5411, "currencyCode": 980, "hold": False}
    base.update(over)
    return base


def _client_info(*accounts):
    return {"name": "U", "accounts": list(accounts)}


def test_ensure_accounts_creates_asset_and_stores_mapping(store):
    mono = FakeMonobankClient(client_info=_client_info({"id": "uah", "type": "black", "currencyCode": 980, "balance": 12345}), statements={})
    ff = FakeFireflyClient()
    Syncer(mono, ff, store, make_config()).ensure_accounts()
    row = store.get_account("uah")
    assert row is not None
    assert row["currency_code"] == 980
    assert row["card_type"] == "black"
    ff_acc = ff.accounts[row["firefly_account_id"]]
    assert ff_acc["name"] == "Monobank Black"
    assert ff_acc["currency_code"] == "UAH"


def test_ensure_accounts_reuses_existing_firefly_account(store):
    ff = FakeFireflyClient()
    ff.accounts["88"] = {"name": "Monobank Black", "currency_code": "UAH", "opening_balance": "0", "opening_balance_date": "2023-01-01", "notes": ""}
    mono = FakeMonobankClient(client_info=_client_info({"id": "uah", "type": "black", "currencyCode": 980, "balance": 0}), statements={})
    Syncer(mono, ff, store, make_config()).ensure_accounts()
    assert store.get_account("uah")["firefly_account_id"] == "88"
    assert len(ff.accounts) == 1


def test_ensure_accounts_skips_already_mapped(store):
    store.upsert_account(mono_account_id="uah", firefly_account_id="42", currency_code=980, card_type="black")
    mono = FakeMonobankClient(client_info=_client_info({"id": "uah", "type": "black", "currencyCode": 980, "balance": 0}), statements={})
    ff = FakeFireflyClient()
    Syncer(mono, ff, store, make_config()).ensure_accounts()
    assert ff.accounts == {}
    assert store.get_account("uah")["firefly_account_id"] == "42"


def test_ensure_accounts_multi_currency_black(store):
    mono = FakeMonobankClient(client_info=_client_info(
        {"id": "uah", "type": "black", "currencyCode": 980, "balance": 0},
        {"id": "usd", "type": "black", "currencyCode": 840, "balance": 0},
        {"id": "jar", "type": "yellow", "currencyCode": 980, "balance": 0},
    ), statements={})
    ff = FakeFireflyClient()
    Syncer(mono, ff, store, make_config()).ensure_accounts()
    assert sorted(a["name"] for a in ff.accounts.values()) == ["Monobank Black", "Monobank Black USD"]
    assert store.get_account("jar") is None


def test_ensure_accounts_raises_without_black(store):
    mono = FakeMonobankClient(client_info=_client_info({"id": "j", "type": "yellow", "currencyCode": 980, "balance": 0}), statements={})
    with pytest.raises(RuntimeError, match="black"):
        Syncer(mono, FakeFireflyClient(), store, make_config()).ensure_accounts()


def _mapped_account(store):
    store.upsert_account(mono_account_id="uah", firefly_account_id="100", currency_code=980, card_type="black")
    return store.get_account("uah")


def test_ingest_new_item_creates_transaction_and_records_it(store, fake_firefly):
    acc = _mapped_account(store)
    syncer = Syncer(FakeMonobankClient(client_info={}, statements={}), fake_firefly, store, make_config(), now=lambda: NOW)
    outcome = syncer._ingest_item(acc, _statement_item(id="t1", amount=-2500, balance=47500))
    assert outcome == "created"
    assert fake_firefly.created_tx_count == 1
    rec = store.get_transaction("t1")
    assert rec["firefly_tx_id"] is not None
    assert rec["status"] == "ok"
    assert rec["amount_minor"] == -2500
    assert rec["balance_minor"] == 47500


def test_ingest_unchanged_item_is_skipped(store, fake_firefly):
    acc = _mapped_account(store)
    syncer = Syncer(FakeMonobankClient(client_info={}, statements={}), fake_firefly, store, make_config(), now=lambda: NOW)
    item = _statement_item(id="t1")
    syncer._ingest_item(acc, item)
    fake_firefly.created_tx_count = 0
    outcome = syncer._ingest_item(acc, item)
    assert outcome == "skipped"
    assert fake_firefly.created_tx_count == 0


def test_ingest_changed_item_updates_transaction(store, fake_firefly):
    acc = _mapped_account(store)
    syncer = Syncer(FakeMonobankClient(client_info={}, statements={}), fake_firefly, store, make_config(), now=lambda: NOW)
    syncer._ingest_item(acc, _statement_item(id="t1", amount=-7700, hold=True))
    outcome = syncer._ingest_item(acc, _statement_item(id="t1", amount=-8000, hold=False))
    assert outcome == "updated"
    assert fake_firefly.updated_tx_count == 1
    assert store.get_transaction("t1")["amount_minor"] == -8000


def test_ingest_adopts_existing_firefly_transaction_when_store_lost(store, fake_firefly):
    acc = _mapped_account(store)
    # Firefly already has the transaction (e.g. SQLite was wiped) but the store does not.
    fake_firefly.transactions["55"] = {"external_id": "t1"}
    syncer = Syncer(FakeMonobankClient(client_info={}, statements={}), fake_firefly, store, make_config(), now=lambda: NOW)
    outcome = syncer._ingest_item(acc, _statement_item(id="t1"))
    assert outcome == "updated"
    assert fake_firefly.created_tx_count == 0
    assert store.get_transaction("t1")["firefly_tx_id"] == "55"


def test_ingest_records_failure_and_allows_retry(store, fake_firefly):
    acc = _mapped_account(store)
    fake_firefly.create_failures = 1
    syncer = Syncer(FakeMonobankClient(client_info={}, statements={}), fake_firefly, store, make_config(), now=lambda: NOW)
    assert syncer._ingest_item(acc, _statement_item(id="t1")) == "failed"
    assert store.get_transaction("t1")["status"] == "failed"
    assert store.get_transaction("t1")["firefly_tx_id"] is None
    # retry succeeds because status != 'ok' bypasses the skip check
    assert syncer._ingest_item(acc, _statement_item(id="t1")) == "created"
    assert store.get_transaction("t1")["status"] == "ok"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_sync_accounts.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'mono_sync.sync'`.

- [ ] **Step 3: Write `mono_sync/sync.py`**

```python
"""Sync orchestration: account setup, historical backfill, incremental polling, reconciliation."""
from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from . import mapping
from .firefly import FireflyError

log = logging.getLogger(__name__)

_WINDOW_SECONDS = 30 * 86400      # safely under Monobank's 31d+1h statement window cap
_OVERLAP_SECONDS = 24 * 3600      # incremental re-scan overlap to catch late items / hold settlements
_RECONCILE_INTERVAL = 3600        # balance reconciliation runs at most hourly
_COUNTERPARTY_NAME = "Monobank"   # single shared expense/revenue account


def _asset_account_name(currency_alpha: str) -> str:
    return "Monobank Black" if currency_alpha == "UAH" else f"Monobank Black {currency_alpha}"


class Syncer:
    def __init__(self, mono, firefly, store, config, *, now=None):
        self.mono = mono
        self.ff = firefly
        self.store = store
        self.cfg = config
        self._now = now or (lambda: int(time.time()))
        self._tz = ZoneInfo(config.timezone)

    # ------------------------------------------------------------------ accounts
    def ensure_accounts(self) -> None:
        info = self.mono.client_info()
        black = [a for a in info.get("accounts", []) if a.get("type") == "black"]
        if not black:
            raise RuntimeError("no Monobank account with type 'black' found in client-info")
        for acc in black:
            mono_id = acc["id"]
            if self.store.get_account(mono_id) is not None:
                continue
            currency_code = acc.get("currencyCode")
            alpha = mapping.currency_alpha(currency_code) or "UAH"
            name = _asset_account_name(alpha)
            existing = self.ff.find_asset_account(name)
            if existing is not None:
                ff_id = existing["id"]
            else:
                today = datetime.now(self._tz).date().isoformat()
                created = self.ff.create_asset_account(
                    name=name, currency_code=alpha, opening_balance="0",
                    opening_balance_date=today, notes=f"Synced from Monobank account {mono_id}",
                )
                ff_id = created["id"]
            self.store.upsert_account(mono_account_id=mono_id, firefly_account_id=ff_id,
                                      currency_code=currency_code, card_type=acc.get("type"))
            log.info("account ready: mono=%s firefly=%s name=%r", mono_id, ff_id, name)

    # ------------------------------------------------------------------ ingest one item
    def _ingest_item(self, account_row, item: dict) -> str:
        """Create or update the Firefly transaction for one StatementItem.

        Returns one of: 'created', 'updated', 'skipped', 'failed'.
        """
        mono_id = str(item["id"])
        new_hash = mapping.transaction_hash(item)
        existing = self.store.get_transaction(mono_id)
        if existing is not None and existing["hash"] == new_hash and existing["status"] == "ok":
            return "skipped"

        tx = mapping.statement_item_to_transaction(
            item,
            asset_account_id=account_row["firefly_account_id"],
            asset_currency_alpha=mapping.currency_alpha(account_row["currency_code"]) or "UAH",
            counterparty_name=_COUNTERPARTY_NAME,
            timezone=self.cfg.timezone,
            mcc_categories=self.cfg.mcc_categories,
        )
        try:
            if existing is not None and existing["firefly_tx_id"]:
                self.ff.update_transaction(existing["firefly_tx_id"], tx)
                ff_id, outcome = existing["firefly_tx_id"], "updated"
            else:
                found = self.ff.find_transaction_by_external_id(mono_id)
                if found is not None:
                    ff_id = found["id"]
                    self.ff.update_transaction(ff_id, tx)
                    outcome = "updated"
                else:
                    created = self.ff.create_transaction(tx)
                    ff_id, outcome = created["id"], "created"
        except FireflyError as exc:
            log.warning("failed to sync Monobank tx %s: %s", mono_id, exc)
            self.store.upsert_transaction(
                mono_tx_id=mono_id, mono_account_id=account_row["mono_account_id"], firefly_tx_id=None,
                time=item["time"], amount_minor=item["amount"], balance_minor=item.get("balance"),
                hash=new_hash, status="failed",
            )
            return "failed"

        self.store.upsert_transaction(
            mono_tx_id=mono_id, mono_account_id=account_row["mono_account_id"], firefly_tx_id=ff_id,
            time=item["time"], amount_minor=item["amount"], balance_minor=item.get("balance"),
            hash=new_hash, status="ok",
        )
        return outcome
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_sync_accounts.py -q`
Expected: PASS (11 passed).

- [ ] **Step 5: Commit**

```bash
git add mono_sync/sync.py tests/test_sync_accounts.py
git commit -m "feat: syncer account setup and single-item ingest"
```

---

## Task 10: `sync.py` — historical backfill

**Files:**
- Modify: `mono_sync/sync.py` (add methods to `Syncer`)
- Test: `tests/test_sync_backfill.py`

- [ ] **Step 1: Write `tests/test_sync_backfill.py`**

```python
from mono_sync.sync import Syncer
from tests.fakes import FakeFireflyClient, FakeMonobankClient, make_config

NOW = 1_700_000_000  # Nov 2023; floor date 2023-05-01 is ~197 days earlier


def _item(id, time, amount, balance, **over):
    base = {"id": id, "time": time, "amount": amount, "balance": balance,
            "description": f"shop {id}", "mcc": 5411, "currencyCode": 980, "hold": False}
    base.update(over)
    return base


def _client_info(balance=90000):
    return {"name": "U", "accounts": [{"id": "uah", "type": "black", "currencyCode": 980, "balance": balance}]}


def _syncer(store, statements, *, client_info=None):
    mono = FakeMonobankClient(client_info=client_info or _client_info(), statements=statements)
    ff = FakeFireflyClient()
    return Syncer(mono, ff, store, make_config(), now=lambda: NOW), mono, ff


def test_backfill_ingests_all_and_completes(store):
    items = [
        _item("a", NOW - 5 * 86400, -10000, 90000),
        _item("b", NOW - 40 * 86400, -20000, 100000),
        _item("c", NOW - 100 * 86400, 50000, 120000),
    ]
    syncer, mono, ff = _syncer(store, {"uah": items})
    syncer.ensure_accounts()
    syncer.run_backfill()
    assert sorted(tx["external_id"] for tx in ff.transactions.values()) == ["a", "b", "c"]
    assert ff.created_tx_count == 3
    assert store.get_account("uah")["backfill_complete"] == 1


def test_backfill_sets_opening_balance_from_oldest(store):
    items = [
        _item("a", NOW - 5 * 86400, -10000, 90000),
        _item("c", NOW - 100 * 86400, 50000, 120000),  # oldest: income 500, balance after = 1200 -> opening = 700.00
    ]
    syncer, mono, ff = _syncer(store, {"uah": items})
    syncer.ensure_accounts()
    syncer.run_backfill()
    ff_acc = ff.accounts[store.get_account("uah")["firefly_account_id"]]
    assert ff_acc["opening_balance"] == "700.00"
    # opening_balance_date is the day before the oldest transaction
    assert ff_acc["opening_balance_date"] < "2023-08-04"  # NOW-100d ≈ 2023-08-03


def test_backfill_is_idempotent(store):
    items = [_item("a", NOW - 5 * 86400, -10000, 90000)]
    syncer, mono, ff = _syncer(store, {"uah": items})
    syncer.ensure_accounts()
    syncer.run_backfill()
    syncer.run_backfill()  # account is backfill_complete -> skipped entirely
    assert ff.created_tx_count == 1


def test_backfill_resumes_and_skips_completed_accounts(store):
    # Two black accounts: one already completed, one fresh.
    store.upsert_account(mono_account_id="done", firefly_account_id="500", currency_code=980, card_type="black")
    store.set_backfill_complete("done", True)
    mono = FakeMonobankClient(
        client_info={"name": "U", "accounts": [
            {"id": "done", "type": "black", "currencyCode": 980, "balance": 0},
            {"id": "uah", "type": "black", "currencyCode": 980, "balance": 100},
        ]},
        statements={"uah": [_item("x", NOW - 3 * 86400, -100, 100)], "done": [_item("nope", NOW - 1 * 86400, -1, 1)]},
    )
    ff = FakeFireflyClient()
    syncer = Syncer(mono, ff, store, make_config(), now=lambda: NOW)
    syncer.ensure_accounts()
    syncer.run_backfill()
    ext = {tx["external_id"] for tx in ff.transactions.values()}
    assert ext == {"x"}                       # 'done' account was not touched
    assert ("done", ) not in [(c[0],) for c in mono.statement_calls]


def test_backfill_splits_oversized_window(store):
    base_t = NOW - 8 * 86400
    items = [_item(f"x{i}", base_t + i * 60, -100, 50000) for i in range(501)]  # 501 items in ~8.3 h
    syncer, mono, ff = _syncer(store, {"uah": items}, client_info=_client_info(50000))
    syncer.ensure_accounts()
    syncer.run_backfill()
    assert ff.created_tx_count == 501
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_sync_backfill.py -q`
Expected: FAIL — `AttributeError: 'Syncer' object has no attribute 'run_backfill'`.

- [ ] **Step 3: Add backfill methods to `Syncer` in `mono_sync/sync.py`**

Append these methods inside the `Syncer` class (after `_ingest_item`):

```python
    # ------------------------------------------------------------------ statement fetching
    def _fetch_chunk(self, account_id: str, frm: int, to: int) -> list:
        """Fetch all statement items in [frm, to], splitting the window if Monobank caps the response at 500."""
        items = self.mono.statement(account_id, frm, to)
        if len(items) >= 500 and (to - frm) > 1:
            mid = frm + (to - frm) // 2
            return self._fetch_chunk(account_id, frm, mid) + self._fetch_chunk(account_id, mid, to)
        return items

    # ------------------------------------------------------------------ backfill
    def run_backfill(self) -> None:
        floor_ts = int(datetime.strptime(self.cfg.backfill_floor_date, "%Y-%m-%d").replace(tzinfo=self._tz).timestamp())
        for acc in self.store.all_accounts():
            if acc["backfill_complete"]:
                continue
            self._backfill_account(acc, floor_ts)

    def _backfill_account(self, acc, floor_ts: int) -> None:
        mono_id = acc["mono_account_id"]
        cursor = acc["backfill_cursor"] if acc["backfill_cursor"] is not None else self._now()
        log.info("backfill starting: account=%s cursor=%s floor=%s", mono_id, cursor, floor_ts)
        while cursor > floor_ts:
            frm = max(cursor - _WINDOW_SECONDS, floor_ts)
            items = self._fetch_chunk(mono_id, frm, cursor)
            created = updated = 0
            for it in items:
                outcome = self._ingest_item(acc, it)
                created += outcome == "created"
                updated += outcome == "updated"
            log.info("backfill window account=%s [%s,%s]: %d items (%d new, %d updated)",
                     mono_id, frm, cursor, len(items), created, updated)
            cursor = frm
            self.store.set_backfill_cursor(mono_id, cursor)
        self._set_opening_balance(acc)
        self.store.set_backfill_complete(mono_id, True)
        log.info("backfill complete: account=%s", mono_id)

    def _set_opening_balance(self, acc) -> None:
        oldest = self.store.oldest_transaction(acc["mono_account_id"])
        if oldest is None or oldest["balance_minor"] is None:
            return
        opening_minor = oldest["balance_minor"] - oldest["amount_minor"]
        opening = f"{opening_minor / 100:.2f}"
        date_str = (datetime.fromtimestamp(oldest["time"], self._tz).date() - timedelta(days=1)).isoformat()
        self.ff.update_asset_account_opening_balance(
            acc["firefly_account_id"], opening_balance=opening, opening_balance_date=date_str,
        )
        log.info("opening balance set: account=%s firefly=%s -> %s on %s",
                 acc["mono_account_id"], acc["firefly_account_id"], opening, date_str)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_sync_backfill.py -q`
Expected: PASS (5 passed).

- [ ] **Step 5: Commit**

```bash
git add mono_sync/sync.py tests/test_sync_backfill.py
git commit -m "feat: historical backfill of Monobank statements"
```

---

## Task 11: `sync.py` — incremental polling & balance reconciliation

**Files:**
- Modify: `mono_sync/sync.py` (add methods to `Syncer`)
- Test: `tests/test_sync_incremental.py`

- [ ] **Step 1: Write `tests/test_sync_incremental.py`**

```python
from mono_sync import mapping
from mono_sync.sync import Syncer
from tests.fakes import FakeMonobankClient, make_config

NOW = 1_700_000_000


def _item(id, time, amount, balance, **over):
    base = {"id": id, "time": time, "amount": amount, "balance": balance,
            "description": f"shop {id}", "mcc": 5411, "currencyCode": 980, "hold": False}
    base.update(over)
    return base


def _mapped(store, *, last_synced=0, ff_id="100"):
    store.upsert_account(mono_account_id="uah", firefly_account_id=ff_id, currency_code=980, card_type="black")
    if last_synced:
        store.set_last_synced_time("uah", last_synced)
    return store.get_account("uah")


def _syncer(store, fake_firefly, statements, *, client_info=None):
    mono = FakeMonobankClient(client_info=client_info or {"name": "U", "accounts": []}, statements=statements)
    return Syncer(mono, fake_firefly, store, make_config(), now=lambda: NOW), mono


def test_incremental_creates_new_since_last_synced(store, fake_firefly):
    _mapped(store, last_synced=NOW - 10 * 86400)
    items = [_item("n1", NOW - 2 * 86400, -5000, 95000), _item("n2", NOW - 1 * 86400, -3000, 92000)]
    syncer, mono = _syncer(store, fake_firefly, {"uah": items})
    syncer.incremental_cycle()
    assert fake_firefly.created_tx_count == 2
    assert store.get_account("uah")["last_synced_time"] == NOW - 1 * 86400
    assert store.get_transaction("n1")["status"] == "ok"


def test_incremental_updates_changed_hold_within_overlap(store, fake_firefly):
    _mapped(store, last_synced=NOW - 3600)
    held = _item("h", NOW - 12 * 3600, -7700, 90000, hold=True)
    store.upsert_transaction(mono_tx_id="h", mono_account_id="uah", firefly_tx_id="55",
                             time=held["time"], amount_minor=-7700, balance_minor=90000,
                             hash=mapping.transaction_hash(held), status="ok")
    fake_firefly.transactions["55"] = {"external_id": "h"}
    settled = _item("h", NOW - 12 * 3600, -8000, 89700, hold=False)
    syncer, mono = _syncer(store, fake_firefly, {"uah": [settled]})
    syncer.incremental_cycle()
    assert fake_firefly.updated_tx_count == 1
    assert fake_firefly.created_tx_count == 0
    assert store.get_transaction("h")["hash"] == mapping.transaction_hash(settled)


def test_incremental_chunks_long_offline_gap(store, fake_firefly):
    _mapped(store, last_synced=NOW - 95 * 86400)
    items = [_item(f"g{k}", NOW - (95 - 20 * k) * 86400, -100 * (k + 1), 50000) for k in range(5)]
    syncer, mono = _syncer(store, fake_firefly, {"uah": items})
    syncer.incremental_cycle()
    assert fake_firefly.created_tx_count == 5
    assert len(mono.statement_calls) >= 4  # ~96 days / 30-day windows


def test_incremental_retries_failed_transaction_next_cycle(store, fake_firefly):
    _mapped(store, last_synced=NOW - 5 * 86400)
    fake_firefly.create_failures = 1
    syncer, mono = _syncer(store, fake_firefly, {"uah": [_item("f1", NOW - 1 * 86400, -1000, 99000)]})
    syncer.incremental_cycle()
    assert store.get_transaction("f1")["status"] == "failed"
    syncer.incremental_cycle()
    assert store.get_transaction("f1")["status"] == "ok"
    assert fake_firefly.created_tx_count == 1


def test_reconcile_warns_on_balance_mismatch(store, fake_firefly, caplog):
    _mapped(store, last_synced=NOW, ff_id="100")
    fake_firefly.balances["100"] = 123.45
    ci = {"name": "U", "accounts": [{"id": "uah", "type": "black", "currencyCode": 980, "balance": 100000}]}
    syncer, mono = _syncer(store, fake_firefly, {"uah": []}, client_info=ci)
    with caplog.at_level("WARNING"):
        syncer.incremental_cycle()
    assert "balance mismatch" in caplog.text
    assert store.get_state("last_reconcile_ts") == str(NOW)


def test_reconcile_skipped_when_recent(store, fake_firefly, caplog):
    _mapped(store, last_synced=NOW, ff_id="100")
    store.set_state("last_reconcile_ts", str(NOW - 100))
    fake_firefly.balances["100"] = 123.45
    ci = {"name": "U", "accounts": [{"id": "uah", "type": "black", "currencyCode": 980, "balance": 100000}]}
    syncer, mono = _syncer(store, fake_firefly, {"uah": []}, client_info=ci)
    with caplog.at_level("WARNING"):
        syncer.incremental_cycle()
    assert mono.client_info_calls == 0
    assert "balance mismatch" not in caplog.text
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_sync_incremental.py -q`
Expected: FAIL — `AttributeError: 'Syncer' object has no attribute 'incremental_cycle'`.

- [ ] **Step 3: Add incremental + reconcile methods to `Syncer` in `mono_sync/sync.py`**

Append these methods inside the `Syncer` class (after `_set_opening_balance`):

```python
    # ------------------------------------------------------------------ incremental
    def incremental_cycle(self) -> None:
        for acc in self.store.all_accounts():
            self._incremental_account(acc)
        self._maybe_reconcile()

    def _incremental_account(self, acc) -> None:
        mono_id = acc["mono_account_id"]
        now = self._now()
        last = acc["last_synced_time"] or 0
        # Resume from just before the last seen item (overlap catches late items / hold settlements).
        # On a fresh install with no backfill there is nothing to resume from, so grab the last window.
        start = (last - _OVERLAP_SECONDS) if last else (now - _WINDOW_SECONDS)
        max_seen = last
        created = updated = failed = 0
        a = start
        while a < now:
            b = min(a + _WINDOW_SECONDS, now)
            for it in self._fetch_chunk(mono_id, a, b):
                outcome = self._ingest_item(acc, it)
                created += outcome == "created"
                updated += outcome == "updated"
                failed += outcome == "failed"
                if it["time"] > max_seen:
                    max_seen = it["time"]
            a = b
        if max_seen > last:
            self.store.set_last_synced_time(mono_id, max_seen)
        log.info("incremental account=%s: %d new, %d updated, %d failed", mono_id, created, updated, failed)

    # ------------------------------------------------------------------ balance reconciliation
    def _maybe_reconcile(self) -> None:
        last = self.store.get_state("last_reconcile_ts")
        now = self._now()
        if last is not None and now - int(last) < _RECONCILE_INTERVAL:
            return
        try:
            info = self.mono.client_info()
        except Exception as exc:  # noqa: BLE001 - reconciliation is best-effort
            log.warning("reconcile: client-info failed: %s", exc)
            return
        by_id = {a["id"]: a for a in info.get("accounts", [])}
        for acc in self.store.all_accounts():
            mono_acc = by_id.get(acc["mono_account_id"])
            if mono_acc is None:
                continue
            mono_balance = (mono_acc.get("balance") or 0) / 100
            try:
                ff_balance = self.ff.get_account_balance(acc["firefly_account_id"])
            except FireflyError as exc:
                log.warning("reconcile: Firefly balance failed for %s: %s", acc["firefly_account_id"], exc)
                continue
            if abs(mono_balance - ff_balance) > 0.01:
                log.warning("balance mismatch account=%s: Monobank=%.2f Firefly=%.2f (diff=%.2f)",
                            acc["mono_account_id"], mono_balance, ff_balance, mono_balance - ff_balance)
            else:
                log.info("balance ok account=%s: %.2f", acc["mono_account_id"], mono_balance)
        self.store.set_state("last_reconcile_ts", str(now))
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_sync_incremental.py -q`
Expected: PASS (6 passed).

- [ ] **Step 5: Run the whole suite**

Run: `pytest -q`
Expected: PASS — every test green.

- [ ] **Step 6: Commit**

```bash
git add mono_sync/sync.py tests/test_sync_incremental.py
git commit -m "feat: incremental polling and balance reconciliation"
```

---

## Task 12: `__main__.py` — entrypoint

**Files:**
- Create: `mono_sync/__main__.py`

No unit test — this is the wiring/loop. It is exercised end-to-end by deploying the container (smoke step in Task 16).

- [ ] **Step 1: Write `mono_sync/__main__.py`**

```python
"""Entrypoint: load config, wire clients, run the one-time backfill (if enabled), then poll forever."""
from __future__ import annotations

import logging
import time

from .config import load_config
from .firefly import FireflyClient
from .monobank import MonobankClient, RateLimiter
from .store import Store
from .sync import Syncer

_MONOBANK_MIN_INTERVAL = 65.0  # seconds; Monobank's limit is 1 req / 60 s — keep a safety margin


def _setup_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


def main() -> None:
    cfg = load_config()
    _setup_logging(cfg.log_level)
    log = logging.getLogger("mono_sync")
    log.info("starting; poll=%dm backfill=%s floor=%s tz=%s",
             cfg.poll_interval_minutes, cfg.backfill, cfg.backfill_floor_date, cfg.timezone)

    store = Store(cfg.db_path)
    limiter = RateLimiter(_MONOBANK_MIN_INTERVAL)
    mono = MonobankClient(cfg.monobank_token, limiter)
    firefly = FireflyClient(cfg.firefly_url, cfg.firefly_token)
    syncer = Syncer(mono, firefly, store, cfg)

    syncer.ensure_accounts()
    if cfg.backfill:
        syncer.run_backfill()

    interval = cfg.poll_interval_minutes * 60
    log.info("entering incremental loop")
    while True:
        try:
            syncer.incremental_cycle()
        except Exception:  # noqa: BLE001 - keep the loop alive; details are logged
            log.exception("incremental cycle failed; retrying after interval")
        time.sleep(interval)


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Smoke-check it imports and shows config errors cleanly**

Run (PowerShell): `python -m mono_sync`
Expected: it exits with `RuntimeError: missing required environment variable: MONOBANK_TOKEN` (because no env is set). That confirms wiring is intact. (Do **not** run it with real tokens yet — that happens after deploy.)

- [ ] **Step 3: Commit**

```bash
git add mono_sync/__main__.py
git commit -m "feat: service entrypoint (backfill then poll loop)"
```

---

## Task 13: `Dockerfile` and `.dockerignore`

**Files:**
- Create: `Dockerfile`, `.dockerignore`

- [ ] **Step 1: Write `Dockerfile`**

```dockerfile
FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY mono_sync ./mono_sync

ENV PYTHONUNBUFFERED=1
VOLUME ["/data"]

ENTRYPOINT ["python", "-m", "mono_sync"]
```

- [ ] **Step 2: Write `.dockerignore`**

```text
.git
.github
docs
tests
.planning
__pycache__
*.pyc
*.db
*.sqlite3
.env
.env.*
.venv
venv
.pytest_cache
.idea
.vscode
```

- [ ] **Step 3: Build locally to verify (optional but recommended)**

Run (PowerShell, if Docker Desktop is available): `docker build -t mono_sync:test .`
Expected: build succeeds. (If Docker is not available locally, skip — CI in Task 15 will build it.)

- [ ] **Step 4: Commit**

```bash
git add Dockerfile .dockerignore
git commit -m "build: Dockerfile and dockerignore"
```

---

## Task 14: `docker-compose.yml` and `.env.example`

**Files:**
- Create: `docker-compose.yml`, `.env.example`

- [ ] **Step 1: Write `docker-compose.yml`**

```yaml
services:
  monobank-sync:
    image: ghcr.io/vatsonio/mono_sync:latest
    container_name: monobank-firefly-sync
    restart: unless-stopped
    environment:
      MONOBANK_TOKEN: ${MONOBANK_TOKEN}
      FIREFLY_URL: ${FIREFLY_URL}
      FIREFLY_TOKEN: ${FIREFLY_TOKEN}
      POLL_INTERVAL_MINUTES: ${POLL_INTERVAL_MINUTES:-5}
      BACKFILL: ${BACKFILL:-true}
      BACKFILL_FLOOR_DATE: ${BACKFILL_FLOOR_DATE:-2023-05-01}
      MCC_CATEGORIES: ${MCC_CATEGORIES:-false}
      TZ: ${TZ:-Europe/Kyiv}
      LOG_LEVEL: ${LOG_LEVEL:-info}
    volumes:
      - monobank-sync-data:/data
    networks:
      - firefly

volumes:
  monobank-sync-data:

networks:
  firefly:
    external: true
    name: ${FIREFLY_NETWORK:-firefly_default}
```

- [ ] **Step 2: Write `.env.example`**

```text
# Monobank personal API token (from https://api.monobank.ua/ — scan the QR with the Monobank app)
MONOBANK_TOKEN=replace-me

# Firefly III: internal container URL (no trailing /api/v1) and a Personal Access Token
# (Firefly III -> Options -> Profile -> OAuth -> Create new token).
# Find the container name with: docker ps  (e.g. the Firefly app container) — then http://<name>:8080
FIREFLY_URL=http://app:8080
FIREFLY_TOKEN=replace-me

# Docker network that the Firefly III stack is attached to. Find it with: docker network ls
FIREFLY_NETWORK=firefly_default

# Behaviour
POLL_INTERVAL_MINUTES=5
BACKFILL=true
BACKFILL_FLOOR_DATE=2023-05-01
MCC_CATEGORIES=false
TZ=Europe/Kyiv
LOG_LEVEL=info
```

- [ ] **Step 3: Commit**

```bash
git add docker-compose.yml .env.example
git commit -m "build: Portainer stack compose file and env example"
```

---

## Task 15: GitHub Actions — test then build & push to GHCR

**Files:**
- Create: `.github/workflows/build.yml`

- [ ] **Step 1: Write `.github/workflows/build.yml`**

```yaml
name: build-and-push

on:
  push:
    branches: [main]
  workflow_dispatch:

permissions:
  contents: read
  packages: write

jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"
      - run: pip install -r requirements.txt pytest
      - run: pytest -q

  build:
    needs: test
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: docker/setup-qemu-action@v3
      - uses: docker/setup-buildx-action@v3
      - uses: docker/login-action@v3
        with:
          registry: ghcr.io
          username: ${{ github.actor }}
          password: ${{ secrets.GITHUB_TOKEN }}
      - uses: docker/metadata-action@v5
        id: meta
        with:
          images: ghcr.io/${{ github.repository }}
          tags: |
            type=raw,value=latest
            type=sha
      - uses: docker/build-push-action@v6
        with:
          context: .
          platforms: linux/amd64,linux/arm64
          push: true
          tags: ${{ steps.meta.outputs.tags }}
          labels: ${{ steps.meta.outputs.labels }}
```

- [ ] **Step 2: Commit**

```bash
git add .github/workflows/build.yml
git commit -m "ci: test then build multi-arch image and push to GHCR"
```

> After this is pushed (final task), check the Actions tab: the `build-and-push` workflow should go green and publish `ghcr.io/vatsonio/mono_sync:latest`. Then, on GitHub: **Packages → mono_sync → Package settings → Change visibility → Public** so the Raspberry Pi can pull it without credentials. (Alternatively keep it private and add a GHCR registry credential in Portainer.)

---

## Task 16: `README.md`

**Files:**
- Create: `README.md`

- [ ] **Step 1: Write `README.md`**

````markdown
# mono_sync — Monobank → Firefly III

Keeps a Firefly III asset account fully in sync with a Monobank **black** card: one-time import of the available history, then automatic polling for new transactions. Runs as a small container (designed for a Raspberry Pi 4 + Portainer + Firefly III). No inbound ports, no webhooks.

## How it works

1. On start it reads `client-info` from the Monobank personal API, finds the `black` account(s), and creates a matching asset account in Firefly III (`Monobank Black`, plus `Monobank Black USD`/`EUR`/… if the card has extra currency accounts).
2. **Backfill** (once): walks 30-day windows backwards to `BACKFILL_FLOOR_DATE`, ~1 Monobank request per minute (Monobank caps statement calls at 1/60 s). On a few years of history this takes roughly half an hour; it is resumable.
3. **Incremental** (every `POLL_INTERVAL_MINUTES`): fetches new statement items (with a 24 h overlap to catch late items and `hold` → settled changes) and upserts them into Firefly. Hourly, it compares the Monobank balance with Firefly's computed balance and logs a warning on mismatch.
4. Idempotency: every Firefly transaction carries `external_id = Monobank transaction id`; a SQLite file on the `/data` volume tracks what has been synced.

All payments map to a single shared Firefly expense/revenue account named **Monobank** (the merchant/counterparty name goes in the transaction description). MCC codes go into the transaction notes plus a `monobank` tag — use Firefly's own rules to categorise. Set `MCC_CATEGORIES=true` to also apply a small built-in MCC→category map.

## Prerequisites

- **Monobank token:** open <https://api.monobank.ua/>, scan the QR with the Monobank app, copy the token. (Personal tokens do not expire unless you revoke them in the app.)
- **Firefly III Personal Access Token:** Firefly III → Options → Profile → OAuth → *Create new token*.
- **`FIREFLY_URL`:** the internal address of the Firefly III container on its Docker network, e.g. `http://app:8080`. Find the container name with `docker ps`.
- **`FIREFLY_NETWORK`:** the Docker network the Firefly III stack is attached to. Find it with `docker network ls` (often `<stackname>_default`).

## Deploy with Portainer

1. In Portainer: **Stacks → Add stack**, paste the contents of [`docker-compose.yml`](docker-compose.yml).
2. In the **Environment variables** section add: `MONOBANK_TOKEN`, `FIREFLY_TOKEN`, `FIREFLY_URL`, `FIREFLY_NETWORK`, and optionally `POLL_INTERVAL_MINUTES`, `BACKFILL`, `BACKFILL_FLOOR_DATE`, `MCC_CATEGORIES`, `TZ`, `LOG_LEVEL` (see [`.env.example`](.env.example)).
3. **Deploy**. Watch the container logs in Portainer — you should see account setup, then `backfill window …` lines, then `entering incremental loop`.

During backfill the asset account's balance in Firefly will look wrong (it climbs from 0 as old transactions are added); it self-corrects when backfill finishes and the opening balance is set.

## Updating

Push to `main` → GitHub Actions rebuilds and publishes `ghcr.io/vatsonio/mono_sync:latest` → in Portainer open the stack and **Pull and redeploy**. State on the `/data` volume is preserved, so backfill is not repeated.

## Development

```bash
pip install -r requirements.txt pytest
pytest -q
```

`tzdata` (in `requirements.txt`) is required for `zoneinfo` to work on machines without an IANA tz database (Windows, slim containers).
````

- [ ] **Step 2: Commit**

```bash
git add README.md
git commit -m "docs: README with setup and deployment guide"
```

---

## Final: full test run, push, post-deploy checklist

- [ ] **Step 1: Run the entire suite**

Run: `pytest -q`
Expected: all tests pass.

- [ ] **Step 2: Push**

```bash
git push origin main
```

- [ ] **Step 3: Post-deploy (manual, by the user)**

1. Wait for the `build-and-push` workflow to go green; make the GHCR package **public** (or add a registry credential in Portainer).
2. Create the Firefly III Personal Access Token; find `FIREFLY_URL` (`docker ps`) and `FIREFLY_NETWORK` (`docker network ls`).
3. Deploy the stack in Portainer with the env vars filled in.
4. Watch logs: account setup → backfill windows → `entering incremental loop`. After the first hourly reconcile, confirm `balance ok account=…` (not `balance mismatch`). If there is a mismatch, the Monobank history was probably truncated before `BACKFILL_FLOOR_DATE` — adjust the asset account's opening balance in Firefly manually.

---

## Self-Review notes (for the plan author / reviewer)

- **Spec coverage:** §2 decisions → Tasks 9 (single shared `Monobank` account, MCC-in-notes, all `black` accounts), 12 (`__main__` wiring), 14–15 (GHCR/Portainer). §4 Monobank/Firefly APIs → Tasks 6–7. §5.1 backfill (30-day windows, ≥65 s spacing, 500-split, cursor resume, opening balance) → Task 10. §5.2 incremental (24 h overlap, hold updates, hourly reconcile) → Task 11. §6 mapping (sign, FX, hold tag, notes) → Task 3. §7 state schema → Task 4. §8 error handling (429/5xx backoff, 403 surfaced, per-tx failure recorded & retried, resumability) → Tasks 6, 9, 11. §9 deployment artifacts → Tasks 13–16. §10 tests → Tasks 2–11 test files. §11 out-of-scope items are intentionally absent. §12 open questions (`FIREFLY_URL`, network name) are surfaced in `.env.example` and README.
- **Layout deviation:** flat `mono_sync/` instead of `src/mono_sync/` — noted in the header.
- **Type/name consistency:** `Syncer._ingest_item` returns `'created'|'updated'|'skipped'|'failed'` and is used that way in `_backfill_account` and `_incremental_account`. `FakeFireflyClient`/`FakeMonobankClient` method signatures match the real clients consumed by `Syncer`. `Config` field list is identical in `config.py`, `make_config`, and all `_cfg`-style helpers.


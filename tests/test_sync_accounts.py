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

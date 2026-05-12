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


def test_raw_json_round_trips_and_is_preserved(store):
    store.upsert_transaction(mono_tx_id="t1", mono_account_id="a", firefly_tx_id="100",
                             time=1000, amount_minor=-500, balance_minor=9500, hash="h1",
                             raw_json='{"id":"t1","amount":-500}')
    assert store.get_transaction("t1")["raw_json"] == '{"id":"t1","amount":-500}'
    # upsert again without raw_json -> the stored JSON is kept (COALESCE)
    store.upsert_transaction(mono_tx_id="t1", mono_account_id="a", firefly_tx_id="100",
                             time=1000, amount_minor=-500, balance_minor=9500, hash="h1", status="failed")
    assert store.get_transaction("t1")["raw_json"] == '{"id":"t1","amount":-500}'


def test_failed_transactions(store):
    store.upsert_transaction(mono_tx_id="ok1", mono_account_id="a", firefly_tx_id="1",
                             time=200, amount_minor=1, balance_minor=1, hash="h", status="ok")
    store.upsert_transaction(mono_tx_id="bad2", mono_account_id="a", firefly_tx_id=None,
                             time=100, amount_minor=2, balance_minor=2, hash="h", status="failed", raw_json="{}")
    store.upsert_transaction(mono_tx_id="bad1", mono_account_id="a", firefly_tx_id=None,
                             time=50, amount_minor=3, balance_minor=3, hash="h", status="failed", raw_json="{}")
    failed = store.failed_transactions()
    assert [r["mono_tx_id"] for r in failed] == ["bad1", "bad2"]  # ordered by time ASC

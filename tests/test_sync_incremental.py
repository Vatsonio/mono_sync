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

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
    assert ff_acc["opening_balance_date"] < "2023-08-08"  # NOW-100d ≈ 2023-08-07; opening = day before


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

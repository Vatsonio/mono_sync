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

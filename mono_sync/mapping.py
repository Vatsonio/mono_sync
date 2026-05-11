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

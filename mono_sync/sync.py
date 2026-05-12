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

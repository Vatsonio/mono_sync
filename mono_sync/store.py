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
    status          TEXT NOT NULL DEFAULT 'ok',
    raw_json        TEXT
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
        self._migrate()
        self._conn.commit()

    def _migrate(self) -> None:
        # Add columns introduced after the initial schema. ALTER TABLE ADD COLUMN is a no-op-safe
        # migration; OperationalError means the column already exists.
        try:
            self._conn.execute("ALTER TABLE transactions ADD COLUMN raw_json TEXT")
        except sqlite3.OperationalError:
            pass

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
                           amount_minor, balance_minor, hash, status="ok", raw_json=None) -> None:
        self._conn.execute(
            """INSERT INTO transactions (mono_tx_id, mono_account_id, firefly_tx_id, time,
                                         amount_minor, balance_minor, hash, status, raw_json)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(mono_tx_id) DO UPDATE SET
                   mono_account_id = excluded.mono_account_id,
                   firefly_tx_id   = excluded.firefly_tx_id,
                   time            = excluded.time,
                   amount_minor    = excluded.amount_minor,
                   balance_minor   = excluded.balance_minor,
                   hash            = excluded.hash,
                   status          = excluded.status,
                   raw_json        = COALESCE(excluded.raw_json, transactions.raw_json)""",
            (mono_tx_id, mono_account_id, firefly_tx_id, int(time), int(amount_minor),
             None if balance_minor is None else int(balance_minor), hash, status, raw_json),
        )
        self._conn.commit()

    def oldest_transaction(self, mono_account_id):
        return self._conn.execute(
            "SELECT * FROM transactions WHERE mono_account_id = ? ORDER BY time ASC LIMIT 1",
            (mono_account_id,),
        ).fetchone()

    def failed_transactions(self) -> list:
        return list(self._conn.execute(
            "SELECT * FROM transactions WHERE status = 'failed' ORDER BY time ASC"
        ))

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

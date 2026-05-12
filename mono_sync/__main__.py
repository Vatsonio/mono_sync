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
    log.info("starting; poll=%dm backfill=%s floor=%s tz=%s firefly_timeout=%ds apply_rules=%s",
             cfg.poll_interval_minutes, cfg.backfill, cfg.backfill_floor_date, cfg.timezone,
             cfg.firefly_timeout, cfg.firefly_apply_rules)

    store = Store(cfg.db_path)
    limiter = RateLimiter(_MONOBANK_MIN_INTERVAL)
    mono = MonobankClient(cfg.monobank_token, limiter)
    firefly = FireflyClient(cfg.firefly_url, cfg.firefly_token,
                            timeout=cfg.firefly_timeout, apply_rules=cfg.firefly_apply_rules)
    syncer = Syncer(mono, firefly, store, cfg)

    try:
        syncer.ensure_accounts()
        if cfg.backfill:
            syncer.run_backfill()
    except Exception:  # noqa: BLE001 - never crash-loop on startup; the incremental loop picks up the rest
        log.exception("account setup / backfill failed; proceeding to incremental loop anyway")

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

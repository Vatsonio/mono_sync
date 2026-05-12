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
    firefly_timeout: int
    firefly_apply_rules: bool
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

    try:
        ff_timeout = int(env.get("FIREFLY_TIMEOUT_SECONDS", "60"))
    except ValueError as exc:
        raise RuntimeError(f"FIREFLY_TIMEOUT_SECONDS must be an integer: {exc}") from exc
    if ff_timeout < 1:
        raise RuntimeError("FIREFLY_TIMEOUT_SECONDS must be >= 1")

    floor = (env.get("BACKFILL_FLOOR_DATE") or "2023-05-01").strip()
    try:
        datetime.strptime(floor, "%Y-%m-%d")
    except ValueError as exc:
        raise RuntimeError(f"BACKFILL_FLOOR_DATE must be YYYY-MM-DD: {exc}") from exc

    return Config(
        monobank_token=required("MONOBANK_TOKEN"),
        firefly_url=required("FIREFLY_URL").rstrip("/"),
        firefly_token=required("FIREFLY_TOKEN"),
        firefly_timeout=ff_timeout,
        firefly_apply_rules=_as_bool(env.get("FIREFLY_APPLY_RULES", "true")),
        poll_interval_minutes=poll,
        backfill=_as_bool(env.get("BACKFILL", "true")),
        backfill_floor_date=floor,
        mcc_categories=_as_bool(env.get("MCC_CATEGORIES", "false")),
        timezone=(env.get("TZ") or "Europe/Kyiv").strip() or "Europe/Kyiv",
        log_level=(env.get("LOG_LEVEL") or "info").strip() or "info",
        db_path=(env.get("DB_PATH") or "/data/state.db").strip() or "/data/state.db",
    )

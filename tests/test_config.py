import pytest

from mono_sync.config import load_config


def _env(**over):
    base = {"MONOBANK_TOKEN": "m-token", "FIREFLY_URL": "http://app:8080/", "FIREFLY_TOKEN": "f-token"}
    base.update(over)
    return base


def test_defaults():
    cfg = load_config(_env())
    assert cfg.monobank_token == "m-token"
    assert cfg.firefly_url == "http://app:8080"  # trailing slash stripped
    assert cfg.firefly_token == "f-token"
    assert cfg.poll_interval_minutes == 5
    assert cfg.backfill is True
    assert cfg.backfill_floor_date == "2023-05-01"
    assert cfg.mcc_categories is False
    assert cfg.timezone == "Europe/Kyiv"
    assert cfg.log_level == "info"
    assert cfg.db_path == "/data/state.db"


def test_overrides():
    cfg = load_config(_env(POLL_INTERVAL_MINUTES="15", BACKFILL="false", MCC_CATEGORIES="yes",
                           BACKFILL_FLOOR_DATE="2020-01-01", LOG_LEVEL="debug", DB_PATH="/tmp/x.db"))
    assert cfg.poll_interval_minutes == 15
    assert cfg.backfill is False
    assert cfg.mcc_categories is True
    assert cfg.backfill_floor_date == "2020-01-01"
    assert cfg.log_level == "debug"
    assert cfg.db_path == "/tmp/x.db"


def test_missing_required():
    with pytest.raises(RuntimeError, match="MONOBANK_TOKEN"):
        load_config({"FIREFLY_URL": "x", "FIREFLY_TOKEN": "y"})


def test_bad_floor_date():
    with pytest.raises(RuntimeError, match="BACKFILL_FLOOR_DATE"):
        load_config(_env(BACKFILL_FLOOR_DATE="2020/01/01"))


def test_bad_poll_interval():
    with pytest.raises(RuntimeError, match="POLL_INTERVAL_MINUTES"):
        load_config(_env(POLL_INTERVAL_MINUTES="0"))

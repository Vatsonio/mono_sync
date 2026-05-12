"""Monobank personal API client with a conservative global rate limiter."""
from __future__ import annotations

import logging
import threading
import time

import requests

log = logging.getLogger(__name__)

_BASE = "https://api.monobank.ua"
_MAX_RETRIES = 5


class RateLimiter:
    """Ensures at least ``min_interval`` seconds between successive ``wait()`` calls."""

    def __init__(self, min_interval: float, *, monotonic=time.monotonic, sleep=time.sleep):
        self._min = float(min_interval)
        self._monotonic = monotonic
        self._sleep = sleep
        self._last: float | None = None
        self._lock = threading.Lock()

    def wait(self) -> None:
        with self._lock:
            now = self._monotonic()
            if self._last is not None:
                gap = now - self._last
                if gap < self._min:
                    self._sleep(self._min - gap)
                    now = self._monotonic()
            self._last = now


class MonobankError(Exception):
    pass


class MonobankClient:
    def __init__(self, token: str, limiter: RateLimiter, *, session: requests.Session | None = None, sleep=time.sleep):
        self._token = token
        self._limiter = limiter
        self._session = session or requests.Session()
        self._sleep = sleep

    def _get(self, path: str):
        url = _BASE + path
        for attempt in range(1, _MAX_RETRIES + 1):
            self._limiter.wait()
            try:
                resp = self._session.get(url, headers={"X-Token": self._token}, timeout=30)
            except requests.RequestException as exc:
                log.warning("monobank request error on %s (%d/%d): %s", path, attempt, _MAX_RETRIES, exc)
                self._sleep(min(60 * attempt, 300))
                continue
            if resp.status_code == 200:
                return resp.json()
            if resp.status_code == 429 or resp.status_code >= 500:
                log.warning("monobank %s on %s (%d/%d); backing off", resp.status_code, path, attempt, _MAX_RETRIES)
                self._sleep(60)
                continue
            raise MonobankError(f"monobank GET {path} -> {resp.status_code}: {resp.text[:300]}")
        raise MonobankError(f"monobank GET {path} failed after {_MAX_RETRIES} attempts")

    def client_info(self) -> dict:
        return self._get("/personal/client-info")

    def statement(self, account_id: str, frm: int, to: int) -> list:
        return self._get(f"/personal/statement/{account_id}/{int(frm)}/{int(to)}")

import pytest

from mono_sync.monobank import MonobankClient, MonobankError, RateLimiter
from tests.fakes import FakeResponse, FakeSession


def test_rate_limiter_first_call_no_sleep():
    slept = []
    rl = RateLimiter(65.0, monotonic=lambda: 0.0, sleep=slept.append)
    rl.wait()
    assert slept == []


def test_rate_limiter_sleeps_to_maintain_interval():
    clock = [0.0]
    slept = []

    def sleep(seconds):
        slept.append(seconds)
        clock[0] += seconds

    rl = RateLimiter(65.0, monotonic=lambda: clock[0], sleep=sleep)
    rl.wait()                 # t=0, first call, no sleep
    clock[0] = 10.0           # only 10s passed
    rl.wait()                 # must sleep 55s
    assert slept == [55.0]
    clock[0] = 1000.0         # plenty of time passed
    rl.wait()                 # no extra sleep
    assert slept == [55.0]


def test_client_info_returns_json():
    session = FakeSession([FakeResponse(200, json_data={"name": "A", "accounts": []})])
    client = MonobankClient("tok", RateLimiter(0.0), session=session, sleep=lambda s: None)
    assert client.client_info() == {"name": "A", "accounts": []}
    method, url, kwargs = session.calls[0]
    assert url.endswith("/personal/client-info")
    assert kwargs["headers"]["X-Token"] == "tok"


def test_statement_builds_url():
    session = FakeSession([FakeResponse(200, json_data=[{"id": "x"}])])
    client = MonobankClient("tok", RateLimiter(0.0), session=session, sleep=lambda s: None)
    assert client.statement("acc-1", 100, 200) == [{"id": "x"}]
    assert session.calls[0][1].endswith("/personal/statement/acc-1/100/200")


def test_retries_on_429_then_succeeds():
    session = FakeSession([FakeResponse(429, text="rate"), FakeResponse(200, json_data={"ok": True})])
    sleeps = []
    client = MonobankClient("tok", RateLimiter(0.0), session=session, sleep=sleeps.append)
    assert client.client_info() == {"ok": True}
    assert sleeps == [60]     # backed off once


def test_raises_on_4xx():
    session = FakeSession([FakeResponse(403, text="forbidden")])
    client = MonobankClient("tok", RateLimiter(0.0), session=session, sleep=lambda s: None)
    with pytest.raises(MonobankError, match="403"):
        client.client_info()

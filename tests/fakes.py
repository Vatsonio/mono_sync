"""In-memory fakes used by the test suite."""
from __future__ import annotations


class FakeResponse:
    def __init__(self, status_code: int, *, json_data=None, text: str = ""):
        self.status_code = status_code
        self._json = json_data
        self.text = text
        self.content = b"x" if (text or json_data is not None) else b""

    def json(self):
        return self._json


class FakeSession:
    """Returns the queued responses in order; records every call."""

    def __init__(self, responses: list[FakeResponse]):
        self._responses = list(responses)
        self.calls: list[tuple] = []

    def get(self, url, **kwargs):
        self.calls.append(("GET", url, kwargs))
        return self._responses.pop(0)

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        return self._responses.pop(0)

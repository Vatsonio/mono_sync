import pytest

from mono_sync.store import Store


@pytest.fixture
def store():
    s = Store(":memory:")
    yield s
    s.close()

from tests.fakes import FakeFireflyClient


@pytest.fixture
def fake_firefly():
    return FakeFireflyClient()

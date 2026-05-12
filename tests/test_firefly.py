import pytest
import requests

from mono_sync.firefly import FireflyClient, FireflyError
from tests.fakes import FakeResponse, FakeSession


def _client(responses, **over):
    kwargs = dict(session=FakeSession(responses), sleep=lambda _s: None)
    kwargs.update(over)
    return FireflyClient("http://app:8080/", "ff-token", **kwargs)


def test_request_sets_auth_header():
    session = FakeSession([FakeResponse(200, json_data={"data": []})])
    FireflyClient("http://app:8080", "ff-token", session=session).find_transaction_by_external_id("x")
    _, url, kwargs = session.calls[0]
    assert kwargs["headers"]["Authorization"] == "Bearer ff-token"
    assert url == "http://app:8080/api/v1/search/transactions"


def test_request_raises_on_error_with_body():
    client = _client([FakeResponse(422, text='{"message":"bad"}')])
    with pytest.raises(FireflyError, match="422"):
        client.create_transaction({"type": "withdrawal"})


def test_find_asset_account_matches_name_across_pages():
    page1 = FakeResponse(200, json_data={
        "data": [{"id": "1", "attributes": {"name": "Other"}}],
        "meta": {"pagination": {"total_pages": 2}},
    })
    page2 = FakeResponse(200, json_data={
        "data": [{"id": "7", "attributes": {"name": "Monobank Black"}}],
        "meta": {"pagination": {"total_pages": 2}},
    })
    found = _client([page1, page2]).find_asset_account("Monobank Black")
    assert found["id"] == "7"


def test_find_asset_account_returns_none_when_absent():
    page = FakeResponse(200, json_data={"data": [], "meta": {"pagination": {"total_pages": 1}}})
    assert _client([page]).find_asset_account("Nope") is None


def test_create_asset_account_body():
    session = FakeSession([FakeResponse(200, json_data={"data": {"id": "9", "attributes": {"name": "Monobank Black"}}})])
    client = FireflyClient("http://app:8080", "t", session=session)
    out = client.create_asset_account(name="Monobank Black", currency_code="UAH",
                                      opening_balance="0", opening_balance_date="2023-05-01", notes="n")
    assert out["id"] == "9"
    body = session.calls[0][2]["json"]
    assert body["type"] == "asset"
    assert body["account_role"] == "defaultAsset"
    assert body["currency_code"] == "UAH"
    assert body["opening_balance"] == "0"
    assert body["opening_balance_date"] == "2023-05-01"


def test_create_transaction_wraps_payload():
    session = FakeSession([FakeResponse(200, json_data={"data": {"id": "55", "attributes": {}}})])
    client = FireflyClient("http://app:8080", "t", session=session)
    out = client.create_transaction({"type": "withdrawal", "amount": "1.00"})
    assert out["id"] == "55"
    body = session.calls[0][2]["json"]
    assert body["error_if_duplicate_hash"] is False
    assert body["apply_rules"] is True
    assert body["fire_webhooks"] is False
    assert body["transactions"] == [{"type": "withdrawal", "amount": "1.00"}]


def test_update_transaction_fetches_journal_id_then_puts():
    get_resp = FakeResponse(200, json_data={"data": {"id": "55", "attributes": {"transactions": [{"transaction_journal_id": "777"}]}}})
    put_resp = FakeResponse(200, json_data={"data": {"id": "55", "attributes": {}}})
    session = FakeSession([get_resp, put_resp])
    client = FireflyClient("http://app:8080", "t", session=session)
    client.update_transaction("55", {"type": "withdrawal", "amount": "2.00"})
    assert session.calls[0][0] == "GET"
    assert session.calls[1][0] == "PUT"
    put_body = session.calls[1][2]["json"]
    assert put_body["transactions"][0]["transaction_journal_id"] == "777"
    assert put_body["transactions"][0]["amount"] == "2.00"


def test_get_account_balance():
    session = FakeSession([FakeResponse(200, json_data={"data": {"id": "9", "attributes": {"current_balance": "123.45"}}})])
    client = FireflyClient("http://app:8080", "t", session=session)
    assert client.get_account_balance("9") == pytest.approx(123.45)


def test_request_retries_on_network_error_then_succeeds():
    session = FakeSession([requests.exceptions.ReadTimeout("slow"),
                           FakeResponse(200, json_data={"data": []})])
    out = FireflyClient("http://app:8080", "t", session=session, sleep=lambda _s: None).find_transaction_by_external_id("x")
    assert out is None  # data was empty -> None
    assert len(session.calls) == 2  # retried once


def test_request_wraps_persistent_network_error_in_firefly_error():
    session = FakeSession([requests.exceptions.ConnectionError("down")] * 3)
    client = FireflyClient("http://app:8080", "t", session=session, sleep=lambda _s: None)
    with pytest.raises(FireflyError, match="failed after 3 attempts"):
        client.get_account_balance("9")
    assert len(session.calls) == 3


def test_request_retries_on_5xx_then_succeeds():
    session = FakeSession([FakeResponse(503, text="busy"),
                           FakeResponse(200, json_data={"data": {"id": "9", "attributes": {"current_balance": "1.00"}}})])
    bal = FireflyClient("http://app:8080", "t", session=session, sleep=lambda _s: None).get_account_balance("9")
    assert bal == pytest.approx(1.00)
    assert len(session.calls) == 2


def test_request_does_not_retry_4xx():
    session = FakeSession([FakeResponse(422, text="bad")])
    client = FireflyClient("http://app:8080", "t", session=session, sleep=lambda _s: None)
    with pytest.raises(FireflyError, match="422"):
        client.create_transaction({"type": "withdrawal"})
    assert len(session.calls) == 1  # no retry on a 4xx


def test_apply_rules_can_be_disabled():
    session = FakeSession([FakeResponse(200, json_data={"data": {"id": "1", "attributes": {}}})])
    client = FireflyClient("http://app:8080", "t", session=session, apply_rules=False)
    client.create_transaction({"type": "withdrawal", "amount": "1.00"})
    assert session.calls[0][2]["json"]["apply_rules"] is False

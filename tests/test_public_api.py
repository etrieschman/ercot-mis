"""The Public API client against a scripted server: no network, no real credentials."""

from datetime import datetime, timedelta, timezone

import pytest

from ercot_mis.products import get_product
from ercot_mis.sources import public_api, retry
from ercot_mis.sources.public_api import ARCHIVE_URL, TOKEN_URL, PublicApiClient, PublicApiError

PRODUCT = get_product("NP4-191-CD")
SECRETS = ("user@example.org", "pass-" + "w" * 8, "key-" + "k" * 8)


class Reply:
    def __init__(self, status=200, body=None, chunks=()):
        self.status_code, self._body, self._chunks = status, body, chunks

    def json(self):
        return self._body

    def iter_content(self, size):
        yield from self._chunks

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        pass


class Server:
    """Answers from a script and records every call."""

    def __init__(self, replies):
        self.replies, self.calls, self.tokens = list(replies), [], 0

    def post(self, url, data, timeout):
        assert url == TOKEN_URL and data["grant_type"] == "password" and data["response_type"] == "id_token"
        self.tokens += 1
        return Reply(body={"id_token": f"token{self.tokens}"})

    def get(self, url, params, stream, timeout, headers):
        self.calls.append((url, params, headers))
        return self.replies.pop(0)


def page(ids, pages=1):
    return Reply(body={"_meta": {"totalPages": pages}, "archives": [
        {"docId": i, "friendlyName": f"doc{i}", "postDatetime": f"2026-01-0{i}T06:00:00",
         "_links": {"endpoint": {"href": f"{ARCHIVE_URL}/np4-191-cd?download={i}"}}} for i in ids]})


def client(replies):
    server = Server(replies)
    return PublicApiClient(*SECRETS, session=server, clock=lambda: 0.0, sleep=lambda s: None), server


@pytest.fixture(autouse=True)
def no_waiting(monkeypatch):
    monkeypatch.setattr(retry, "DELAYS", (0.0, 0.0))


def test_listing_follows_pages_and_sends_both_credentials():
    api, server = client([page([2, 3], pages=2), page([1], pages=2)])
    docs = api.list_documents(PRODUCT, datetime(2026, 1, 1, 6, tzinfo=timezone.utc))
    assert [d.doc_id for d in docs] == ["1", "2", "3"]
    assert docs[0].posted_at.utcoffset() == timedelta(hours=-6) and docs[0].size_bytes == 0 and docs[0].url.endswith("download=1")
    assert [c[1]["page"] for c in server.calls] == [1, 2]
    assert server.calls[0][1]["postDatetimeFrom"] == "2026-01-01T00:00:00"  # ERCOT local time
    assert server.calls[0][2] == {"Authorization": "Bearer token1", "Ocp-Apim-Subscription-Key": SECRETS[2]}
    assert server.tokens == 1


def test_an_expired_token_is_renewed_once_and_throttling_is_retried():
    api, server = client([Reply(401), Reply(429), page([1])])
    assert len(api.list_documents(PRODUCT)) == 1
    assert server.tokens == 2 and len(server.calls) == 3


def test_a_refusal_reports_the_status_and_no_credential():
    api, _ = client([Reply(403)])
    with pytest.raises(PublicApiError) as error:
        api.list_documents(PRODUCT)
    assert "403" in str(error.value) and not any(s in str(error.value) + repr(api) for s in SECRETS)


def test_download_streams_the_document():
    api, _ = client([Reply(chunks=(b"ab", b"c"))])
    assert b"".join(api.download(f"{ARCHIVE_URL}/np4-191-cd?download=1")) == b"abc"


def test_calls_are_spaced_to_the_rate_limit():
    waits, now = [], [0.0]
    server = Server([page([1]), page([2])])
    api = PublicApiClient(*SECRETS, session=server, clock=lambda: now[0], sleep=lambda s: (waits.append(s), now.__setitem__(0, now[0] + s)))
    api.list_documents(PRODUCT)
    api.list_documents(PRODUCT)
    assert waits == [public_api.MIN_INTERVAL]


def test_missing_secrets_say_which(monkeypatch):
    monkeypatch.setattr(public_api, "load_secret", lambda name: None)
    with pytest.raises(public_api.ConfigError, match="ERCOT_PUBLIC_API_USERNAME"):
        PublicApiClient.from_secrets()

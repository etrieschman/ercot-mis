"""ERCOT's Public API: the archive of public reports, as files.

The Public API (``api.ercot.com``) serves two things per product: JSON rows, and an
archive of the documents ERCOT posted. ercot-mis takes the archive only, so a public
product arrives the same way an EWS one does: original bytes, content-addressed.
Unlike EWS it keeps history past the MIS display window.

Access needs three values (``load_secret``: environment, ``.env``, then the Keychain):
the account's user name and password, traded for an ID token that lasts an hour, and
the subscription key sent with every call. None of them ever reaches a log or an
error message.

The archive listing gives a document ID, a posting time and a download link; it gives
no size, report group or operating date, so those stay empty in the catalog and the
size check on download is skipped.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterator
from datetime import datetime, timedelta, timezone

import requests

from ..config import ConfigError, load_secret
from ..products import Product
from .ews import ERCOT_TZ, RemoteDoc
from .retry import retrying

TOKEN_URL = "https://ercotb2c.b2clogin.com/ercotb2c.onmicrosoft.com/B2C_1_PUBAPI-ROPC-FLOW/oauth2/v2.0/token"
# The public client of ERCOT's B2C tenant, from the Public API user guide; not a secret.
CLIENT_ID = "fec253ea-0d06-4272-a5e6-b478baeecd70"
ARCHIVE_URL = "https://api.ercot.com/api/public-reports/archive"

TOKEN_LIFETIME = timedelta(minutes=55)  # ERCOT's tokens last an hour
PAGE_SIZE = 1000
MIN_INTERVAL = 2.1  # seconds between calls: the API allows 30 a minute
CHUNK = 1 << 20


class PublicApiError(RuntimeError):
    """The Public API refused a call; the message carries a status, never a credential."""


class _Transient(PublicApiError):
    """Throttled (429) or a gateway error (5xx): worth another attempt."""


class PublicApiClient:
    """Lists and downloads archived documents of public products."""

    def __init__(self, username: str, password: str, subscription_key: str, *, timeout: float = 300,
                 session: requests.Session | None = None, clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep):
        self._username, self._password, self._key = username, password, subscription_key
        self.timeout = timeout
        self.session = session or requests.Session()
        self._clock, self._sleep = clock, sleep
        self._token: str | None = None
        self._token_at: datetime | None = None
        self._last_call: float | None = None

    def __repr__(self) -> str:
        return "PublicApiClient()"

    @classmethod
    def from_secrets(cls, **kwargs) -> PublicApiClient:
        names = ("ERCOT_PUBLIC_API_USERNAME", "ERCOT_PUBLIC_API_PASSWORD", "ERCOT_PUBLIC_API_SUBSCRIPTION_KEY")
        values = [load_secret(name) for name in names]
        missing = [name for name, value in zip(names, values) if not value]
        if missing:
            raise ConfigError(f"Missing {', '.join(missing)}: set them in .env or the Keychain (see README, Credentials).")
        return cls(*values, **kwargs)

    # ------------------------------------------------------------------ the Source interface

    def list_documents(self, product: Product, start: datetime | None = None, end: datetime | None = None) -> list[RemoteDoc]:
        """Every archived document of a product posted within the window, oldest first."""
        params = {"size": PAGE_SIZE}
        if start is not None:
            params["postDatetimeFrom"] = _local(start)
        if end is not None:
            params["postDatetimeTo"] = _local(end)
        docs: list[RemoteDoc] = []
        page, pages = 1, 1
        while page <= pages:
            body = self._get(f"{ARCHIVE_URL}/{product.emil_id.lower()}", {**params, "page": page}).json()
            docs += parse_archive(body, product.emil_id)
            pages = int((body.get("_meta") or {}).get("totalPages") or 1)
            page += 1
        return sorted(docs, key=lambda d: (d.posted_at is None, d.posted_at, d.doc_id))

    def download(self, url: str) -> Iterator[bytes]:
        """Stream one archived document."""
        with self._get(url, None, stream=True) as response:
            yield from response.iter_content(CHUNK)

    # ------------------------------------------------------------------ transport

    def token(self, now: datetime | None = None) -> str:
        now = now or datetime.now(timezone.utc)
        if self._token is None or self._token_at is None or now - self._token_at > TOKEN_LIFETIME:
            response = self.session.post(TOKEN_URL, timeout=self.timeout, data={
                "grant_type": "password", "username": self._username, "password": self._password,
                "response_type": "id_token", "scope": f"openid {CLIENT_ID} offline_access", "client_id": CLIENT_ID})
            if response.status_code != 200:
                # The body can echo the user name, so only the status is reported.
                raise PublicApiError(f"HTTP {response.status_code} from the token endpoint; check the Public API user name and password")
            self._token, self._token_at = response.json()["id_token"], now
        return self._token

    def _get(self, url: str, params: dict | None, stream: bool = False) -> requests.Response:
        return retrying(lambda: self._get_once(url, params, stream), (requests.RequestException, _Transient))

    def _get_once(self, url: str, params: dict | None, stream: bool) -> requests.Response:
        for fresh in (False, True):
            self._pace()
            response = self.session.get(url, params=params, stream=stream, timeout=self.timeout, headers={
                "Authorization": f"Bearer {self.token()}", "Ocp-Apim-Subscription-Key": self._key})
            if response.status_code == 401 and not fresh:
                self._token = None  # expired early; ask once more with a new one
                continue
            break
        if response.status_code == 429 or response.status_code >= 500:
            raise _Transient(f"HTTP {response.status_code}")
        if response.status_code != 200:
            raise PublicApiError(f"HTTP {response.status_code}")
        return response

    def _pace(self) -> None:
        now = self._clock()
        if self._last_call is not None and now - self._last_call < MIN_INTERVAL:
            self._sleep(MIN_INTERVAL - (now - self._last_call))
        self._last_call = self._clock()


def parse_archive(body: dict, emil_id: str) -> list[RemoteDoc]:
    """One page of an archive listing as documents."""
    docs = []
    for item in body.get("archives") or []:
        name = item.get("friendlyName") or ""
        docs.append(RemoteDoc(
            report_type_id=None, doc_id=str(item["docId"]), file_name=name, report_group="", operating_date="",
            posted_at=_posted(item.get("postDatetime")), size_bytes=0, format="zip",
            url=((item.get("_links") or {}).get("endpoint") or {}).get("href") or f"{ARCHIVE_URL}/{emil_id.lower()}?download={item['docId']}"))
    return docs


def _local(t: datetime) -> str:
    """A bound as the API reads it: ERCOT local time without an offset."""
    if t.tzinfo is None:
        raise ValueError("Public API time bounds must be timezone-aware datetimes")
    return t.astimezone(ERCOT_TZ).strftime("%Y-%m-%dT%H:%M:%S")


def _posted(text: str | None) -> datetime | None:
    if not text:
        return None
    t = datetime.fromisoformat(text)
    return t if t.tzinfo else t.replace(tzinfo=ERCOT_TZ)

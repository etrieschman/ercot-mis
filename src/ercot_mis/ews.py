"""ERCOT's External Web Services (EWS): listing and downloading Secure and ECEII documents over SOAP.

EWS is the only programmatic route to the CRR and DAM network models. Every call presents an
API certificate twice: for mutual TLS, and as a WS-Security signature over the SOAP body. The
server accepts SHA-1 only, for the digest and the signature. The envelope is built by hand from
ERCOT's ``Message.xsd``; zeep signs it. EWS keeps a document only for the product's display
window, so anything not archived in time is gone (CLAUDE.md, "EWS").
"""

from __future__ import annotations

import base64
import secrets
import time
from collections.abc import Callable, Iterator
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, TypeVar
from urllib.parse import parse_qs, urlparse

import polars as pl
import requests
from lxml import etree

from .clock import TZ
from .config import Identity

if TYPE_CHECKING:
    from .products import Product

T = TypeVar("T")

ENDPOINT = "https://misapi.ercot.com/NodalAPI/EWS/"
TIMEOUT = 300  # seconds per call; a 30 MB download on a slow day
CHUNK = 1 << 20

# Reports are served by the MarketInfo operation; its SOAPAction is a path, not a URN.
SOAP_ACTION = "/BusinessService/NodalService.serviceagent/HttpEndPoint/MarketInfo"

NS = {
    "soap": "http://schemas.xmlsoap.org/soap/envelope/",
    "msg": "http://www.ercot.com/schema/2007-06/nodal/ews/message",
    # Message.xsd uses a "www.docs" variant of the WSS namespaces for ReplayDetection; the Security header zeep
    # adds uses the standard OASIS ones. The server validates both, so the two stay different.
    "wsse_msg": "http://www.docs.oasis-open.org/wss/2004/01/"
    "oasis-200401-wss-wssecurity-secext-1.0.xsd",
    "wsu_msg": "http://www.docs.oasis-open.org/wss/2004/01/"
    "oasis-200401-wss-wssecurity-utility-1.0.xsd",
}

# Seconds to wait before the second and third attempts at a stalled call.
DELAYS: tuple[float, ...] = (5.0, 20.0)


def retrying(call: Callable[[], T], transient: tuple[type[BaseException], ...]) -> T:
    """The result of ``call``, retried after each ``transient`` error (a stalled download, a 5xx reply) with the ``DELAYS`` between attempts."""
    for delay in (*DELAYS, None):
        try:
            return call()
        except transient:
            if delay is None:
                raise
            time.sleep(delay)
    raise AssertionError("unreachable")


class EwsError(RuntimeError):
    """EWS answered with a SOAP fault, a reply code other than OK, or an HTTP error."""


class _ServerError(EwsError):
    """A gateway error (HTTP 502 to 504), retried."""


@dataclass(frozen=True)
class RemoteDoc:
    """One posted document as a listing describes it, from EWS or the Public API archive."""

    report_type_id: int | None
    doc_id: str | None
    file_name: str
    report_group: str
    operating_date: str
    posted_at: datetime | None
    size_bytes: int
    format: str
    url: str


def _iso(t: datetime) -> str:
    if t.tzinfo is None:
        raise ValueError("EWS time bounds must be timezone-aware datetimes")
    return t.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def build_request(
    duns: str,
    user: str,
    report_type_id: int,
    start: datetime | None = None,
    end: datetime | None = None,
) -> str:
    """An unsigned GetReports envelope for one report type, posted within ``start`` and ``end`` when given.

    Header and Request are ``xsd:sequence`` types, so the children keep the schema's order; ``Option`` carries the report type id.
    """
    nonce = base64.b64encode(secrets.token_bytes(16)).decode()
    created = _iso(datetime.now(timezone.utc))
    window = "".join(
        f"        <msg:{tag}>{_iso(t)}</msg:{tag}>\n"
        for tag, t in (("StartTime", start), ("EndTime", end))
        if t is not None
    )
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<soap:Envelope xmlns:soap="{NS["soap"]}">
  <soap:Header/>
  <soap:Body>
    <msg:RequestMessage xmlns:msg="{NS["msg"]}"
                        xmlns:wsse="{NS["wsse_msg"]}"
                        xmlns:wsu="{NS["wsu_msg"]}">
      <msg:Header>
        <msg:Verb>get</msg:Verb>
        <msg:Noun>Reports</msg:Noun>
        <msg:ReplayDetection>
          <msg:Nonce>{nonce}</msg:Nonce>
          <msg:Created>{created}</msg:Created>
        </msg:ReplayDetection>
        <msg:Revision>1</msg:Revision>
        <msg:Source>{duns}</msg:Source>
        <msg:UserID>{user}</msg:UserID>
      </msg:Header>
      <msg:Request>
{window}        <msg:Option>{report_type_id}</msg:Option>
      </msg:Request>
    </msg:RequestMessage>
  </soap:Body>
</soap:Envelope>
"""


def sign(envelope: str, cert: Path, key: Path) -> bytes:
    """The envelope with a WS-Security header: an RSA-SHA1 signature over the body and the certificate as a BinarySecurityToken."""
    try:
        import xmlsec
        from zeep.wsse.signature import BinarySignature
    except ImportError as error:
        raise ImportError(
            "WS-Security signing needs xmlsec. If its wheel fails to build: "
            "brew install libxmlsec1 pkg-config"
        ) from error

    signer = BinarySignature(
        str(key),
        str(cert),
        password=None,
        signature_method=xmlsec.Transform.RSA_SHA1,
        digest_method=xmlsec.Transform.SHA1,
    )
    root = etree.fromstring(envelope.encode("utf-8"))
    root, _ = signer.apply(root, {})
    return etree.tostring(root, xml_declaration=True, encoding="UTF-8")


def parse_reports(xml: bytes, report_type_id: int) -> list[RemoteDoc]:
    """The documents in a GetReports reply; a SOAP fault or an error reply code raises ``EwsError``.

    Elements are matched by local name because ERCOT has changed the payload namespaces between revisions.
    """
    root = etree.fromstring(xml)

    fault = root.find(".//soap:Fault", NS)
    if fault is not None:
        detail = " ".join(t for t in (fault.findtext("faultstring"), fault.findtext("detail")) if t)
        raise EwsError(f"SOAP Fault: {detail or 'no detail'}")

    code = root.findtext(".//msg:ReplyCode", namespaces=NS)
    if code and code.upper() != "OK":
        errors = [(e.text or "").strip() for e in root.findall(".//msg:Error", NS)]
        # An empty window comes back as an error reply, not as an empty payload.
        if errors and all(e.startswith("No reports found") for e in errors):
            return []
        raise EwsError(f"ReplyCode={code}: {errors}")

    docs = []
    for node in root.iter():
        if not isinstance(node.tag, str) or etree.QName(node).localname != "Report":
            continue
        fields = {
            etree.QName(child).localname: (child.text or "").strip()
            for child in node
            if isinstance(child.tag, str)
        }
        if fields:
            docs.append(_to_doc(fields, report_type_id))
    return docs


def _to_doc(fields: dict[str, str], report_type_id: int) -> RemoteDoc:
    url = fields.get("URL", "")
    doc_id = parse_qs(urlparse(url).query).get("doclookupId", [None])[0]
    return RemoteDoc(
        report_type_id=report_type_id,
        doc_id=doc_id,
        file_name=fields.get("fileName", ""),
        report_group=fields.get("reportGroup", ""),
        operating_date=fields.get("operatingDate", ""),
        posted_at=_parse_time(fields.get("created", "")),
        size_bytes=int(fields.get("size") or 0),
        format=fields.get("format", ""),
        url=url,
    )


def _parse_time(text: str) -> datetime | None:
    if not text:
        return None
    try:
        t = datetime.fromisoformat(text)
    except ValueError:
        return None
    return t if t.tzinfo else t.replace(tzinfo=TZ)


class EwsClient:
    """Lists and downloads documents over EWS with the participant's API certificate."""

    def __init__(self, identity: Identity):
        self.identity = identity
        self.session = requests.Session()

    def list_documents(
        self,
        product: Product,
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> list[RemoteDoc]:
        """The documents posted for ``product``, within ``start`` and ``end`` when given."""
        if product.report_type_id is None:
            raise ValueError(f"{product.emil_id} has no EWS report type ID")
        return self.get_reports(product.report_type_id, start, end)

    def download(self, url: str) -> Iterator[bytes]:
        """The bytes of a listed document, in chunks; the download servlet wants the certificate but no signed envelope."""
        with self.session.get(
            url,
            cert=(str(self.identity.cert), str(self.identity.key)),
            timeout=TIMEOUT,
            stream=True,
        ) as response:
            if response.status_code != 200:
                raise EwsError(f"HTTP {response.status_code} downloading a document")
            yield from response.iter_content(CHUNK)

    def get_reports(
        self,
        report_type_id: int,
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> list[RemoteDoc]:
        """The documents posted for a report type, within ``start`` and ``end`` when given; a stalled call is retried."""
        return retrying(lambda: self._get_reports(report_type_id, start, end), (requests.RequestException, _ServerError))

    def _get_reports(self, report_type_id: int, start: datetime | None, end: datetime | None) -> list[RemoteDoc]:
        envelope = build_request(self.identity.duns, self.identity.api_user, report_type_id, start, end)
        body = sign(envelope, self.identity.cert, self.identity.key)
        response = self.session.post(
            ENDPOINT,
            data=body,
            headers={"Content-Type": "text/xml; charset=utf-8", "SOAPAction": SOAP_ACTION},
            cert=(str(self.identity.cert), str(self.identity.key)),
            timeout=TIMEOUT,
        )
        if response.status_code >= 502:
            raise _ServerError(f"HTTP {response.status_code}")
        if response.status_code != 200:
            # A fault arrives as HTTP 500 with a SOAP body that says why.
            try:
                parse_reports(response.content, report_type_id)
            except (EwsError, etree.XMLSyntaxError) as error:
                raise EwsError(f"HTTP {response.status_code}: {error}") from None
            raise EwsError(f"HTTP {response.status_code}")
        return parse_reports(response.content, report_type_id)


# ------------------------------------------------------------------ the archive probe: how far back EWS keeps a product

# Listing columns that embed the participant's DUNS (ERCOT's file names) or point at its download servlet; they stay in the catalog.
PRIVATE_COLUMNS = ("file_name", "url")

DOCUMENT_SCHEMA = {
    "report_type_id": pl.Int64,
    "doc_id": pl.String,
    "report_group": pl.String,
    "operating_date": pl.String,
    "posted_at": pl.Datetime("us", "UTC"),
    "size_bytes": pl.Int64,
    "format": pl.String,
}


@dataclass(frozen=True)
class Probe:
    """What EWS holds for one product: ``summary`` (counts, dates, bytes, report groups) and ``documents`` (the listing without file names and URLs). Both print safely."""

    summary: dict
    documents: pl.DataFrame


def probe(client: EwsClient | None, spec: Product, *, archive_years: float = 7, now: datetime | None = None) -> Probe:
    """Two listings of a product and nothing downloaded: one unbounded, one asking for documents posted before the display window.

    ``summary["n_docs_before_display_window"]`` says whether EWS keeps anything older than the window.
    """
    if client is None or spec.source != "ews" or spec.report_type_id is None:
        raise NotImplementedError(f"{spec.emil_id} comes from the Public API; probe() measures the EWS archive only.")
    now = datetime.now(timezone.utc) if now is None else now
    cutoff = now - timedelta(days=spec.display_days or 0)

    unbounded = client.get_reports(spec.report_type_id)
    older_error = None
    try:
        older = client.get_reports(
            spec.report_type_id,
            start=now - timedelta(days=round(365.25 * archive_years)),
            end=cutoff,
        )
    except EwsError as error:
        older, older_error = [], str(error)

    docs = _dedupe(unbounded + older)
    return Probe(summary=_summarize(spec, docs, len(unbounded), cutoff, now, older_error), documents=_frame(docs))


def _dedupe(docs: list[RemoteDoc]) -> list[RemoteDoc]:
    seen: dict[str, RemoteDoc] = {}
    for doc in docs:
        seen.setdefault(doc.doc_id or doc.url or f"{doc.file_name}|{doc.posted_at}", doc)
    earliest = datetime.min.replace(tzinfo=timezone.utc)
    return sorted(seen.values(), key=lambda d: (d.posted_at is None, d.posted_at or earliest))


def _summarize(
    spec: Product,
    docs: list[RemoteDoc],
    n_unbounded: int,
    cutoff: datetime,
    now: datetime,
    older_error: str | None,
) -> dict:
    posted = [d.posted_at for d in docs if d.posted_at is not None]
    operating = sorted(d.operating_date for d in docs if d.operating_date)
    groups: dict[str, int] = {}
    for doc in docs:
        groups[doc.report_group] = groups.get(doc.report_group, 0) + 1
    return {
        "emil_id": spec.emil_id,
        "name": spec.name,
        "report_type_id": spec.report_type_id,
        "display_days": spec.display_days,
        "n_docs": len(docs),
        "n_docs_unbounded_listing": n_unbounded,
        "n_docs_before_display_window": sum(1 for t in posted if t < cutoff),
        "earliest_posted": min(posted, default=None),
        "latest_posted": max(posted, default=None),
        "earliest_operating_date": operating[0] if operating else None,
        "latest_operating_date": operating[-1] if operating else None,
        "total_bytes": sum(d.size_bytes for d in docs),
        "report_groups": groups,
        "older_window_error": older_error,
        "probed_at": now,
    }


def _frame(docs: list[RemoteDoc]) -> pl.DataFrame:
    rows = [
        {**{k: v for k, v in asdict(d).items() if k not in PRIVATE_COLUMNS},
         "posted_at": d.posted_at.astimezone(timezone.utc) if d.posted_at else None}
        for d in docs
    ]
    return pl.DataFrame(rows, schema=DOCUMENT_SCHEMA)

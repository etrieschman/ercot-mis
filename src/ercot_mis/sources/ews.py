"""ERCOT External Web Services (EWS): the SOAP channel for Secure and ECEII reports.

ERCOT runs two unrelated APIs. The Public Data API (REST) serves only products
classified Public. EWS (``misapi.ercot.com/NodalAPI/EWS/``) is the only
programmatic route to Secure and ECEII products such as the CRR and DAM network
models. This module lists the documents posted for a report type; downloading
them arrives with the archive store.

Credentials
-----------
EWS needs an *API* certificate, not the personal one used for MIS in a browser:
the server rejects user IDs not prefixed ``API_`` (``SECU1073``). Two layers of
authentication apply to every call, both with the same certificate:

1. mutual TLS during the handshake, and
2. a WS-Security XML signature over the SOAP Body (``SECU1096`` without it).

ERCOT's WS-Security stack predates SHA-2 and rejects anything but SHA-1
(``SECU3518`` for the digest, ``SECU3517`` for the signature). That is the
server's requirement, not a choice made here.

The envelope is built by hand from ERCOT's ``Message.xsd``; zeep is used only as
a signing library. The shipped WSDL declares a placeholder address and an untyped
payload, so a generated client would buy little and hide the wire format.
"""

from __future__ import annotations

import base64
import secrets
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import parse_qs, urlparse
from zoneinfo import ZoneInfo

import requests
from lxml import etree

from ..config import Identity

if TYPE_CHECKING:
    from ..products import Product

ENDPOINT = "https://misapi.ercot.com/NodalAPI/EWS/"
CHUNK = 1 << 20

# All EWS operations accept the same RequestMessage; the Noun selects the
# resource, and reports are served by MarketInfo. The SOAPAction is a path, not a
# URN: a URN yields "RUNTIME0031: Failed to locate the operation".
SOAP_ACTION = "/BusinessService/NodalService.serviceagent/HttpEndPoint/MarketInfo"

NS = {
    "soap": "http://schemas.xmlsoap.org/soap/envelope/",
    "msg": "http://www.ercot.com/schema/2007-06/nodal/ews/message",
    # Message.xsd imports a "www.docs" variant of the WSS namespaces for its
    # ReplayDetection types, while the Security header zeep adds uses the standard
    # OASIS namespaces. The server validates both, so do not unify them.
    "wsse_msg": "http://www.docs.oasis-open.org/wss/2004/01/"
    "oasis-200401-wss-wssecurity-secext-1.0.xsd",
    "wsu_msg": "http://www.docs.oasis-open.org/wss/2004/01/"
    "oasis-200401-wss-wssecurity-utility-1.0.xsd",
}

# ERCOT operates in Central Prevailing Time; used only when a timestamp has no offset.
ERCOT_TZ = ZoneInfo("America/Chicago")


class EwsError(RuntimeError):
    """EWS answered with a SOAP Fault, a non-OK reply code, or an HTTP error."""


@dataclass(frozen=True)
class RemoteDoc:
    """One posted document, as described by a GetReports listing."""

    report_type_id: int
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
    """Build an unsigned GetReports envelope.

    Element order is load-bearing: ``HeaderType`` and ``RequestType`` are
    ``xsd:sequence``, so children follow schema order (Header: Verb, Noun,
    ReplayDetection, Revision, Source, UserID; Request: ..., StartTime, EndTime,
    ..., Option). ``Option`` is the report type ID and is required. Either time
    bound may be omitted, and omitting both asks for everything available.
    ReplayDetection is mandatory and its Nonce is fresh per call.
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
    """Attach a WS-Security header carrying an XML signature over the Body.

    The certificate travels as a BinarySecurityToken, the X.509 Token Profile
    ERCOT's spec cites. The server checks the digest algorithm before the
    signature algorithm, so a wrong digest masks a wrong signature method.
    """
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
    """Extract the documents from a GetReports reply.

    Reports are matched by local name rather than a fixed namespace: the payload
    and the message envelope use different namespaces, and ERCOT has changed them
    across revisions, so a future change shows up as a parse difference rather
    than a silent empty result.
    """
    root = etree.fromstring(xml)

    fault = root.find(".//soap:Fault", NS)
    if fault is not None:
        detail = " ".join(t for t in (fault.findtext("faultstring"), fault.findtext("detail")) if t)
        raise EwsError(f"SOAP Fault: {detail or 'no detail'}")

    code = root.findtext(".//msg:ReplyCode", namespaces=NS)
    if code and code.upper() != "OK":
        errors = [(e.text or "").strip() for e in root.findall(".//msg:Error", NS)]
        # An empty window comes back as ReplyCode=ERROR, not as an empty payload.
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
    return t if t.tzinfo else t.replace(tzinfo=ERCOT_TZ)


class EwsClient:
    """Lists documents over EWS with an ERCOT API certificate."""

    def __init__(
        self,
        identity: Identity,
        endpoint: str = ENDPOINT,
        timeout: float = 300,
        session: requests.Session | None = None,
    ):
        self.identity = identity
        self.endpoint = endpoint
        self.timeout = timeout
        self.session = session or requests.Session()

    def list_documents(
        self,
        product: Product,
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> list[RemoteDoc]:
        """The Source interface: list a product's documents."""
        if product.report_type_id is None:
            raise ValueError(f"{product.emil_id} has no EWS report type ID")
        return self.get_reports(product.report_type_id, start, end)

    def download(self, url: str) -> Iterator[bytes]:
        """The Source interface: stream a listed document.

        The download servlet wants the same client certificate as the SOAP call, but no
        signed envelope.
        """
        with self.session.get(
            url,
            cert=(str(self.identity.cert), str(self.identity.key)),
            timeout=self.timeout,
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
        """List the documents posted for a report type, optionally within a time window."""
        envelope = build_request(self.identity.duns, self.identity.api_user, report_type_id, start, end)
        body = sign(envelope, self.identity.cert, self.identity.key)
        response = self.session.post(
            self.endpoint,
            data=body,
            headers={"Content-Type": "text/xml; charset=utf-8", "SOAPAction": SOAP_ACTION},
            # Presented again for mutual TLS; sign() already put it inside the message.
            cert=(str(self.identity.cert), str(self.identity.key)),
            timeout=self.timeout,
        )
        if response.status_code != 200:
            # Faults usually arrive as HTTP 500 with a SOAP body worth reading.
            try:
                parse_reports(response.content, report_type_id)
            except (EwsError, etree.XMLSyntaxError) as error:
                raise EwsError(f"HTTP {response.status_code}: {error}") from None
            raise EwsError(f"HTTP {response.status_code}")
        return parse_reports(response.content, report_type_id)

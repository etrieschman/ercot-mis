from datetime import datetime, timedelta, timezone

import pytest
from lxml import etree

from ercot_mis.sources import ews

DSIG = "http://www.w3.org/2000/09/xmldsig#"

REPLY = b"""<?xml version="1.0" encoding="UTF-8"?>
<soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/">
  <soap:Body>
    <ns0:ResponseMessage xmlns:ns0="http://www.ercot.com/schema/2007-06/nodal/ews/message">
      <ns0:Reply><ns0:ReplyCode>OK</ns0:ReplyCode></ns0:Reply>
      <ns0:Payload>
        <ns1:Report xmlns:ns1="http://www.ercot.com/schema/2007-06/nodal/ews">
          <ns1:operatingDate>2026-08-17</ns1:operatingDate>
          <ns1:reportGroup>CRR Network Model (Monthly)</ns1:reportGroup>
          <ns1:fileName>example_network_model.zip</ns1:fileName>
          <ns1:created>2026-08-17T09:27:31-05:00</ns1:created>
          <ns1:size>43214180</ns1:size>
          <ns1:format>zip</ns1:format>
          <ns1:URL>https://mis.ercot.com/misdownload/servlets/mirDownload?doclookupId=1234567</ns1:URL>
        </ns1:Report>
        <!-- a comment node must not break iteration -->
        <ns1:Report xmlns:ns1="http://www.ercot.com/schema/2007-06/nodal/ews">
          <ns1:reportGroup>CRR Network Model (Monthly)</ns1:reportGroup>
          <ns1:fileName>no_offset.zip</ns1:fileName>
          <ns1:created>2026-08-20T12:26:42</ns1:created>
          <ns1:size></ns1:size>
        </ns1:Report>
      </ns0:Payload>
    </ns0:ResponseMessage>
  </soap:Body>
</soap:Envelope>"""


def _children(node):
    return [etree.QName(c).localname for c in node]


def test_request_follows_schema_order():
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    end = datetime(2026, 2, 1, 6, tzinfo=timezone(timedelta(hours=-6)))
    root = etree.fromstring(ews.build_request("123456789", "API_EXAMPLE", 11204, start, end).encode())
    header = root.find(".//msg:Header", ews.NS)
    request = root.find(".//msg:Request", ews.NS)
    assert _children(header) == ["Verb", "Noun", "ReplayDetection", "Revision", "Source", "UserID"]
    assert _children(request) == ["StartTime", "EndTime", "Option"]
    assert request.findtext("msg:StartTime", namespaces=ews.NS) == "2026-01-01T00:00:00Z"
    assert request.findtext("msg:EndTime", namespaces=ews.NS) == "2026-02-01T12:00:00Z"
    assert request.findtext("msg:Option", namespaces=ews.NS) == "11204"


def test_request_without_window_asks_for_everything():
    root = etree.fromstring(ews.build_request("123456789", "API_EXAMPLE", 13070).encode())
    assert _children(root.find(".//msg:Request", ews.NS)) == ["Option"]


def test_request_nonce_is_fresh_and_times_must_be_aware():
    nonce = lambda: etree.fromstring(ews.build_request("1", "API_X", 1).encode()).findtext(".//msg:Nonce", namespaces=ews.NS)
    assert nonce() != nonce()
    with pytest.raises(ValueError, match="timezone-aware"):
        ews.build_request("1", "API_X", 1, start=datetime(2026, 1, 1))


def test_parse_reports():
    first, second = ews.parse_reports(REPLY, 11205)
    assert first.doc_id == "1234567"
    assert first.size_bytes == 43214180
    assert first.posted_at == datetime(2026, 8, 17, 14, 27, 31, tzinfo=timezone.utc)
    assert first.report_group == "CRR Network Model (Monthly)"
    assert second.size_bytes == 0 and second.doc_id is None
    assert second.posted_at.utcoffset() == timedelta(hours=-5)  # CDT assumed when no offset


def test_parse_fault_and_reply_code():
    fault = b"""<soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/"><soap:Body>
      <soap:Fault><faultcode>soap:Client</faultcode>
      <faultstring>SECU1096: Could not find a WS-Security Header</faultstring></soap:Fault>
    </soap:Body></soap:Envelope>"""
    with pytest.raises(ews.EwsError, match="SECU1096"):
        ews.parse_reports(fault, 1)
    refused = REPLY.replace(b">OK<", b">ERROR<")
    with pytest.raises(ews.EwsError, match="ReplyCode=ERROR"):
        ews.parse_reports(refused, 1)


def test_no_reports_found_is_an_empty_listing():
    empty = b"""<soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/"><soap:Body>
      <ns0:ResponseMessage xmlns:ns0="http://www.ercot.com/schema/2007-06/nodal/ews/message">
        <ns0:Reply><ns0:ReplyCode>ERROR</ns0:ReplyCode>
          <ns0:Error>No reports found matching the criteria</ns0:Error></ns0:Reply>
      </ns0:ResponseMessage>
    </soap:Body></soap:Envelope>"""
    assert ews.parse_reports(empty, 11204) == []


def _self_signed(tmp_path):
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "API_TEST")])
    now = datetime.now(timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=1))
        .not_valid_after(now + timedelta(days=1))
        .sign(key, hashes.SHA256())
    )
    cert_path, key_path = tmp_path / "test.crt", tmp_path / "test.key"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption(),
        )
    )
    return cert_path, key_path


def test_signature_uses_sha1_and_binary_token(tmp_path):
    cert, key = _self_signed(tmp_path)
    signed = etree.fromstring(ews.sign(ews.build_request("1", "API_X", 11204), cert, key))
    assert signed.find(f".//{{{DSIG}}}DigestMethod").get("Algorithm") == DSIG + "sha1"
    assert signed.find(f".//{{{DSIG}}}SignatureMethod").get("Algorithm") == DSIG + "rsa-sha1"
    names = {etree.QName(e).localname for e in signed.iter() if isinstance(e.tag, str)}
    assert {"Security", "BinarySecurityToken", "Signature"} <= names
    # The signed payload is untouched.
    assert signed.findtext(".//msg:Option", namespaces=ews.NS) == "11204"

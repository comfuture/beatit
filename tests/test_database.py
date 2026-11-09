"""TLS verification against real TLS handshakes; no database connection is opened."""

import ssl
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from transcription.database import Database, mysql_tls_context


@pytest.fixture(scope="module")
def tls_server(tmp_path_factory):
    root = tmp_path_factory.mktemp("mysql-tls")
    now = datetime.now(timezone.utc)

    def authority(name):
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, name)])
        cert = (
            x509.CertificateBuilder()
            .subject_name(subject)
            .issuer_name(subject)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(days=1))
            .not_valid_after(now + timedelta(days=1))
            .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
            .add_extension(
                x509.KeyUsage(False, False, False, False, False, True, True, False, False),
                critical=True,
            )
            .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), False)
            .sign(key, hashes.SHA256())
        )
        path = root / (name + ".pem")
        path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
        return key, cert, str(path)

    ca_key, ca_cert, trusted_ca = authority("trusted-ca")
    _, _, other_ca = authority("other-ca")
    server_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    server_cert = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "db.example.test")]))
        .issuer_name(ca_cert.subject)
        .public_key(server_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(hours=1))
        .not_valid_after(now + timedelta(hours=1))
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(x509.SubjectAlternativeName([x509.DNSName("db.example.test")]), False)
        .add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()), False
        )
        .sign(ca_key, hashes.SHA256())
    )
    certificate = root / "server.pem"
    certificate.write_bytes(server_cert.public_bytes(serialization.Encoding.PEM))
    key_path = root / "server-key.pem"
    key_path.write_bytes(
        server_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    server = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server.load_cert_chain(certificate, key_path)
    return server, trusted_ca, other_ca


def handshake(client_context, server_context, hostname):
    """Exchange actual TLS records entirely in memory, without binding a port."""
    client_in, client_out = ssl.MemoryBIO(), ssl.MemoryBIO()
    server_in, server_out = ssl.MemoryBIO(), ssl.MemoryBIO()
    client = client_context.wrap_bio(client_in, client_out, server_hostname=hostname)
    server = server_context.wrap_bio(server_in, server_out, server_side=True)
    completed = set()
    for _ in range(20):
        for peer in (client, server):
            if peer not in completed:
                try:
                    peer.do_handshake()
                    completed.add(peer)
                except ssl.SSLWantReadError:
                    pass
        if pending := client_out.read():
            server_in.write(pending)
        if pending := server_out.read():
            client_in.write(pending)
        if len(completed) == 2:
            return client
    raise AssertionError("TLS handshake did not complete")


def test_verify_ca_accepts_hostname_mismatch_with_a_pinned_trusted_ca(tls_server):
    server, trusted_ca, _ = tls_server
    context = mysql_tls_context("127.0.0.1", {"ssl_mode": ["VERIFY_CA"], "ssl_ca": [trusted_ca]})
    assert handshake(context, server, "127.0.0.1").cipher()
    assert context.verify_mode == ssl.CERT_REQUIRED
    assert context.check_hostname is False


def test_verify_ca_still_rejects_a_certificate_signed_by_another_ca(tls_server):
    server, _, other_ca = tls_server
    context = mysql_tls_context("127.0.0.1", {"ssl_mode": ["VERIFY_CA"], "ssl_ca": [other_ca]})
    with pytest.raises(ssl.SSLCertVerificationError):
        handshake(context, server, "127.0.0.1")


@pytest.mark.parametrize("mode", [None, "VERIFY_IDENTITY"])
def test_default_and_explicit_identity_verification_reject_hostname_mismatch(tls_server, mode):
    server, trusted_ca, _ = tls_server
    query = {"ssl_ca": [trusted_ca]}
    if mode is not None:
        query["ssl_mode"] = [mode]
    context = mysql_tls_context("db.example.test", query)
    assert handshake(context, server, "db.example.test").cipher()
    with pytest.raises(ssl.SSLCertVerificationError, match="mismatch"):
        handshake(context, server, "another.example.test")


def test_remote_default_requires_a_trusted_certificate(tls_server):
    server, trusted_ca, _ = tls_server
    context = mysql_tls_context("db.example.test", {})
    with pytest.raises(ssl.SSLCertVerificationError):
        handshake(context, server, "db.example.test")
    context.load_verify_locations(cafile=trusted_ca)
    assert handshake(context, server, "db.example.test").cipher()
    with pytest.raises(ssl.SSLCertVerificationError, match="mismatch"):
        handshake(context, server, "another.example.test")


@pytest.mark.parametrize("hostname", ["localhost", "127.0.0.1", "::1"])
def test_loopback_without_explicit_tls_settings_remains_available_for_development(hostname):
    assert mysql_tls_context(hostname, {}) is None


def test_explicit_identity_mode_requires_verified_tls_even_on_loopback(tls_server):
    server, trusted_ca, _ = tls_server
    context = mysql_tls_context(
        "127.0.0.1", {"ssl_mode": ["VERIFY_IDENTITY"], "ssl_ca": [trusted_ca]}
    )
    with pytest.raises(ssl.SSLCertVerificationError, match="mismatch"):
        handshake(context, server, "127.0.0.1")


@pytest.mark.parametrize(
    "query,error",
    [
        ({"ssl_mode": "VERIFY_CA"}, "requires an explicit ssl_ca"),
        ({"ssl_mode": "VERIFY_CA", "ssl_ca": ""}, "ssl_ca"),
        ({"ssl_mode": "DISABLED"}, "supports only"),
        ({"ssl_mode": "PREFERRED"}, "supports only"),
        ({"ssl_mode": "REQUIRED"}, "supports only"),
        ({"ssl_mode": "unknown"}, "supports only"),
        ({"ssl_mode": ""}, "ssl_mode"),
        ({"ssl_mode": ["VERIFY_CA", "VERIFY_IDENTITY"]}, "ssl_mode"),
        ({"ssl_ca": ["first.pem", "second.pem"]}, "ssl_ca"),
    ],
)
def test_invalid_tls_configuration_fails_before_opening_a_database(monkeypatch, query, error):
    def forbidden(*args, **kwargs):
        raise AssertionError("Invalid TLS settings must fail before connecting")

    monkeypatch.setattr("transcription.database.pymysql.connect", forbidden)
    url = "mysql://beatit:example-password@127.0.0.1:13306/beatit?" + urlencode(query, doseq=True)
    with pytest.raises(RuntimeError, match=error) as raised:
        Database(url)
    assert "example-password" not in str(raised.value)


def test_unreadable_pinned_ca_fails_before_opening_a_database(tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("An unreadable CA must fail before connecting")

    monkeypatch.setattr("transcription.database.pymysql.connect", forbidden)
    query = urlencode({"ssl_mode": "VERIFY_CA", "ssl_ca": str(tmp_path / "missing.pem")})
    with pytest.raises(RuntimeError, match="Unable to load the MySQL ssl_ca"):
        Database("mysql://beatit:example-password@127.0.0.1:13306/beatit?" + query)

import base64

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from core import connection, session


def test_pem_private_key_is_converted_to_base64_der():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                            serialization.NoEncryption()).decode()
    der = base64.b64decode(connection.pem_to_der_b64(pem))
    loaded = serialization.load_der_private_key(der, password=None)
    assert loaded.private_numbers() == key.private_numbers()


def test_slug():
    assert session.slug("Jane.Doe@Example.com") == "jane.doe_example.com"
    assert session.slug("  ") == "anonymous"


def test_open_session_uses_a_fresh_builder_each_time(monkeypatch):
    """Session.builder is shared and merges options: one viewer's private key
    must never end up in another viewer's connection."""
    from snowflake.snowpark import Session

    from core.connection import ConnectionSettings

    seen = []

    class FakeSess:
        def sql(self, q):
            class R:
                def collect(self_inner):
                    return [("U",)]
            return R()

    def fake_create(self):
        seen.append(dict(self._options))
        return FakeSess()

    monkeypatch.setattr(Session.SessionBuilder, "create", fake_create)
    monkeypatch.setattr(session, "_mode", "local")
    private_pem, _ = connection.generate_key_pair()
    session.open_session(ConnectionSettings("a", "alice", "keypair", private_key_pem=private_pem))
    session.open_session(ConnectionSettings("a", "bob", "password", password="pw"))
    assert "private_key" in seen[0]
    assert "private_key" not in seen[1] and "authenticator" not in seen[1]
    assert seen[1]["client_session_keep_alive"] is True

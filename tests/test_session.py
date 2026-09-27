import base64

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from core import session


def test_pem_private_key_is_converted_to_base64_der():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                            serialization.NoEncryption()).decode()
    der = base64.b64decode(session._pem_to_der_b64(pem))
    loaded = serialization.load_der_private_key(der, password=None)
    assert loaded.private_numbers() == key.private_numbers()


def test_slug():
    assert session.slug("Jane.Doe@Example.com") == "jane.doe_example.com"
    assert session.slug("  ") == "anonymous"

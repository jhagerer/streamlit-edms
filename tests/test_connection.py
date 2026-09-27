import base64
import tomllib

import pytest
from cryptography.hazmat.primitives import serialization

from core import connection
from core.connection import ConnectionSettings


def test_normalize_account():
    assert connection.normalize_account(" https://MyOrg-Acct.snowflakecomputing.com/ ") == "MyOrg-Acct"
    assert connection.normalize_account("xy12345.eu-central-1") == "xy12345.eu-central-1"


def test_validate_reports_missing_fields():
    errors = connection.validate(ConnectionSettings(auth="keypair"))
    assert any("Account" in e for e in errors)
    assert any("User" in e for e in errors)
    assert any("private key" in e for e in errors)
    assert any("identifier" in e for e in
               connection.validate(ConnectionSettings("a", "u", "pat", token="t", role="x; drop")))


def test_connector_params_per_method():
    private_pem, _ = connection.generate_key_pair()
    kp = connection.connector_params(ConnectionSettings("org-acct", "SVC", "keypair", role="R",
                                                        private_key_pem=private_pem))
    assert kp["authenticator"] == "SNOWFLAKE_JWT" and kp["role"] == "R"
    serialization.load_der_private_key(base64.b64decode(kp["private_key"]), password=None)
    assert "warehouse" not in kp

    pat = connection.connector_params(ConnectionSettings("a", "u", "pat", token=" tok "))
    assert pat["authenticator"] == "PROGRAMMATIC_ACCESS_TOKEN" and pat["token"] == "tok"

    pw = connection.connector_params(ConnectionSettings("a", "u", "password", password="p",
                                                        passcode="123456"))
    assert pw["password"] == "p" and pw["passcode"] == "123456" and "authenticator" not in pw

    sso = connection.connector_params(ConnectionSettings("a", "u", "externalbrowser"))
    assert sso["authenticator"] == "externalbrowser"

    with pytest.raises(ValueError):
        connection.connector_params(ConnectionSettings("a", "u", "pat"))


def test_encrypted_key_needs_and_uses_passphrase():
    private_pem, _ = connection.generate_key_pair("s3cret")
    assert "ENCRYPTED" in private_pem
    der = base64.b64decode(connection.pem_to_der_b64(private_pem, "s3cret"))
    serialization.load_der_private_key(der, password=None)
    with pytest.raises(Exception):
        connection.pem_to_der_b64(private_pem, None)


def test_secrets_toml_round_trip():
    private_pem, _ = connection.generate_key_pair()
    s = ConnectionSettings("https://org-acct.snowflakecomputing.com", "SVC", "keypair",
                           role="R", warehouse="WH", database="DB", schema="S",
                           private_key_pem=private_pem)
    cfg = tomllib.loads(connection.secrets_toml(s))["connections"]["snowflake"]
    assert cfg["account"] == "org-acct"
    assert cfg["private_key"] == private_pem.strip()
    assert cfg["authenticator"] == "SNOWFLAKE_JWT"
    # What the form pre-fills from secrets never contains the secret itself.
    back = connection.settings_from_secrets(cfg)
    assert (back.account, back.user, back.auth, back.schema) == ("org-acct", "SVC", "keypair", "S")
    assert back.private_key_pem == ""

    hidden = connection.secrets_toml(s, include_secrets=False)
    assert "PRIVATE KEY" not in hidden and "<secret>" in hidden


def test_merged_with_keeps_previous_secrets():
    prev = ConnectionSettings("a", "u", "pat", token="old")
    new = ConnectionSettings("a", "u", "pat", warehouse="WH2").merged_with(prev)
    assert new.token == "old" and new.warehouse == "WH2"
    assert "token" not in new.public()


def test_key_pair_helpers():
    private_pem, public_pem = connection.generate_key_pair()
    assert connection.public_key_from_private(private_pem) == public_pem
    body = connection.public_key_body(public_pem)
    assert "-----" not in body and "\n" not in body
    assert connection.alter_user_sql("svc_user", public_pem) == \
        f"ALTER USER svc_user SET RSA_PUBLIC_KEY = '{body}'"
    assert connection.alter_user_sql("jane@x.com", public_pem).startswith('ALTER USER "jane@x.com"')

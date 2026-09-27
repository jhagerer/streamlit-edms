"""Snowflake connection settings entered in the app (outside SiS).

Pure: builds connector parameters, secrets.toml text and key pairs. No
Streamlit, no network. ``core/session.py`` uses it to open the connection.
"""

from __future__ import annotations

import base64
import json
import re
from dataclasses import asdict, dataclass

AUTH_METHODS = {
    "keypair": "Key pair (recommended for hosted apps)",
    "pat": "Programmatic access token",
    "password": "Password (+ MFA passcode)",
    "externalbrowser": "Browser SSO (only when the app runs on your own computer)",
}

SECRET_FIELDS = ("private_key_pem", "private_key_passphrase", "password", "passcode", "token")

_IDENT = re.compile(r'^(?:[A-Za-z_][A-Za-z0-9_$]*|"[^"]+")$')


@dataclass
class ConnectionSettings:
    account: str = ""
    user: str = ""
    auth: str = "keypair"
    role: str = ""
    warehouse: str = ""
    database: str = ""
    schema: str = ""
    private_key_pem: str = ""
    private_key_passphrase: str = ""
    password: str = ""
    passcode: str = ""
    token: str = ""

    def public(self) -> dict:
        """The non-secret fields, safe to show."""
        return {k: v for k, v in asdict(self).items() if k not in SECRET_FIELDS}

    def merged_with(self, previous: "ConnectionSettings | None") -> "ConnectionSettings":
        """Blank secret fields keep the previous value (so changing e.g. the
        warehouse does not require pasting the key again)."""
        if previous is None:
            return self
        data = asdict(self)
        for f in SECRET_FIELDS:
            if not data[f]:
                data[f] = getattr(previous, f)
        return ConnectionSettings(**data)


def normalize_account(account: str) -> str:
    """Accept 'orgname-account', a locator, or a full URL."""
    a = account.strip()
    a = re.sub(r"^https?://", "", a, flags=re.IGNORECASE)
    a = re.sub(r"\.snowflakecomputing\.com/?.*$", "", a, flags=re.IGNORECASE)
    return a.strip("/")


def validate(s: ConnectionSettings) -> list[str]:
    errors = []
    if not normalize_account(s.account):
        errors.append("Account is required (e.g. myorg-myaccount).")
    if not s.user.strip():
        errors.append("User is required.")
    if s.auth not in AUTH_METHODS:
        errors.append(f"Unknown authentication method {s.auth!r}.")
    if s.auth == "keypair" and not s.private_key_pem.strip():
        errors.append("A private key is required for key-pair authentication.")
    if s.auth == "password" and not s.password:
        errors.append("A password is required.")
    if s.auth == "pat" and not s.token.strip():
        errors.append("A programmatic access token is required.")
    for name in ("role", "warehouse", "database", "schema"):
        value = getattr(s, name).strip()
        if value and not _IDENT.match(value):
            errors.append(f"{name.capitalize()} is not a valid identifier: {value!r}.")
    return errors


def pem_to_der_b64(pem: str, passphrase: str | None = None) -> str:
    """The connector wants a private key as base64 DER; people have PEM (.p8).
    Encrypted keys are decrypted here with the passphrase."""
    from cryptography.hazmat.primitives import serialization

    key = serialization.load_pem_private_key(
        pem.strip().encode(), password=passphrase.encode() if passphrase else None
    )
    der = key.private_bytes(
        encoding=serialization.Encoding.DER,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    return base64.b64encode(der).decode()


def connector_params(s: ConnectionSettings) -> dict:
    """Keyword arguments for snowflake.connector / Snowpark Session.builder."""
    errors = validate(s)
    if errors:
        raise ValueError(" ".join(errors))
    params: dict = {"account": normalize_account(s.account), "user": s.user.strip()}
    for name in ("role", "warehouse", "database", "schema"):
        if getattr(s, name).strip():
            params[name] = getattr(s, name).strip()
    if s.auth == "keypair":
        params["authenticator"] = "SNOWFLAKE_JWT"
        params["private_key"] = pem_to_der_b64(s.private_key_pem, s.private_key_passphrase or None)
    elif s.auth == "password":
        params["password"] = s.password
        if s.passcode.strip():
            params["passcode"] = s.passcode.strip()
    elif s.auth == "pat":
        params["authenticator"] = "PROGRAMMATIC_ACCESS_TOKEN"
        params["token"] = s.token.strip()
    elif s.auth == "externalbrowser":
        params["authenticator"] = "externalbrowser"
    return params


def settings_from_secrets(cfg: dict) -> ConnectionSettings:
    """Pre-fill the form from a [connections.snowflake] section (no secrets)."""
    auth = str(cfg.get("authenticator", "")).lower()
    if cfg.get("private_key") or cfg.get("private_key_file") or auth == "snowflake_jwt":
        method = "keypair"
    elif auth == "programmatic_access_token":
        method = "pat"
    elif auth == "externalbrowser":
        method = "externalbrowser"
    elif cfg.get("password"):
        method = "password"
    else:
        method = "keypair"
    return ConnectionSettings(
        account=str(cfg.get("account", "")), user=str(cfg.get("user", "")), auth=method,
        role=str(cfg.get("role", "")), warehouse=str(cfg.get("warehouse", "")),
        database=str(cfg.get("database", "")), schema=str(cfg.get("schema", "")),
    )


def _toml_str(value: str) -> str:
    # A JSON string is a valid TOML basic string (same escapes).
    return json.dumps(value)


def secrets_toml(s: ConnectionSettings, include_secrets: bool = True) -> str:
    """A [connections.snowflake] section for .streamlit/secrets.toml or the
    Community Cloud secrets box. The PEM key is kept as PEM; the app converts
    it on connect."""
    lines = ["[connections.snowflake]",
             f"account = {_toml_str(normalize_account(s.account))}",
             f"user = {_toml_str(s.user.strip())}"]
    for name in ("role", "warehouse", "database", "schema"):
        if getattr(s, name).strip():
            lines.append(f"{name} = {_toml_str(getattr(s, name).strip())}")

    def secret(v: str) -> str:
        return _toml_str(v) if include_secrets else '"<secret>"'

    if s.auth == "keypair":
        lines.append('authenticator = "SNOWFLAKE_JWT"')
        lines.append(f"private_key = {secret(s.private_key_pem.strip())}")
        if s.private_key_passphrase:
            lines.append(f"private_key_passphrase = {secret(s.private_key_passphrase)}")
    elif s.auth == "password":
        lines.append(f"password = {secret(s.password)}")
    elif s.auth == "pat":
        lines.append('authenticator = "PROGRAMMATIC_ACCESS_TOKEN"')
        lines.append(f"token = {secret(s.token.strip())}")
    elif s.auth == "externalbrowser":
        lines.append('authenticator = "externalbrowser"')
    return "\n".join(lines) + "\n"


# ── Key pairs ────────────────────────────────────────────────────────────────


def generate_key_pair(passphrase: str | None = None) -> tuple[str, str]:
    """A new RSA 2048 key pair as (private PKCS#8 PEM, public PEM)."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    enc = (serialization.BestAvailableEncryption(passphrase.encode()) if passphrase
           else serialization.NoEncryption())
    private_pem = key.private_bytes(serialization.Encoding.PEM,
                                    serialization.PrivateFormat.PKCS8, enc).decode()
    public_pem = key.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    ).decode()
    return private_pem, public_pem


def public_key_body(public_pem: str) -> str:
    """The base64 body Snowflake expects in RSA_PUBLIC_KEY."""
    return "".join(l.strip() for l in public_pem.strip().splitlines() if "-----" not in l)


def public_key_from_private(private_pem: str, passphrase: str | None = None) -> str:
    from cryptography.hazmat.primitives import serialization

    key = serialization.load_pem_private_key(
        private_pem.strip().encode(), password=passphrase.encode() if passphrase else None
    )
    return key.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    ).decode()


def alter_user_sql(user: str, public_pem: str) -> str:
    name = user.strip()
    if not name:
        raise ValueError("user is required")
    if not _IDENT.match(name):
        name = '"' + name.replace('"', '""') + '"'
    return f"ALTER USER {name} SET RSA_PUBLIC_KEY = '{public_key_body(public_pem)}'"

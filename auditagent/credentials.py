"""
AuditAgent — Per-User Credentials (encrypted at rest)
Separate concern from auth.py: these are the TARGET-SITE credentials
(AUDIT_REGISTER_URL, AUDIT_EMAIL, etc.) each AuditAgent user configures
for testing their own app — not their AuditAgent login. Encrypted at
rest with a server-side master key (Fernet/AES) so a database leak alone
doesn't expose every user's target-site secrets in plaintext.
"""

import os

from cryptography.fernet import Fernet, InvalidToken

import db

ALLOWED_KEYS = {
    "AUDIT_REGISTER_URL", "AUDIT_LOGIN_URL", "AUDIT_EMAIL", "AUDIT_USERNAME",
    "AUDIT_PASSWORD", "AUDIT_BEARER_TOKEN", "AUDIT_COOKIES",
    "AUDIT_REGISTER_EMAIL", "AUDIT_LOGIN_FIELD_USER", "AUDIT_LOGIN_FIELD_PASS",
}

SECRET_KEYS = {"AUDIT_PASSWORD", "AUDIT_BEARER_TOKEN", "AUDIT_COOKIES"}


def _get_master_key() -> bytes:
    key = os.environ.get("CREDENTIALS_MASTER_KEY")
    if key:
        return key.encode() if isinstance(key, str) else key

    key_path = os.path.join(os.getcwd(), ".credentials_master.key")
    if os.path.isfile(key_path):
        with open(key_path, "rb") as f:
            return f.read().strip()

    new_key = Fernet.generate_key()
    with open(key_path, "wb") as f:
        f.write(new_key)
    print(f"⚠ No CREDENTIALS_MASTER_KEY set — generated one and saved it to {key_path}. "
          f"Set CREDENTIALS_MASTER_KEY explicitly before deploying this anywhere real — "
          f"losing this file makes all stored per-user credentials permanently unrecoverable.")
    return new_key


_fernet: Fernet | None = None


def _get_fernet() -> Fernet:
    global _fernet
    if _fernet is None:
        key = _get_master_key()
        try:
            _fernet = Fernet(key)
        except ValueError as e:
            # A cryptic "Fernet key must be 32 url-safe base64-encoded
            # bytes" is exactly what you'd get from pasting a generic
            # random string (e.g. from a platform's auto-generate-a-secret
            # feature) instead of a real Fernet key — confirmed this
            # directly. Give an actionable fix, not just the raw error.
            raise RuntimeError(
                f"CREDENTIALS_MASTER_KEY is set but isn't a valid Fernet key ({e}). "
                f"Generate a real one with: python3 -c \"from cryptography.fernet import "
                f"Fernet; print(Fernet.generate_key().decode())\" — a platform's generic "
                f"auto-generated secret will NOT work here; it needs this exact format."
            )
    return _fernet


def init_credentials_table():
    conn = db.get_connection()
    db.execute(conn, """
        CREATE TABLE IF NOT EXISTS user_credentials (
            user_id INTEGER NOT NULL,
            key_name TEXT NOT NULL,
            encrypted_value TEXT NOT NULL,
            PRIMARY KEY (user_id, key_name)
        )
    """)
    conn.commit()
    return conn


def save_user_credential(user_id: int, key_name: str, value: str) -> None:
    if key_name not in ALLOWED_KEYS:
        raise ValueError(f"Unknown credential key: {key_name}")
    conn = init_credentials_table()
    try:
        encrypted = _get_fernet().encrypt(value.encode("utf-8")).decode("utf-8")
        db.execute(
            conn,
            "INSERT INTO user_credentials (user_id, key_name, encrypted_value) VALUES (?, ?, ?) "
            "ON CONFLICT(user_id, key_name) DO UPDATE SET encrypted_value = excluded.encrypted_value",
            (user_id, key_name, encrypted),
        )
        conn.commit()
    finally:
        conn.close()


def delete_user_credential(user_id: int, key_name: str) -> None:
    conn = init_credentials_table()
    try:
        db.execute(conn, "DELETE FROM user_credentials WHERE user_id = ? AND key_name = ?", (user_id, key_name))
        conn.commit()
    finally:
        conn.close()


def get_user_credentials(user_id: int) -> dict:
    """Decrypted credentials for this user, shaped like the AUDIT_* env
    vars active_tester.py already expects — passed in directly instead of
    falling back to os.environ, so one user's configured credentials are
    never visible to another user's test run."""
    conn = init_credentials_table()
    try:
        rows = db.execute(
            conn, "SELECT key_name, encrypted_value FROM user_credentials WHERE user_id = ?", (user_id,)
        ).fetchall()
    finally:
        conn.close()

    result = {}
    for key_name, encrypted in rows:
        try:
            result[key_name] = _get_fernet().decrypt(encrypted.encode("utf-8")).decode("utf-8")
        except InvalidToken:
            continue  # corrupted/undecryptable entry — skip rather than crash the whole request
    return result


def get_user_credential_status(user_id: int) -> dict:
    """Which keys are configured, without ever exposing the actual
    secret values back to the browser."""
    conn = init_credentials_table()
    try:
        rows = db.execute(
            conn, "SELECT key_name FROM user_credentials WHERE user_id = ?", (user_id,)
        ).fetchall()
    finally:
        conn.close()
    configured = {r[0] for r in rows}
    return {key: (key in configured) for key in ALLOWED_KEYS}

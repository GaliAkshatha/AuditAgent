"""
AuditAgent — Auth
Real user accounts for AuditAgent itself (separate from any credentials
used to test a TARGET app — those are a different concern, handled by
credentials.py). This is what makes "private recent runs per account" and
"each person's target-site credentials stay theirs" possible.

Passwords: bcrypt (via the `bcrypt` package directly — a salted, slow
hash purpose-built for this, not something to reimplement).

Sessions: a signed, timestamped token (itsdangerous) stored in an
httponly cookie — the browser can't read or tamper with it via JS, and
the server can verify it wasn't forged without a database lookup on every
single request (only the user_id needs looking up when actually needed).
"""

import os
import re
import secrets
from datetime import datetime, timezone

import bcrypt
from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired
from fastapi import Request, HTTPException

import db

SESSION_MAX_AGE = 60 * 60 * 24 * 30  # 30 days
COOKIE_NAME = "auditagent_session"
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _get_secret_key() -> str:
    """A real production deployment should set SESSION_SECRET_KEY
    explicitly. If it's not set, one is generated and persisted to a local
    file so sessions survive a restart — but this is a fallback for local/
    early development, not a substitute for setting it properly once this
    is actually deployed somewhere real."""
    key = os.environ.get("SESSION_SECRET_KEY")
    if key:
        return key

    secret_path = os.path.join(os.getcwd(), ".session_secret")
    if os.path.isfile(secret_path):
        with open(secret_path) as f:
            return f.read().strip()

    key = secrets.token_urlsafe(32)
    with open(secret_path, "w") as f:
        f.write(key)
    print(f"⚠ No SESSION_SECRET_KEY set — generated one and saved it to {secret_path}. "
          f"Set SESSION_SECRET_KEY explicitly before deploying this anywhere real.")
    return key


_serializer: URLSafeTimedSerializer | None = None


def _get_serializer() -> URLSafeTimedSerializer:
    global _serializer
    if _serializer is None:
        _serializer = URLSafeTimedSerializer(_get_secret_key())
    return _serializer


def init_auth_tables():
    conn = db.get_connection()
    db.execute(conn, f"""
        CREATE TABLE IF NOT EXISTS users (
            id {db.AUTOINCREMENT_PK},
            email TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
    """)
    conn.commit()
    # Migrate the existing `runs` table (from orchestrator.py) to support
    # per-user scoping — nullable so old rows created before accounts
    # existed don't break, new rows always set it.
    if db.table_exists(conn, "runs") and not db.column_exists(conn, "runs", "user_id"):
        db.execute(conn, "ALTER TABLE runs ADD COLUMN user_id INTEGER")
        conn.commit()
    return conn


def validate_email(email: str) -> bool:
    return bool(EMAIL_RE.match(email))


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return bcrypt.checkpw(password.encode("utf-8"), password_hash.encode("utf-8"))
    except ValueError:
        return False  # malformed hash — fail closed, never raise into a 500


def register_user(email: str, password: str) -> dict:
    email = email.strip().lower()
    if not validate_email(email):
        raise ValueError("Invalid email address")
    if len(password) < 8:
        raise ValueError("Password must be at least 8 characters")

    conn = init_auth_tables()
    try:
        existing = db.execute(conn, "SELECT id FROM users WHERE email = ?", (email,)).fetchone()
        if existing:
            raise ValueError("An account with this email already exists")

        password_hash = hash_password(password)
        user_id = db.insert_returning_id(
            conn,
            "INSERT INTO users (email, password_hash, created_at) VALUES (?, ?, ?)",
            (email, password_hash, datetime.now(timezone.utc).isoformat()),
        )
        conn.commit()
        return {"id": user_id, "email": email}
    finally:
        conn.close()


def authenticate_user(email: str, password: str) -> dict | None:
    email = email.strip().lower()
    conn = init_auth_tables()
    try:
        row = db.execute(
            conn, "SELECT id, email, password_hash FROM users WHERE email = ?", (email,)
        ).fetchone()
    finally:
        conn.close()

    if not row:
        return None
    user_id, user_email, password_hash = row
    if not verify_password(password, password_hash):
        return None
    return {"id": user_id, "email": user_email}


def create_session_token(user_id: int) -> str:
    return _get_serializer().dumps({"user_id": user_id})


def verify_session_token(token: str) -> int | None:
    try:
        data = _get_serializer().loads(token, max_age=SESSION_MAX_AGE)
        return data.get("user_id")
    except (BadSignature, SignatureExpired):
        return None


def get_user_by_id(user_id: int) -> dict | None:
    conn = init_auth_tables()
    try:
        row = db.execute(conn, "SELECT id, email FROM users WHERE id = ?", (user_id,)).fetchone()
    finally:
        conn.close()
    if not row:
        return None
    return {"id": row[0], "email": row[1]}


def get_current_user_id(request: Request) -> int:
    """FastAPI dependency — raises 401 if not logged in. Use on any
    endpoint that should require an account."""
    token = request.cookies.get(COOKIE_NAME)
    if not token:
        raise HTTPException(401, "Not logged in")
    user_id = verify_session_token(token)
    if user_id is None:
        raise HTTPException(401, "Session expired or invalid — please log in again")
    return user_id


def get_current_user_id_optional(request: Request) -> int | None:
    """Same as above but returns None instead of raising — for endpoints
    that behave differently for logged-in vs. anonymous users rather than
    strictly requiring an account (e.g. an unauthenticated demo mode)."""
    token = request.cookies.get(COOKIE_NAME)
    if not token:
        return None
    return verify_session_token(token)

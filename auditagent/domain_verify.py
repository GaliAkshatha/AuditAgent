"""
AuditAgent — Domain Ownership Verification
Before allowing the more invasive parts of an audit (the concurrency
probe, which deliberately sends concurrent load — a legitimate diagnostic
against your own site, but indistinguishable from abuse against someone
else's) against a domain the tool doesn't already know you control, we
require proof of ownership. Same standard pattern used by Google Search
Console, Let's Encrypt (ACME HTTP-01), and most SaaS security scanners:

    1. Generate a random token for the domain.
    2. The person proves control by either:
       a) Publishing it at https://{domain}/.well-known/auditagent-verify.txt
       b) Adding it as a DNS TXT record at _auditagent.{domain}
    3. We check both; either one passing = verified.

Deliberately does NOT require email/SMTP infrastructure — both methods
only need things a real site owner already has (file upload access, or
DNS management access), and both are well-established, low-friction
patterns people running real infrastructure already recognize.

Passive, read-only crawling (no concurrency probe, no auth) remains
unverified-domain-safe — it's no more invasive than what any search
engine crawler already does, and it respects robots.txt. Verification
gates the parts of the tool that go beyond that.
"""

import os
import secrets
import time
from datetime import datetime, timedelta, timezone

import requests

import db

VERIFICATION_TTL_DAYS = 30
WELL_KNOWN_PATH = "/.well-known/auditagent-verify.txt"
DNS_TXT_SUBDOMAIN = "_auditagent"


def init_verification_table():
    conn = db.get_connection()
    db.execute(conn, """
        CREATE TABLE IF NOT EXISTS domain_verifications (
            domain TEXT PRIMARY KEY,
            token TEXT NOT NULL,
            verified INTEGER NOT NULL DEFAULT 0,
            method TEXT,
            verified_at TEXT,
            expires_at TEXT,
            created_at TEXT NOT NULL
        )
    """)
    conn.commit()
    return conn


def start_verification(domain: str) -> dict:
    """Generates (or returns the existing unexpired) token for a domain,
    along with both sets of instructions for proving control."""
    conn = init_verification_table()
    row = db.execute(
        conn, "SELECT token, verified, expires_at FROM domain_verifications WHERE domain = ?", (domain,)
    ).fetchone()

    if row and row[1] and row[2] and datetime.fromisoformat(row[2]) > datetime.now(timezone.utc):
        token = row[0]  # already verified and not expired — reuse the token
    elif row:
        token = row[0]  # unverified attempt exists — reuse so old instructions stay valid
    else:
        token = secrets.token_urlsafe(24)
        db.execute(
            conn,
            "INSERT INTO domain_verifications (domain, token, verified, created_at) VALUES (?, ?, 0, ?)",
            (domain, token, datetime.now(timezone.utc).isoformat()),
        )
        conn.commit()
    conn.close()

    return {
        "domain": domain,
        "token": token,
        "well_known": {
            "url": f"https://{domain}{WELL_KNOWN_PATH}",
            "instructions": f"Create a file at {WELL_KNOWN_PATH} on your site containing exactly: {token}",
        },
        "dns_txt": {
            "record_name": f"{DNS_TXT_SUBDOMAIN}.{domain}",
            "record_value": token,
            "instructions": f"Add a DNS TXT record for {DNS_TXT_SUBDOMAIN}.{domain} with value: {token}",
        },
    }


def check_well_known(domain: str, token: str, timeout: float = 8.0) -> bool:
    for scheme in ("https", "http"):
        try:
            resp = requests.get(f"{scheme}://{domain}{WELL_KNOWN_PATH}", timeout=timeout)
            if resp.status_code == 200 and token in resp.text:
                return True
        except requests.RequestException:
            continue
    return False


def check_dns_txt(domain: str, token: str, timeout: float = 8.0) -> bool:
    """Uses DNS-over-HTTPS (Cloudflare's resolver) rather than raw DNS
    sockets — works through normal HTTPS, no special network permissions
    needed, and doesn't require the dnspython dependency."""
    record_name = f"{DNS_TXT_SUBDOMAIN}.{domain}"
    try:
        resp = requests.get(
            "https://cloudflare-dns.com/dns-query",
            params={"name": record_name, "type": "TXT"},
            headers={"Accept": "application/dns-json"},
            timeout=timeout,
        )
        if resp.status_code != 200:
            return False
        data = resp.json()
        for answer in data.get("Answer", []):
            # TXT record data comes back quoted, e.g. "\"abc123\""
            if token in answer.get("data", ""):
                return True
    except (requests.RequestException, ValueError):
        pass
    return False


def check_verification(domain: str) -> dict:
    """Runs both checks, updates the DB if either passes, returns current status."""
    conn = init_verification_table()
    row = db.execute(conn, "SELECT token FROM domain_verifications WHERE domain = ?", (domain,)).fetchone()
    if not row:
        conn.close()
        return {"domain": domain, "verified": False, "error": "No verification started for this domain yet"}

    token = row[0]
    method = None
    if check_well_known(domain, token):
        method = "well_known"
    elif check_dns_txt(domain, token):
        method = "dns_txt"

    if method:
        now = datetime.now(timezone.utc)
        expires = now + timedelta(days=VERIFICATION_TTL_DAYS)
        db.execute(
            conn,
            "UPDATE domain_verifications SET verified = 1, method = ?, verified_at = ?, expires_at = ? WHERE domain = ?",
            (method, now.isoformat(), expires.isoformat(), domain),
        )
        conn.commit()
        conn.close()
        return {"domain": domain, "verified": True, "method": method, "expires_at": expires.isoformat()}

    conn.close()
    return {"domain": domain, "verified": False}


def is_verified(domain: str) -> bool:
    """Fast check used to gate features — does NOT re-verify over the
    network, just checks the stored (and unexpired) result."""
    conn = init_verification_table()
    row = db.execute(
        conn, "SELECT verified, expires_at FROM domain_verifications WHERE domain = ?", (domain,)
    ).fetchone()
    conn.close()
    if not row or not row[0] or not row[1]:
        return False
    return datetime.fromisoformat(row[1]) > datetime.now(timezone.utc)

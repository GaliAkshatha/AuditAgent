"""
AuditAgent — Active Tester (opt-in — use with a demo/test account only)
Actually calls the mutating endpoints (POST/PATCH/PUT/DELETE) discovered
by static_analysis.py, using generic test payloads, and records every
call through test_ledger.py so the whole run can be rolled back
afterward. This is what turns "we found 44 endpoints nobody's watching"
into "we actually exercised them and here's what happened" — the natural
next step after passive crawling and static analysis, gated behind
everything this project has built specifically to make that safe:

    - NOT part of the default pipeline — must be explicitly invoked.
    - Credentials come from environment variables ONLY (never a CLI arg,
      never a request body) — this is what keeps active testing
      structurally incompatible with being exposed through the shared
      multi-user web app. api.py never accepts credentials in any
      request; there's nowhere for them to go.
    - Every mutation is recorded in a TestLedger and can be rolled back
      via test_ledger.rollback() — reverse-dependency-ordered, not just
      reverse-chronological (see test_ledger.py for why that matters).
    - Should only ever run against a demo/test account on a domain you've
      verified ownership of (domain_verify.py) — rollback is a safety
      net on top of that, not a replacement for it.

Credentials env vars:
    AUDIT_COOKIES="session=abc123; other=xyz"   (a real Cookie-header string)

Usage:
    python active_tester.py https://staging.example.com endpoints.json --dry-run
    python active_tester.py https://staging.example.com endpoints.json
    python active_tester.py --rollback ledger.json
"""

import argparse
import json
import os
import re
import secrets
import sys
import time

import requests

from test_ledger import TestLedger, rollback as run_rollback

try:
    from dotenv import load_dotenv
    _DOTENV_AVAILABLE = True
except ImportError:
    _DOTENV_AVAILABLE = False

TEST_MARKER = "AuditAgent test data — safe to delete"


def build_authenticated_session(env: dict | None = None) -> tuple[requests.Session, dict | None]:
    """Builds a requests.Session with whatever credentials are available.
    Returns (session, auto_created_account) — the second value is None
    unless AUDIT_REGISTER_URL triggered a fresh account creation, in which
    case it's {"email": ...} so the caller can attempt best-effort
    self-cleanup afterward (see attempt_self_cleanup).

    `env`: a dict of AUDIT_* credentials to use instead of process
    environment variables — this is what makes per-user credentials work
    (api.py passes in one user's decrypted credentials.py values here, so
    one user's configured target-site login never leaks into another
    user's test run). Defaults to os.environ (re-read fresh via
    load_dotenv(override=True)) for CLI/self-hosted usage, where there's
    only ever one operator and .env is the natural place for this.

    Precedence: AUDIT_BEARER_TOKEN (already have a token) > AUDIT_REGISTER_URL
    (create a fresh throwaway account) > AUDIT_LOGIN_URL + credentials
    (log into an existing demo account)."""
    if env is None:
        if _DOTENV_AVAILABLE:
            load_dotenv(override=True)
        env = os.environ

    session = requests.Session()

    cookies = env.get("AUDIT_COOKIES")
    if cookies:
        for part in cookies.split(";"):
            if "=" in part:
                k, v = part.strip().split("=", 1)
                session.cookies.set(k, v)

    token = env.get("AUDIT_BEARER_TOKEN")
    if token:
        session.headers["Authorization"] = f"Bearer {token}"
        return session, None

    register_url = env.get("AUDIT_REGISTER_URL")
    if register_url:
        token, account_info = _attempt_auto_register(session, register_url, env)
        if token:
            session.headers["Authorization"] = f"Bearer {token}"
        return session, account_info

    login_url = env.get("AUDIT_LOGIN_URL")
    if login_url:
        token = _attempt_auto_login(session, login_url, env)
        if token:
            session.headers["Authorization"] = f"Bearer {token}"

    return session, None


# Common field-name variants across real apps — tried in order until one
# gets a 2xx response. Configurable overrides (AUDIT_LOGIN_FIELD_USER /
# AUDIT_LOGIN_FIELD_PASS) let you skip guessing entirely if you already
# know your app's exact field names.
_USER_FIELD_CANDIDATES = ["email", "username", "user"]
_TOKEN_RESPONSE_KEYS = [
    "accessToken", "access_token", "token", "jwt", "authToken",
    "sessionToken", "idToken", "bearerToken", "auth_token", "session_token",
    "sessionId", "session_id",
]


def _extract_token(body: dict) -> tuple[str | None, list[str]]:
    """Returns (token, keys_seen) — keys_seen is every top-level and
    one-level-nested key actually present in the response, regardless of
    whether extraction succeeded. This is what makes a failure
    diagnosable instead of a silent dead end: if extraction fails, we can
    tell you exactly what field names the response DID have, so you can
    see immediately whether it's a naming mismatch (fixable by adding a
    key) rather than guessing blindly."""
    keys_seen = list(body.keys())

    for key in _TOKEN_RESPONSE_KEYS:
        if key in body and isinstance(body[key], str):
            return body[key], keys_seen

    # Nested up to two levels deep — covers shapes like
    # {"data": {"accessToken": "..."}} and {"data": {"user": {...},
    # "tokens": {"accessToken": "..."}}}
    for value in body.values():
        if isinstance(value, dict):
            keys_seen.extend(f"{k} (nested)" for k in value.keys())
            for key in _TOKEN_RESPONSE_KEYS:
                if key in value and isinstance(value[key], str):
                    return value[key], keys_seen
            for inner_value in value.values():
                if isinstance(inner_value, dict):
                    keys_seen.extend(f"{k} (nested x2)" for k in inner_value.keys())
                    for key in _TOKEN_RESPONSE_KEYS:
                        if key in inner_value and isinstance(inner_value[key], str):
                            return inner_value[key], keys_seen

    return None, keys_seen


def _generate_test_email(env: dict) -> str:
    """Uses AUDIT_REGISTER_EMAIL as a template if given (uniquified via
    '+' aliasing if it contains '@' — works with Gmail and many providers,
    though not universally supported, so a real domain you control is
    the safer choice if your app requires email verification). Falls back
    to a clearly-tagged placeholder if no template is given — fine unless
    your app requires a deliverable inbox.

    Uses a high-resolution timestamp plus a short random suffix, not just
    whole seconds — two runs within the same second would otherwise
    generate the identical email and collide with a real 409 Conflict on
    the second registration attempt (caught this during testing)."""
    import time
    tag = f"{int(time.time())}{secrets.token_hex(3)}"
    template = env.get("AUDIT_REGISTER_EMAIL")
    if template and "@" in template:
        local, domain = template.split("@", 1)
        return f"{local}+auditagent{tag}@{domain}"
    return f"auditagent-test-{tag}@example.com"


def _attempt_auto_register(session: requests.Session, register_url: str, env: dict) -> tuple[str | None, dict | None]:
    email = _generate_test_email(env)
    password = env.get("AUDIT_PASSWORD") or secrets.token_urlsafe(12) + "!A1"
    user_field = env.get("AUDIT_LOGIN_FIELD_USER")
    pass_field = env.get("AUDIT_LOGIN_FIELD_PASS", "password")
    user_fields_to_try = [user_field] if user_field else _USER_FIELD_CANDIDATES

    for field_name in user_fields_to_try:
        payload = {
            field_name: email, pass_field: password,
            "name": "AuditAgent Test User", "confirmPassword": password,
            "note": TEST_MARKER,
        }
        try:
            resp = session.post(register_url, json=payload, timeout=10)
        except requests.RequestException as e:
            print(f"⚠ Auto-register request failed: {e}")
            return None, None

        if 200 <= resp.status_code < 300:
            print(f"✓ Auto-register succeeded — created {email} (field: \"{field_name}\")")
            account_info = {"email": email}
            try:
                body = resp.json()
            except (ValueError, json.JSONDecodeError):
                body = None

            token, keys_seen = (_extract_token(body) if isinstance(body, dict) else (None, []))
            if token:
                return token, account_info

            # Registration succeeded but no token was found in the
            # response — this is the exact "it registered but can't use
            # the account" scenario. Show the ACTUAL keys the response
            # had, not just "failed" — this is the difference between a
            # silent dead end and something you can actually fix (e.g.
            # "oh, it's called sessionId, not accessToken").
            if body is not None:
                print(f"  ⚠ Registered successfully, but no recognizable token field in the response. "
                      f"Response contained: {keys_seen or '(empty object)'}")
            else:
                print(f"  ⚠ Registered successfully (status {resp.status_code}), but the response "
                      f"wasn't valid JSON — can't look for a token in it.")

            # Registration didn't return a token directly — try logging in
            # with the account we just created. AUDIT_LOGIN_URL if given,
            # else guess the sibling login endpoint (a very common
            # convention: .../register -> .../login).
            login_url = env.get("AUDIT_LOGIN_URL") or register_url.replace("register", "login")
            print(f"  Attempting login at {login_url} with the account just created…")
            try:
                login_resp = session.post(login_url, json={field_name: email, pass_field: password}, timeout=10)
                if 200 <= login_resp.status_code < 300:
                    login_body = login_resp.json()
                    login_token, login_keys_seen = (
                        _extract_token(login_body) if isinstance(login_body, dict) else (None, [])
                    )
                    if login_token:
                        print("  ✓ Follow-up login succeeded")
                        return login_token, account_info
                    print(f"  ⚠ Login succeeded (status {login_resp.status_code}) but STILL no "
                          f"recognizable token field. Response contained: {login_keys_seen or '(empty object)'}. "
                          f"If you know the real field name, set AUDIT_BEARER_TOKEN directly instead — "
                          f"log in manually once and paste the token in.")
                else:
                    print(f"  ⚠ Follow-up login got {login_resp.status_code} — account was created but "
                          f"couldn't be used this run (possibly requires email verification first).")
            except requests.RequestException as e:
                print(f"  ⚠ Follow-up login failed: {e}")
            return None, account_info  # account exists even though we couldn't get a token

    print(f"⚠ Auto-register failed — tried field names {user_fields_to_try}, none succeeded.")
    return None, None


def diagnose_failure(session: requests.Session, method: str, url: str, payload: dict | None,
                      attempts: int = 4, delay_s: float = 2.5, patient: bool = False) -> dict:
    """Re-sends the exact same request multiple times, with a pause
    between each, and classifies the failure pattern — this is the
    automated version of "is this a real bug, or something environmental
    (cold start, connection pool exhaustion, a transient blip)?" Opt-in,
    only run when explicitly requested (a single failed test isn't proof
    of anything; a repeated, controlled retry pattern is actual evidence).

    Classification logic, based on which attempts succeed/fail:
        - all attempts get the identical status: CONSISTENT failure —
          the strongest signal of a real, deterministic bug in the
          target's code, since environmental issues don't reliably
          reproduce identically every single time.
        - only the FIRST attempt fails, all others succeed: matches a
          COLD START pattern — the service needed to wake up/warm up,
          and only paid that cost once.
        - a mix that doesn't fit either pattern: INTERMITTENT — points
          at something load/timing-dependent (a small connection pool,
          a race condition, rate limiting) rather than a fixed code bug.
        - the failure didn't reproduce at all: the original failure may
          have been a one-off blip; worth noting, not something to chase
          further without more evidence."""
    results = []
    for i in range(attempts):
        if i > 0:
            time.sleep(delay_s)
        t0 = time.time()
        try:
            resp = session.request(method, url, json=payload, timeout=90 if patient else 15)
            elapsed_ms = round((time.time() - t0) * 1000)
            results.append({"attempt": i + 1, "status": resp.status_code, "elapsed_ms": elapsed_ms})
        except requests.RequestException as e:
            elapsed_ms = round((time.time() - t0) * 1000)
            results.append({"attempt": i + 1, "status": None, "error": str(e), "elapsed_ms": elapsed_ms})

    statuses = [r["status"] for r in results]
    first_failed = statuses[0] is None or statuses[0] >= 400
    rest_all_ok = all(s is not None and s < 400 for s in statuses[1:])
    all_identical = len(set(statuses)) == 1
    any_failed = any(s is None or s >= 400 for s in statuses)

    if not any_failed:
        classification = "not_reproduced"
        explanation = ("The failure didn't happen again across "
                        f"{attempts} attempts — the original failure may have been a one-off blip "
                        "rather than something to chase further right now.")
    elif all_identical:
        classification = "consistent_failure"
        explanation = (f"All {attempts} attempts failed identically (status {statuses[0]}) — "
                        "this points to a real, deterministic bug in the target's code, since "
                        "environmental issues (cold start, load) don't reliably reproduce the exact "
                        "same way every single time. Worth checking the target's own server logs for "
                        "the specific error behind this status code.")
    elif first_failed and rest_all_ok:
        classification = "cold_start_pattern"
        first_ms = results[0]["elapsed_ms"]
        rest_ms = [r["elapsed_ms"] for r in results[1:]]
        avg_rest_ms = round(sum(rest_ms) / len(rest_ms)) if rest_ms else 0
        timing_note = ""
        if avg_rest_ms > 0 and first_ms > avg_rest_ms * 2:
            timing_note = (f" The timing backs this up: the first attempt took {first_ms}ms "
                            f"vs. an average of {avg_rest_ms}ms after — consistent with the service "
                            f"actually being slow to wake up, not just coincidentally erroring once.")
        explanation = ("Only the FIRST attempt failed; every retry after it succeeded. This matches "
                        "a cold-start pattern — the service (or a database connection, cache, etc. it "
                        "depends on) needed to wake up, and only paid that cost once. Common on "
                        "free-tier hosting that spins down idle services." + timing_note)
    else:
        classification = "intermittent_failure"
        explanation = ("Failures were scattered across attempts, not consistent and not limited to "
                        "just the first one. This points to something load- or timing-dependent — a "
                        "small database connection pool being exhausted under repeated requests, a "
                        "race condition, or rate limiting — rather than a fixed, always-reproduces bug.")

    return {
        "method": method, "url": url, "attempts": results,
        "classification": classification, "explanation": explanation,
    }


def attempt_self_cleanup(session: requests.Session, base_url: str, endpoints: list[dict], account_info: dict) -> None:
    """Best-effort only — many apps don't expose self-deletion via the
    API at all, in which case this just tells you so instead of pretending
    to have cleaned up. Looks for a DELETE endpoint matching common
    'delete my own account' path patterns."""
    candidates = [
        e for e in endpoints
        if e["method"] == "DELETE" and any(kw in e["path"].lower() for kw in ("/me", "/self", "/account"))
    ]
    if not candidates:
        print(f"⚠ No self-delete endpoint found — the auto-created test account "
              f"({account_info['email']}) was NOT automatically removed. Delete it manually if needed.")
        return

    path = candidates[0]["path"]
    url = base_url.rstrip("/") + path
    try:
        resp = session.delete(url, timeout=10)
        if resp.status_code < 400:
            print(f"✓ Self-cleanup: deleted the auto-created test account via {path} ({resp.status_code})")
        else:
            print(f"⚠ Self-cleanup attempted via {path} but got {resp.status_code} — "
                  f"account ({account_info['email']}) may still exist.")
    except requests.RequestException as e:
        print(f"⚠ Self-cleanup request failed: {e}")


def _attempt_auto_login(session: requests.Session, login_url: str, env: dict) -> str | None:
    password = env.get("AUDIT_PASSWORD")
    identifier = env.get("AUDIT_EMAIL") or env.get("AUDIT_USERNAME")
    if not password or not identifier:
        print("⚠ AUDIT_LOGIN_URL is set but AUDIT_EMAIL/AUDIT_USERNAME or AUDIT_PASSWORD is missing — skipping auto-login.")
        return None

    user_field = env.get("AUDIT_LOGIN_FIELD_USER")
    pass_field = env.get("AUDIT_LOGIN_FIELD_PASS", "password")
    user_fields_to_try = [user_field] if user_field else _USER_FIELD_CANDIDATES

    for field_name in user_fields_to_try:
        try:
            resp = session.post(login_url, json={field_name: identifier, pass_field: password}, timeout=10)
        except requests.RequestException as e:
            print(f"⚠ Auto-login request failed: {e}")
            return None

        if 200 <= resp.status_code < 300:
            try:
                body = resp.json()
            except (ValueError, json.JSONDecodeError):
                body = None
            token, keys_seen = (_extract_token(body) if isinstance(body, dict) else (None, []))
            if token:
                print(f"✓ Auto-login succeeded (field: \"{field_name}\")")
                return token
            print(f"⚠ Login got a {resp.status_code} but no recognizable token field in the response. "
                  f"Response contained: {keys_seen or '(empty object)'}. "
                  f"If you know the real field name, set AUDIT_BEARER_TOKEN directly instead.")
            return None

    print(f"⚠ Auto-login failed — tried field names {user_fields_to_try}, none succeeded. "
          f"Set AUDIT_LOGIN_FIELD_USER explicitly if your app uses a different field name, "
          f"or set AUDIT_BEARER_TOKEN directly instead.")
    return None


def _generate_test_payload() -> dict:
    """A generic, clearly-marked placeholder payload. Honest limitation:
    without a schema, we can't know what fields a given endpoint actually
    requires — this is a heuristic starting point, not a guarantee. Any
    endpoint that rejects it (422/400) gets reported as skipped, not
    silently retried with guesses."""
    return {"name": "AuditAgent Test", "title": "AuditAgent Test", "note": TEST_MARKER}


def _safe_json(resp) -> dict | list | None:
    try:
        return resp.json()
    except (ValueError, json.JSONDecodeError):
        return None


def detect_register_endpoint(base_url: str, endpoints: list[dict]) -> str | None:
    """Scans already-discovered endpoints for a likely registration route
    — no reason to make someone manually type a URL the audit already
    found via static analysis. Looks for a POST route with "register" or
    "signup" in its path, case-insensitive; returns the first match's
    full URL, or None if nothing looks like one."""
    candidates = ["register", "signup", "sign-up", "sign_up"]
    for ep in endpoints:
        if ep.get("method") != "POST":
            continue
        path_lower = ep.get("path", "").lower()
        if any(c in path_lower for c in candidates):
            return base_url.rstrip("/") + ep["path"]
    return None


def group_endpoints_by_resource(endpoints: list[dict]) -> dict[str, dict]:
    """Groups endpoints like GET/POST /items with GET/PATCH/DELETE
    /items/:id under one resource key ('/items') — separating
    collection-level operations (create, list) from member-level ones
    (operate on one specific resource by id). Handles Express (:id),
    FastAPI/Flask (<id> or {id}) path-param styles."""
    groups: dict[str, dict] = {}
    param_pattern = re.compile(r"^(:\w+|\{[^}]+\}|<[^>]+>)$")

    for ep in endpoints:
        path = ep["path"]
        parts = [p for p in path.rstrip("/").split("/") if p != ""]
        param_idx = next((i for i, p in enumerate(parts) if param_pattern.match(p)), None)

        if param_idx is not None:
            base = "/" + "/".join(parts[:param_idx])
            groups.setdefault(base, {"collection": [], "member": []})
            groups[base]["member"].append(ep)
        else:
            base = "/" + "/".join(parts)
            groups.setdefault(base, {"collection": [], "member": []})
            groups[base]["collection"].append(ep)

    return groups


def _build_member_url(base_url: str, member_path: str, resource_id: str) -> str:
    """Substitutes the real created-resource id into a member endpoint's
    path-param placeholder, e.g. '/items/:id' -> '.../items/42'."""
    parts = member_path.rstrip("/").split("/")
    param_pattern = re.compile(r"^(:\w+|\{[^}]+\}|<[^>]+>)$")
    parts = [str(resource_id) if param_pattern.match(p) else p for p in parts]
    return base_url.rstrip("/") + "/".join(parts)


class ActiveTester:
    def __init__(self, base_url: str, endpoints: list[dict], session: requests.Session | None = None):
        self.base_url = base_url.rstrip("/")
        self.endpoints = endpoints
        self.session = session or requests.Session()
        self.ledger = TestLedger()
        self.report: list[dict] = []

    def run(self, dry_run: bool = True) -> dict:
        groups = group_endpoints_by_resource(self.endpoints)

        for base_path, group in groups.items():
            post_ep = next((e for e in group["collection"] if e["method"] == "POST"), None)

            if post_ep:
                self._test_one_resource_group(base_path, post_ep, group["member"], dry_run)
                continue

            # No POST to anchor a test resource on. Previously this meant
            # the whole group vanished from the report with zero trace —
            # a real, silent coverage gap, not just an incomplete one.
            # Now: standalone GETs are safe to test directly (read-only,
            # nothing to create or clean up), so we do. Anything mutating
            # (PATCH/PUT/DELETE) with no POST is reported as explicitly
            # skipped, with the real reason — testing it would mean
            # modifying a specific pre-existing resource we didn't create
            # and don't control the identity of, which isn't something to
            # do automatically without knowing what's actually behind
            # that id.
            standalone_gets = [e for e in group["collection"] if e["method"] == "GET"]
            for get_ep in standalone_gets:
                self._test_standalone_get(base_path, get_ep, dry_run)

            unsafe_mutating = [
                e for e in group["member"] + group["collection"]
                if e["method"] in ("PATCH", "PUT", "DELETE")
            ]
            if unsafe_mutating:
                self._report_skipped_group(base_path, unsafe_mutating)

        return {
            "dry_run": dry_run,
            "resource_groups_tested": len(self.report),
            "report": self.report,
            "ledger": self.ledger.to_dict() if not dry_run else None,
        }

    def _test_standalone_get(self, base_path: str, get_ep: dict, dry_run: bool):
        """A GET with no corresponding POST — always safe to test
        directly since it's read-only, nothing created or needing
        cleanup."""
        full_url = self.base_url + get_ep["path"]
        entry = {"resource": base_path, "steps": []}
        if dry_run:
            entry["steps"].append({"method": "GET", "url": full_url, "planned": True})
            self.report.append(entry)
            return
        try:
            r = self.session.get(full_url, timeout=10)
            entry["steps"].append({"method": "GET", "url": full_url, "status": r.status_code})
        except requests.RequestException as e:
            entry["steps"].append({"method": "GET", "url": full_url, "error": str(e)})
        self.report.append(entry)

    def _report_skipped_group(self, base_path: str, unsafe_endpoints: list[dict]):
        """Makes a skip VISIBLE instead of silent — you can now see
        exactly which endpoints weren't tested and the real safety reason
        why, rather than the group just not appearing anywhere."""
        entry = {"resource": base_path, "steps": [], "skipped": True}
        for ep in unsafe_endpoints:
            entry["steps"].append({
                "method": ep["method"], "url": self.base_url + ep["path"],
                "skipped_reason": "No POST endpoint found to create a test resource for this group — "
                                   "testing this would mean modifying a specific pre-existing resource "
                                   "whose identity and ownership we don't control, which isn't safe to "
                                   "do automatically.",
            })
        self.report.append(entry)

    def _test_one_resource_group(self, base_path: str, post_ep: dict, member_eps: list[dict], dry_run: bool):
        full_url = self.base_url + base_path
        payload = _generate_test_payload()
        entry = {"resource": base_path, "steps": []}

        if dry_run:
            entry["steps"].append({"method": "POST", "url": full_url, "body": payload, "planned": True})
            for m in member_eps:
                entry["steps"].append({"method": m["method"], "url": f"{full_url}/{{id}}", "planned": True})
            self.report.append(entry)
            return

        try:
            resp = self.session.post(full_url, json=payload, timeout=10)
        except requests.RequestException as e:
            entry["steps"].append({"method": "POST", "url": full_url, "error": str(e)})
            self.report.append(entry)
            return

        resp_body = _safe_json(resp)
        self.ledger.record("POST", full_url, payload, resp.status_code, resp_body)
        entry["steps"].append({"method": "POST", "url": full_url, "status": resp.status_code, "body": payload})

        if not (200 <= resp.status_code < 300):
            entry["steps"][-1]["note"] = "Creation failed or rejected the test payload — resource group skipped beyond this point"
            self.report.append(entry)
            return

        resource_id = None
        if isinstance(resp_body, dict):
            for key in ("id", "_id", "uuid", "ID", "Id"):
                if key in resp_body:
                    resource_id = str(resp_body[key])
                    break

        if not resource_id:
            entry["steps"][-1]["note"] = "Created, but no id found in response — can't test member endpoints or auto-compensate this creation"
            self.report.append(entry)
            return

        for member_ep in member_eps:
            member_url = _build_member_url(self.base_url, member_ep["path"], resource_id)
            method = member_ep["method"]

            if method == "GET":
                try:
                    r = self.session.get(member_url, timeout=10)
                    entry["steps"].append({"method": "GET", "url": member_url, "status": r.status_code})
                except requests.RequestException as e:
                    entry["steps"].append({"method": "GET", "url": member_url, "error": str(e)})
                continue

            if method in ("PATCH", "PUT"):
                pre_state = _safe_json(self.session.get(member_url, timeout=10))
                patch_payload = {"name": "AuditAgent Test (patched)", "note": TEST_MARKER}
                try:
                    r = self.session.request(method, member_url, json=patch_payload, timeout=10)
                    self.ledger.record(method, member_url, patch_payload, r.status_code, _safe_json(r), pre_state=pre_state)
                    entry["steps"].append({"method": method, "url": member_url, "status": r.status_code, "body": patch_payload})
                except requests.RequestException as e:
                    entry["steps"].append({"method": method, "url": member_url, "error": str(e)})
                continue

            if method == "DELETE":
                pre_state = _safe_json(self.session.get(member_url, timeout=10))
                try:
                    r = self.session.delete(member_url, timeout=10)
                    self.ledger.record("DELETE", member_url, None, r.status_code, _safe_json(r), pre_state=pre_state)
                    entry["steps"].append({"method": "DELETE", "url": member_url, "status": r.status_code})
                except requests.RequestException as e:
                    entry["steps"].append({"method": "DELETE", "url": member_url, "error": str(e)})

        self.report.append(entry)


def main():
    parser = argparse.ArgumentParser(description="AuditAgent — Active Tester (opt-in, demo/test accounts only)")
    parser.add_argument("base_url", nargs="?", help="Base URL of the app to test")
    parser.add_argument("endpoints_file", nargs="?", help="Path to static_analysis.py's JSON output")
    parser.add_argument("--dry-run", action="store_true", help="Show what WOULD be tested without making any requests")
    parser.add_argument("--output", type=str, default="active_test_ledger.json", help="Where to save the ledger (for later rollback)")
    parser.add_argument("--rollback", type=str, default=None, help="Path to a saved ledger JSON to roll back, instead of running new tests")
    parser.add_argument("--execute-rollback", action="store_true", help="With --rollback: actually execute it (dry-run is the safe default otherwise)")
    args = parser.parse_args()

    if args.rollback:
        ledger = TestLedger.load(args.rollback)
        session, _ = build_authenticated_session()
        result = run_rollback(ledger, session=session, dry_run=not args.execute_rollback)
        print(json.dumps(result, indent=2, default=str))
        return

    if not args.base_url or not args.endpoints_file:
        parser.error("base_url and endpoints_file are required unless using --rollback")

    with open(args.endpoints_file) as f:
        endpoints_data = json.load(f)
    endpoints = endpoints_data.get("endpoints", endpoints_data)

    if args.dry_run:
        # Dry-run must have ZERO side effects — auto-register performs a
        # REAL account creation, so it must never run here. Caught this by
        # actually running --dry-run and watching it register a real user
        # anyway, which defeats the entire point of a dry run.
        session, auto_created_account = requests.Session(), None
        if os.environ.get("AUDIT_REGISTER_URL"):
            print("(dry run — skipping auto-register; no account will actually be created)")
    else:
        session, auto_created_account = build_authenticated_session()
        if "Authorization" not in session.headers and not session.cookies:
            print("⚠ No credentials configured — testing will run unauthenticated.")
            print("  Set one of these in a .env file if the app requires login:")
            print("    AUDIT_BEARER_TOKEN=\"eyJhbGc...\"                          (already have a token)")
            print("    AUDIT_COOKIES=\"session=...\"                              (cookie-based auth)")
            print("    AUDIT_LOGIN_URL=... + AUDIT_EMAIL=... + AUDIT_PASSWORD=... (log in automatically)")
            print("    AUDIT_REGISTER_URL=...                                    (create a fresh test account)")

    tester = ActiveTester(args.base_url, endpoints, session=session)
    result = tester.run(dry_run=args.dry_run)

    if auto_created_account and not args.dry_run:
        print()
        attempt_self_cleanup(session, args.base_url, endpoints, auto_created_account)

    print(f"\n{'='*60}")
    print(f"AuditAgent — Active Test {'(DRY RUN)' if args.dry_run else ''}")
    print(f"{'='*60}")
    print(f"Resource groups tested: {result['resource_groups_tested']}\n")
    for entry in result["report"]:
        print(f"  {entry['resource']}:")
        for step in entry["steps"]:
            status = step.get("status", step.get("error", "planned"))
            print(f"    {step['method']} {step['url']} -> {status}")
            if "note" in step:
                print(f"      ({step['note']})")

    if not args.dry_run:
        tester.ledger.save(args.output)
        print(f"\nLedger saved to {args.output}")
        print(f"To roll back: python active_tester.py --rollback {args.output} --execute-rollback")


if __name__ == "__main__":
    main()

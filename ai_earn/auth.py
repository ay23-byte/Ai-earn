"""Offline account authentication. Not a network or operating-system security boundary."""

import hashlib
import hmac
import re
import secrets
import sqlite3
import time
from typing import Callable

from .auth_storage import AuthStorage


PASSWORD_ITERATIONS = 600_000
SESSION_SECONDS = 30 * 60
WINDOW_SECONDS = 15 * 60
_LIMITS = {"register": (10, 10), "login": (5, 30), "recover": (5, 20), "replace_recovery": (5, 20)}
_USERNAME = re.compile(r"[a-z0-9_]{3,32}\Z")
_TOKEN = re.compile(r"[A-Za-z0-9_-]{43}\Z")


class AuthError(Exception):
    """Safe, non-sensitive error for the local authentication interface."""


class RateLimitError(AuthError):
    pass


class AccessDenied(AuthError):
    pass


def _username(value: str) -> str:
    if not isinstance(value, str) or not _USERNAME.fullmatch(value):
        raise AuthError("Username must be 3–32 lowercase ASCII letters, numbers or underscores.")
    return value


def _password(value: str) -> bytes:
    if not isinstance(value, str) or not 12 <= len(value) <= 256:
        raise AuthError("Password must contain 12–256 characters.")
    try:
        return value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise AuthError("Password contains invalid characters.") from exc


def _token_hash(value: str) -> bytes | None:
    if not isinstance(value, str) or not _TOKEN.fullmatch(value):
        return None
    return hashlib.sha256(value.encode("ascii")).digest()


def _derive(password: bytes, salt: bytes, iterations: int) -> bytes:
    return hashlib.pbkdf2_hmac("sha256", password, salt, iterations)


def _audit(conn: sqlite3.Connection, now: int, event: str, account_id: str | None = None) -> None:
    conn.execute("INSERT INTO auth_audit(occurred_at,event,account_id) VALUES(?,?,?)", (now, event, account_id))


def _audit_throttled(conn: sqlite3.Connection, now: int, event: str) -> None:
    # Repeated rejected requests must not grow the audit database without bound.
    previous = conn.execute(
        "SELECT 1 FROM auth_audit WHERE event=? AND occurred_at>? LIMIT 1",
        (event, now - WINDOW_SECONDS),
    ).fetchone()
    if not previous:
        _audit(conn, now, event)


def _attempt(conn: sqlite3.Connection, action: str, subject: str, now: int) -> bool:
    # Subject is a SHA-256 digest of a validated username, never a password or token.
    conn.execute("DELETE FROM auth_attempts WHERE occurred_at <= ?", (now - WINDOW_SECONDS,))
    subject_limit, global_limit = _LIMITS[action]
    total = conn.execute("SELECT count(*) FROM auth_attempts WHERE action=?", (action,)).fetchone()[0]
    per_subject = conn.execute("SELECT count(*) FROM auth_attempts WHERE action=? AND subject=?", (action, subject)).fetchone()[0]
    if total >= global_limit or per_subject >= subject_limit:
        _audit_throttled(conn, now, "rate_limited")
        return False
    conn.execute("INSERT INTO auth_attempts(action,subject,occurred_at) VALUES(?,?,?)", (action, subject, now))
    return True


def _subject(username: str) -> str:
    return hashlib.sha256(username.encode("ascii")).hexdigest()


class AuthService:
    def __init__(self, storage: AuthStorage, clock: Callable[[], float] = time.time):
        self.storage = storage
        self.clock = clock

    def register(self, username: str, password: str) -> str:
        username = _username(username)
        secret = _password(password)
        now = int(self.clock())
        recovery = secrets.token_urlsafe(32)
        result = "ok"
        with self.storage.transaction() as conn:
            if not _attempt(conn, "register", _subject(username), now):
                result = "limited"
            elif conn.execute("SELECT 1 FROM accounts WHERE username=?", (username,)).fetchone():
                _audit(conn, now, "registration_rejected")
                result = "exists"
            else:
                account_id = secrets.token_hex(16)
                salt = secrets.token_bytes(16)
                password_hash = _derive(secret, salt, PASSWORD_ITERATIONS)
                conn.execute(
                    "INSERT INTO accounts(id,username,password_salt,password_hash,password_iterations,recovery_hash,created_at) VALUES(?,?,?,?,?,?,?)",
                    (account_id, username, salt, password_hash, PASSWORD_ITERATIONS, _token_hash(recovery), now),
                )
                _audit(conn, now, "registration", account_id)
        if result == "limited":
            raise RateLimitError("Too many attempts. Try again later.")
        if result != "ok":
            raise AuthError("Unable to register this account.")
        return recovery

    def login(self, username: str, password: str) -> str:
        username = _username(username)
        # Invalid password formats are treated as incorrect credentials without exposing account existence.
        try:
            secret = _password(password)
            password_valid = True
        except AuthError:
            secret = b"invalid credential"
            password_valid = False
        now = int(self.clock())
        result = "invalid"
        token = None
        with self.storage.transaction() as conn:
            if not _attempt(conn, "login", _subject(username), now):
                result = "limited"
            else:
                account = conn.execute("SELECT * FROM accounts WHERE username=?", (username,)).fetchone()
                # Equal-cost dummy derivation when no user exists.
                salt = account["password_salt"] if account else bytes(16)
                iterations = account["password_iterations"] if account else PASSWORD_ITERATIONS
                actual = _derive(secret, salt, iterations)
                if account and password_valid and hmac.compare_digest(actual, account["password_hash"]):
                    token = secrets.token_urlsafe(32)
                    conn.execute("INSERT INTO sessions(token_hash,account_id,expires_at) VALUES(?,?,?)", (_token_hash(token), account["id"], now + SESSION_SECONDS))
                    _audit(conn, now, "login", account["id"])
                    result = "ok"
                else:
                    _audit(conn, now, "login_failed")
        if result == "limited":
            raise RateLimitError("Too many attempts. Try again later.")
        if result != "ok" or token is None:
            raise AuthError("Invalid username or password.")
        return token

    def profile(self, token: str, account_id: str | None = None) -> dict[str, str]:
        digest = _token_hash(token)
        if digest is None:
            raise AccessDenied("Authentication required.")
        now = int(self.clock())
        with self.storage.transaction() as conn:
            session = conn.execute(
                "SELECT accounts.id, accounts.username FROM sessions JOIN accounts ON accounts.id=sessions.account_id WHERE sessions.token_hash=? AND sessions.revoked_at IS NULL AND sessions.expires_at>?",
                (digest, now),
            ).fetchone()
            if not session or (account_id is not None and session["id"] != account_id):
                _audit_throttled(conn, now, "access_denied")
                denied = True
            else:
                denied = False
                result = {"id": session["id"], "username": session["username"]}
        if denied:
            raise AccessDenied("Access denied.")
        return result

    def logout(self, token: str) -> None:
        digest = _token_hash(token)
        if digest is None:
            raise AccessDenied("Authentication required.")
        now = int(self.clock())
        with self.storage.transaction() as conn:
            session = conn.execute("SELECT account_id FROM sessions WHERE token_hash=? AND revoked_at IS NULL AND expires_at>?", (digest, now)).fetchone()
            if session:
                conn.execute("UPDATE sessions SET revoked_at=? WHERE token_hash=?", (now, digest))
                _audit(conn, now, "logout", session["account_id"])
        if not session:
            raise AccessDenied("Access denied.")

    def replace_recovery_code(self, token: str, *, confirmed: bool = False) -> str:
        """Replace the signed-in account's code; the previous code stops working.

        If delivery fails, the user can log in again and repeat this operation.
        No account identifier supplied by a caller is trusted. This local API
        requires explicit confirmation from the authenticated caller.
        """
        if confirmed is not True:
            raise AuthError("Explicit confirmation is required.")
        digest = _token_hash(token)
        if digest is None:
            raise AccessDenied("Authentication required.")
        now = int(self.clock())
        result = "denied"
        replacement = None
        with self.storage.transaction() as conn:
            account = conn.execute(
                "SELECT accounts.id, accounts.username FROM sessions JOIN accounts ON accounts.id=sessions.account_id WHERE sessions.token_hash=? AND sessions.revoked_at IS NULL AND sessions.expires_at>?",
                (digest, now),
            ).fetchone()
            if not account:
                _audit_throttled(conn, now, "access_denied")
            elif not _attempt(conn, "replace_recovery", _subject(account["username"]), now):
                result = "limited"
            else:
                replacement = secrets.token_urlsafe(32)
                conn.execute("UPDATE accounts SET recovery_hash=? WHERE id=?", (_token_hash(replacement), account["id"]))
                _audit(conn, now, "recovery_code_replaced", account["id"])
                result = "ok"
        if result == "limited":
            raise RateLimitError("Too many attempts. Try again later.")
        if result != "ok" or replacement is None:
            raise AccessDenied("Access denied.")
        return replacement

    def recover(self, username: str, recovery_code: str, new_password: str) -> str:
        username = _username(username)
        secret = _password(new_password)
        digest = _token_hash(recovery_code)
        now = int(self.clock())
        result = "invalid"
        new_recovery = None
        with self.storage.transaction() as conn:
            if not _attempt(conn, "recover", _subject(username), now):
                result = "limited"
            else:
                account = conn.execute("SELECT * FROM accounts WHERE username=?", (username,)).fetchone()
                if digest is not None and account and hmac.compare_digest(digest, account["recovery_hash"]):
                    new_recovery = secrets.token_urlsafe(32)
                    salt = secrets.token_bytes(16)
                    password_hash = _derive(secret, salt, PASSWORD_ITERATIONS)
                    conn.execute("UPDATE accounts SET password_salt=?, password_hash=?, password_iterations=?, recovery_hash=? WHERE id=?", (salt, password_hash, PASSWORD_ITERATIONS, _token_hash(new_recovery), account["id"]))
                    conn.execute("UPDATE sessions SET revoked_at=? WHERE account_id=? AND revoked_at IS NULL", (now, account["id"]))
                    _audit(conn, now, "password_recovered", account["id"])
                    result = "ok"
                else:
                    _audit(conn, now, "recovery_failed")
        if result == "limited":
            raise RateLimitError("Too many attempts. Try again later.")
        if result != "ok" or new_recovery is None:
            raise AuthError("Invalid username or recovery code.")
        return new_recovery

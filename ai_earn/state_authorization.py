"""Internal session-bound unit of work for future local state services.

Only trusted application composition may choose the auth and state stores.
This module is not a public API, OS security boundary, or per-record policy.
"""
from contextlib import contextmanager
import os
from typing import Iterator
import sqlite3

from .auth import AccessDenied, AuthService, _token_hash
from .state_storage import StateStorage, StateStorageError


@contextmanager
def _authorized_transaction(
    auth: AuthService, state: StateStorage, token: str
) -> Iterator[tuple[sqlite3.Connection, str, int]]:
    """Hold auth then state locks, then validate session and derive its actor.

    Expiry is checked after both locks and again before state commit. The
    auth lock prevents normal logout or recovery from racing this operation.
    Callers must not acquire these locks in the reverse order or invoke auth
    operations while holding this context. State commits before the auth
    transaction exits. The auth transaction is read-only here; an error after
    state commit can nevertheless make the outcome ambiguous. Future mutations
    must implement durable idempotency.
    """
    digest = _token_hash(token)
    if digest is None:
        raise AccessDenied("Authentication required.")
    try:
        if os.path.samefile(auth.storage.path, state.path):
            raise StateStorageError("Authentication and state must use different files.")
    except OSError as exc:
        raise StateStorageError("Authentication or state database is unavailable.") from exc
    # Keep this order everywhere: auth before state, to avoid lock inversion.
    with auth.storage.transaction() as auth_conn:
        with state.transaction() as state_conn:
            if state_conn.execute("PRAGMA user_version").fetchone()[0] not in (2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12):
                raise StateStorageError("State schema version 2, 3, 4, 5, 6, 7, 8, 9, 10, 11 or 12 is required.")
            now = int(auth.clock())
            session = auth_conn.execute(
                "SELECT accounts.id, sessions.expires_at FROM sessions JOIN accounts ON accounts.id=sessions.account_id "
                "WHERE sessions.token_hash=? AND sessions.revoked_at IS NULL AND sessions.expires_at>?",
                (digest, now),
            ).fetchone()
            if session is None:
                raise AccessDenied("Access denied.")
            yield state_conn, session["id"], now
            # Long-running operations cannot commit after the session expires.
            if session["expires_at"] <= int(auth.clock()):
                raise AccessDenied("Access denied.")

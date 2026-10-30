"""Internal operation-history comparison for disposable state copies.

This is not reconciliation, tamper evidence, a public API or an OS boundary.
Trusted code must select a fixed auth database and two trusted state files.
"""
import hmac
import os

from .auth import AccessDenied, AuthService, _token_hash
from .state_authorization import _authorized_transaction
from .state_storage import StateStorage, StateStorageError, _check_integrity


def _ordered_operations(conn, owner):
    # The schema's owner/key primary key supports ordered streaming; do not
    # materialize all operation records or expose keys or hashes to the caller.
    return iter(conn.execute(
        "SELECT operation_key, request_hash, resource_type, resource_id FROM state_operations "
        "WHERE owner_id=? ORDER BY operation_key COLLATE BINARY", (owner,),
    ))


def _compare_operations(current, candidate, owner):
    left, right = _ordered_operations(current, owner), _ordered_operations(candidate, owner)
    a, b = next(left, None), next(right, None)
    result = {"matching": 0, "current_only": 0, "candidate_only": 0, "conflicts": 0}
    while a is not None or b is not None:
        if b is None or (a is not None and a["operation_key"] < b["operation_key"]):
            result["current_only"] += 1
            a = next(left, None)
        elif a is None or b["operation_key"] < a["operation_key"]:
            result["candidate_only"] += 1
            b = next(right, None)
        else:
            same = (hmac.compare_digest(a["request_hash"], b["request_hash"])
                    and a["resource_type"] == b["resource_type"]
                    and a["resource_id"] == b["resource_id"])
            result["matching" if same else "conflicts"] += 1
            a, b = next(left, None), next(right, None)
    return result


class StateRecoveryComparison:
    """Compare a session owner's operation keys across two trusted copies.

    No state is merged or repaired. Matching counts don't establish that
    records, audit contents, history or external effects are equal or valid.
    This holds write-intent SQLite locks and may block other local operations.
    """

    def __init__(self, auth: AuthService, current: StateStorage, candidate: StateStorage):
        self._auth = auth
        self._current = current
        self._candidate = candidate

    def compare(self, token: str) -> dict[str, int]:
        if _token_hash(token) is None:
            raise AccessDenied("Authentication required.")
        try:
            if (os.path.samefile(self._current.path, self._candidate.path)
                    or os.path.samefile(self._auth.storage.path, self._current.path)
                    or os.path.samefile(self._auth.storage.path, self._candidate.path)):
                raise StateStorageError("Authentication and state copies must be distinct files.")
            # Every comparison uses the same state-file order after the auth lock.
            current_path = os.path.normcase(str(self._current.path.resolve(strict=True)))
            candidate_path = os.path.normcase(str(self._candidate.path.resolve(strict=True)))
        except OSError as exc:
            raise StateStorageError("A required database is unavailable.") from exc
        if current_path == candidate_path:
            raise StateStorageError("Authentication and state copies must be distinct files.")
        current_first = current_path < candidate_path
        first, second = ((self._current, self._candidate) if current_first
                         else (self._candidate, self._current))
        # _authorized_transaction holds auth first, validates the session, and
        # rechecks expiry after both state transactions have finished.
        with _authorized_transaction(self._auth, first, token) as (first_conn, owner, _now):
            with second.transaction() as second_conn:
                current, candidate = ((first_conn, second_conn) if current_first
                                      else (second_conn, first_conn))
                version = current.execute("PRAGMA user_version").fetchone()[0]
                if version < 2 or version != candidate.execute("PRAGMA user_version").fetchone()[0]:
                    raise StateStorageError("State copies must have the same supported schema version.")
                # Validate both full copies, not just the requesting owner's rows.
                _check_integrity(current)
                _check_integrity(candidate)
                return {"schema_version": version, **_compare_operations(current, candidate, owner)}

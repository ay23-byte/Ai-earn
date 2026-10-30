"""Internal owner-scoped audit reading for disposable local state.

The audit is not tamper-evident or a complete security log. Trusted code must
use a fixed auth/state pair; this is not a public API or an OS boundary.
"""
from .auth import AuthService
from .state_authorization import _authorized_transaction
from .state_storage import StateStorage


class AuditQueryError(Exception):
    """Invalid audit pagination input."""


class StateAudit:
    """Read only the authenticated actor's state events, in ascending ID order."""

    def __init__(self, auth: AuthService, state: StateStorage):
        self._auth = auth
        self._state = state

    def list_events(self, token: str, *, after_id: int = 0, limit: int = 50) -> list[dict[str, str | int]]:
        if type(after_id) is not int or not 0 <= after_id <= 9_223_372_036_854_775_807:
            raise AuditQueryError("Invalid audit cursor.")
        if type(limit) is not int or not 1 <= limit <= 100:
            raise AuditQueryError("Invalid audit page size.")
        with _authorized_transaction(self._auth, self._state, token) as (conn, owner, _now):
            rows = conn.execute(
                "SELECT id, occurred_at, event, resource_type, resource_id FROM state_audit "
                "WHERE actor_id=? AND id>? ORDER BY id LIMIT ?", (owner, after_id, limit),
            ).fetchall()
            return [dict(row) for row in rows]

    def list_owner_events(
        self, token: str, *, after_sequence: int = 0, limit: int = 50
    ) -> list[dict[str, str | int]]:
        """Return an owner-relative page without global event IDs.

        The sequence is a 1-based position in this owner's current event list,
        not a durable identifier. Deletion or restoring an older snapshot can
        shift positions; it must not be carried across such changes. This
        internal method is not a public API or a timing-side-channel defense.
        """
        if type(after_sequence) is not int or not 0 <= after_sequence <= 9_223_372_036_854_775_807:
            raise AuditQueryError("Invalid audit cursor.")
        if type(limit) is not int or not 1 <= limit <= 100:
            raise AuditQueryError("Invalid audit page size.")
        with _authorized_transaction(self._auth, self._state, token) as (conn, owner, _now):
            rows = conn.execute(
                "SELECT occurred_at, event, resource_type, resource_id FROM state_audit "
                "WHERE actor_id=? ORDER BY id LIMIT ? OFFSET ?", (owner, limit, after_sequence),
            ).fetchall()
            return [dict(sequence=after_sequence + index, **dict(row))
                    for index, row in enumerate(rows, start=1)]

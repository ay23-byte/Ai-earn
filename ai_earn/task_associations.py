"""Owned queue/workflow associations, not execution or authority.

Associations are immutable local metadata. The queue and workflow can still
change independently; a link does not prove work, review, delivery or payment.
This internal service requires a trusted fixed auth/state pair and disposable data.
"""
import hmac
import secrets

from .auth import AccessDenied, AuthService
from .opportunity_drafts import DraftError, _digest, _key, _record_id
from .state_authorization import _authorized_transaction
from .state_storage import StateStorage, StateStorageError
from .task_queue import _owned as _owned_queue
from .task_workflows import _owned as _owned_workflow


_RESOURCE = "task_association"


class AssociationError(Exception):
    """Invalid input, conflicting operation key, or disallowed association."""


def _input(validator, value):
    try:
        return validator(value)
    except DraftError as exc:
        raise AssociationError("Invalid association input.") from exc


def _version(conn):
    if conn.execute("PRAGMA user_version").fetchone()[0] != 10:
        raise StateStorageError("State schema version 10 is required for associations.")


def _owned_rows(conn, owner, ident):
    row = conn.execute("SELECT id, queue_id, workflow_id, created_at FROM task_associations WHERE id=? AND owner_id=?",
                       (ident, owner)).fetchone()
    if row is None:
        raise AccessDenied("Access denied.")
    try:
        queue = _owned_queue(conn, owner, row["queue_id"])
        workflow = _owned_workflow(conn, owner, row["workflow_id"])
    except AccessDenied as exc:
        raise StateStorageError("Association parent is unavailable.") from exc
    return row, queue, workflow


def _owned(conn, owner, ident):
    return _owned_rows(conn, owner, ident)[0]


def _retry(conn, owner, key, digest):
    row = conn.execute("SELECT request_hash, resource_type, resource_id FROM state_operations WHERE owner_id=? AND operation_key=?", (owner, key)).fetchone()
    if row is None:
        return None
    if row["resource_type"] != _RESOURCE or not hmac.compare_digest(row["request_hash"], digest):
        raise AssociationError("Operation key was already used for a different request.")
    try:
        _owned(conn, owner, row["resource_id"])
    except AccessDenied as exc:
        raise StateStorageError("Stored operation result is unavailable.") from exc
    return row["resource_id"]


def _audit(conn, now, owner, ident):
    conn.execute("INSERT INTO state_audit(occurred_at, actor_id, event, resource_type, resource_id) VALUES(?,?,?,?,?)",
                 (now, owner, "task_association_created", _RESOURCE, ident))


def _remember(conn, owner, key, digest, ident):
    conn.execute("INSERT INTO state_operations(owner_id, operation_key, request_hash, resource_type, resource_id) VALUES(?,?,?,?,?)",
                 (owner, key, digest, _RESOURCE, ident))


class TaskAssociations:
    """One-to-one historical references, with no worker or lifecycle coordination."""

    def __init__(self, auth: AuthService, state: StateStorage):
        self._auth = auth
        self._state = state

    def create(self, token: str, queue_id: str, workflow_id: str, *, operation_key: str) -> str:
        queue_id = _input(_record_id, queue_id)
        workflow_id = _input(_record_id, workflow_id)
        key = _input(_key, operation_key)
        digest = _digest("task_association_create", queue_id + workflow_id)
        with _authorized_transaction(self._auth, self._state, token) as (conn, owner, now):
            _version(conn)
            existing = _retry(conn, owner, key, digest)
            if existing is not None:
                return existing
            queue = _owned_queue(conn, owner, queue_id)
            workflow = _owned_workflow(conn, owner, workflow_id)
            if queue["status"] != "queued" or workflow["status"] != "planned":
                raise AssociationError("A queued item and planned workflow are required.")
            if conn.execute("SELECT 1 FROM task_associations WHERE owner_id=? AND (queue_id=? OR workflow_id=?)",
                            (owner, queue_id, workflow_id)).fetchone() is not None:
                raise AssociationError("A record is already associated.")
            ident = secrets.token_hex(16)
            conn.execute("INSERT INTO task_associations VALUES (?,?,?,?,?)", (ident, owner, queue_id, workflow_id, now))
            _audit(conn, now, owner, ident)
            _remember(conn, owner, key, digest, ident)
            return ident

    def get(self, token: str, record_id: str) -> dict:
        ident = _input(_record_id, record_id)
        with _authorized_transaction(self._auth, self._state, token) as (conn, owner, _now):
            _version(conn)
            return dict(_owned(conn, owner, ident))

    def inspect(self, token: str, record_id: str) -> dict:
        """Read both linked states at one captured time; never authorize work.

        This is a point-in-time observation using a wall clock, not a lease
        claim, synchronization, proof of review, or external-effect fence.
        Conditions can change as soon as the transaction exits.
        """
        ident = _input(_record_id, record_id)
        with _authorized_transaction(self._auth, self._state, token) as (conn, owner, now):
            _version(conn)
            association, queue, workflow = _owned_rows(conn, owner, ident)
            deadline_passed = bool(queue["deadline_at"] and queue["deadline_at"] <= now)
            ready = queue["status"] == "queued" and queue["available_at"] <= now and not deadline_passed
            active = queue["status"] == "leased" and now < queue["lease_until"]
            return {
                "association_id": association["id"],
                "queue_id": association["queue_id"],
                "workflow_id": association["workflow_id"],
                "observed_at": now,
                "queue_status": queue["status"],
                "workflow_status": workflow["status"],
                "queue_ready_at_observation": ready,
                "lease_active_at_observation": active,
                "deadline_passed_at_observation": deadline_passed,
            }

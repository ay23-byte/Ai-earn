"""Experimental offline scheduling queue; it does not execute or complete work.

Use only disposable data and a trusted, fixed auth/state pair. Lease IDs fence
stale clients but are not credentials; this is not an OS or public API boundary.
"""
import hmac
import json
import secrets
import sqlite3

from .auth import AccessDenied, AuthService
from .opportunity_drafts import DraftError, _digest, _key, _record_id, _title
from .state_authorization import _authorized_transaction
from .state_storage import StateStorage, StateStorageError, _check_queue_integrity, _has_paired_completion, _is_managed_pair


_MAX_TIME = 4_102_444_800  # 2100-01-01 UTC; avoid unbounded timestamp arithmetic.


class QueueError(Exception):
    """Invalid queue input, conflicting key or disallowed transition."""


def _input(validator, value):
    try:
        return validator(value)
    except DraftError as exc:
        raise QueueError("Invalid queue input.") from exc


def _integer(value, minimum, maximum):
    if type(value) is not int or not minimum <= value <= maximum:
        raise QueueError("Invalid queue input.")
    return value


def _version(conn):
    if conn.execute("PRAGMA user_version").fetchone()[0] not in (7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20, 21, 22, 23, 24, 25, 26, 27, 28):
        raise StateStorageError("State schema version 7, 8, 9, 10, 11, 12, 13, 14 or 15 is required for the queue.")


def _owned(conn, owner, record_id):
    row = conn.execute("SELECT * FROM task_queue WHERE id=? AND owner_id=?", (record_id, owner)).fetchone()
    if row is None:
        raise AccessDenied("Access denied.")
    _check_queue_integrity(conn, owner=owner, record_id=record_id)
    # Validate a v11 completion marker even for mutations and historical retries.
    _has_paired_completion(conn, owner, record_id)
    _is_managed_pair(conn, owner, queue_id=record_id)
    return row


def _retry(conn, owner, key, digest, kind):
    row = conn.execute("SELECT request_hash, resource_type, resource_id FROM state_operations WHERE owner_id=? AND operation_key=?", (owner, key)).fetchone()
    if row is None:
        return None
    if row["resource_type"] != kind or not hmac.compare_digest(row["request_hash"], digest):
        raise QueueError("Operation key was already used for a different request.")
    if kind == "queue_lease":
        result = conn.execute("SELECT queue_id FROM queue_leases WHERE id=? AND owner_id=?", (row["resource_id"], owner)).fetchone()
        if result is None:
            raise StateStorageError("Stored operation result is unavailable.")
        queue_id = result["queue_id"]
    else:
        queue_id = row["resource_id"]
    try:
        _owned(conn, owner, queue_id)
    except AccessDenied as exc:
        raise StateStorageError("Stored operation result is unavailable.") from exc
    return row["resource_id"]


def _audit(conn, now, owner, event, record_id):
    conn.execute("INSERT INTO state_audit(occurred_at, actor_id, event, resource_type, resource_id) VALUES(?,?,?,?,?)",
                 (now, owner, event, "task_queue", record_id))


def _remember(conn, owner, key, digest, kind, record_id):
    conn.execute("INSERT INTO state_operations(owner_id, operation_key, request_hash, resource_type, resource_id) VALUES(?,?,?,?,?)",
                 (owner, key, digest, kind, record_id))


def _request(action, values):
    return _digest(action, json.dumps(values, ensure_ascii=True, separators=(",", ":")))


def _claim_in_transaction(conn, owner, now, task, duration, key, digest):
    """Apply queue claim rules inside an already authorized state transaction.

    The caller must perform retry handling and recheck lease time before commit.
    """
    if task["status"] != "queued" or task["available_at"] > now or (task["deadline_at"] and task["deadline_at"] <= now):
        raise QueueError("Task is not ready for a lease.")
    if now + duration > _MAX_TIME:
        raise QueueError("Invalid queue timing.")
    until = min(now + duration, task["deadline_at"]) if task["deadline_at"] else now + duration
    ident = task["id"]
    lease_id = secrets.token_hex(16)
    conn.execute("INSERT INTO queue_leases VALUES(?,?,?,?,?)", (lease_id, owner, ident, task["attempts"] + 1, until))
    conn.execute("UPDATE task_queue SET status='leased', attempts=attempts+1, current_lease_id=?, lease_until=?, updated_at=? WHERE id=? AND owner_id=?",
                 (lease_id, until, now, ident, owner))
    _audit(conn, now, owner, "queue_leased", ident)
    _remember(conn, owner, key, digest, "queue_lease", lease_id)
    return lease_id, until


def _reschedule_in_transaction(conn, owner, now, task, lease_id, key, digest, *, expired):
    """Apply queue retry/recovery rules in an authorized state transaction.

    Callers handle exact retries and recheck the lease time before commit.
    """
    if _has_paired_completion(conn, owner, task["id"]):
        raise QueueError("Completed queue items cannot be rescheduled.")
    if task["status"] != "leased" or task["current_lease_id"] != lease_id or (now >= task["lease_until"]) != expired:
        raise QueueError("Lease is stale or transition is not allowed.")
    available = now + task["backoff_seconds"]
    if available > _MAX_TIME:
        raise QueueError("Invalid queue timing.")
    terminal = task["attempts"] >= task["max_attempts"] or (task["deadline_at"] and available >= task["deadline_at"])
    status = "exhausted" if terminal else "queued"
    ident = task["id"]
    conn.execute("UPDATE task_queue SET status=?, available_at=?, current_lease_id=NULL, lease_until=NULL, updated_at=? WHERE id=? AND owner_id=?",
                 (status, available, now, ident, owner))
    _audit(conn, now, owner, "queue_exhausted" if terminal else ("queue_recovered" if expired else "queue_retry"), ident)
    _remember(conn, owner, key, digest, "task_queue", ident)
    return ident


class TaskQueue:
    """Owner-scoped records and leases only, with no execution or completion."""

    def __init__(self, auth: AuthService, state: StateStorage):
        self._auth = auth
        self._state = state

    def enqueue(self, token: str, title: str, *, operation_key: str, priority: int = 0,
                max_attempts: int = 3, backoff_seconds: int = 60,
                available_at: int = 0, deadline_at: int = 0) -> str:
        title = _input(_title, title)
        key = _input(_key, operation_key)
        priority = _integer(priority, 0, 9)
        max_attempts = _integer(max_attempts, 1, 10)
        backoff_seconds = _integer(backoff_seconds, 0, 86400)
        available_at = _integer(available_at, 0, _MAX_TIME)
        deadline_at = _integer(deadline_at, 0, _MAX_TIME)
        digest = _request("queue_enqueue", [title, priority, max_attempts, backoff_seconds, available_at, deadline_at])
        with _authorized_transaction(self._auth, self._state, token) as (conn, owner, now):
            _version(conn)
            existing = _retry(conn, owner, key, digest, "task_queue")
            if existing is not None:
                return existing
            if not 0 <= now <= _MAX_TIME or (deadline_at and (deadline_at <= now or deadline_at <= available_at)):
                raise QueueError("Invalid queue timing.")
            ident = secrets.token_hex(16)
            conn.execute("INSERT INTO task_queue VALUES (?,?,?,'queued',?,?,0,?,?,?,NULL,NULL,?,?)",
                         (ident, owner, title, priority, max_attempts, backoff_seconds,
                          max(now, available_at), deadline_at, now, now))
            _audit(conn, now, owner, "queue_enqueued", ident)
            _remember(conn, owner, key, digest, "task_queue", ident)
            return ident

    def get(self, token: str, record_id: str) -> dict:
        ident = _input(_record_id, record_id)
        with _authorized_transaction(self._auth, self._state, token) as (conn, owner, _now):
            _version(conn)
            row = dict(_owned(conn, owner, ident))
            if _has_paired_completion(conn, owner, ident):
                completed = conn.execute("SELECT created_at FROM paired_completions WHERE owner_id=? AND queue_id=?", (owner, ident)).fetchone()
                row["status"] = "completed"
                row["current_lease_id"] = None
                row["lease_until"] = None
                row["completed_at"] = completed["created_at"]
            del row["owner_id"]
            return row

    def ready(self, token: str, *, limit: int = 20) -> list[dict]:
        limit = _integer(limit, 1, 100)
        with _authorized_transaction(self._auth, self._state, token) as (conn, owner, now):
            _version(conn)
            managed_filter = ("AND NOT EXISTS (SELECT 1 FROM task_associations a JOIN managed_task_pairs m "
                              "ON m.id=a.id AND m.owner_id=a.owner_id "
                              "WHERE a.queue_id=task_queue.id AND a.owner_id=task_queue.owner_id) "
                              if conn.execute("PRAGMA user_version").fetchone()[0] >= 12 else "")
            rows = conn.execute("SELECT id, title, priority, available_at, deadline_at FROM task_queue "
                                "WHERE owner_id=? AND status='queued' AND available_at<=? "
                                "AND (deadline_at=0 OR deadline_at>?) " + managed_filter +
                                "ORDER BY priority DESC, available_at, id LIMIT ?",
                                (owner, now, now, limit)).fetchall()
            for row in rows:
                _check_queue_integrity(conn, owner=owner, record_id=row["id"])
                _has_paired_completion(conn, owner, row["id"])
                _is_managed_pair(conn, owner, queue_id=row["id"])
            return [dict(row) for row in rows]

    def claim(self, token: str, record_id: str, *, operation_key: str, lease_seconds: int) -> str:
        ident = _input(_record_id, record_id)
        key = _input(_key, operation_key)
        duration = _integer(lease_seconds, 1, 3600)
        digest = _request("queue_claim", [ident, duration])
        with _authorized_transaction(self._auth, self._state, token) as (conn, owner, now):
            _version(conn)
            task = _owned(conn, owner, ident)
            existing = _retry(conn, owner, key, digest, "queue_lease")
            if existing is not None:
                return existing
            if _is_managed_pair(conn, owner, queue_id=ident):
                raise QueueError("Managed queue items require paired operations.")
            lease_id, until = _claim_in_transaction(conn, owner, now, task, duration, key, digest)
            if int(self._auth.clock()) >= until:
                raise QueueError("Lease expired before commit.")
            return lease_id

    def get_lease(self, token: str, lease_id: str) -> dict:
        lease_id = _input(_record_id, lease_id)
        with _authorized_transaction(self._auth, self._state, token) as (conn, owner, now):
            _version(conn)
            row = conn.execute("SELECT id, queue_id, attempt, lease_until FROM queue_leases WHERE id=? AND owner_id=?", (lease_id, owner)).fetchone()
            if row is None:
                raise AccessDenied("Access denied.")
            task = _owned(conn, owner, row["queue_id"])
            result = dict(row)
            completed = _has_paired_completion(conn, owner, task["id"])
            result["active"] = (task["status"] == "leased" and task["current_lease_id"] == lease_id
                                and now < row["lease_until"] and not completed)
            return result

    def retry(self, token: str, record_id: str, lease_id: str, *, operation_key: str) -> str:
        """Release a current unexpired lease after a failed attempt."""
        return self._reschedule(token, record_id, lease_id, operation_key, expired=False)

    def recover(self, token: str, record_id: str, lease_id: str, *, operation_key: str) -> str:
        """Explicitly recover a lease after its timeout; never run work."""
        return self._reschedule(token, record_id, lease_id, operation_key, expired=True)

    def _reschedule(self, token, record_id, lease_id, operation_key, *, expired):
        ident, lease_id = _input(_record_id, record_id), _input(_record_id, lease_id)
        key = _input(_key, operation_key)
        digest = _request("queue_recover" if expired else "queue_retry", [ident, lease_id])
        with _authorized_transaction(self._auth, self._state, token) as (conn, owner, now):
            _version(conn)
            task = _owned(conn, owner, ident)
            existing = _retry(conn, owner, key, digest, "task_queue")
            if existing is not None:
                return existing
            if _is_managed_pair(conn, owner, queue_id=ident):
                raise QueueError("Managed queue items require paired operations.")
            result = _reschedule_in_transaction(conn, owner, now, task, lease_id, key, digest, expired=expired)
            if (int(self._auth.clock()) >= task["lease_until"]) != expired:
                raise QueueError("Lease timing changed before commit.")
            return result

    def cancel(self, token: str, record_id: str, *, operation_key: str) -> str:
        ident = _input(_record_id, record_id)
        key = _input(_key, operation_key)
        digest = _request("queue_cancel", [ident])
        with _authorized_transaction(self._auth, self._state, token) as (conn, owner, now):
            _version(conn)
            task = _owned(conn, owner, ident)
            existing = _retry(conn, owner, key, digest, "task_queue")
            if existing is not None:
                return existing
            if _is_managed_pair(conn, owner, queue_id=ident):
                raise QueueError("Managed queue items require paired operations.")
            if task["status"] != "queued":
                raise QueueError("Transition is not allowed.")
            conn.execute("UPDATE task_queue SET status='cancelled', updated_at=? WHERE id=? AND owner_id=?", (now, ident, owner))
            _audit(conn, now, owner, "queue_cancelled", ident)
            _remember(conn, owner, key, digest, "task_queue", ident)
            return ident

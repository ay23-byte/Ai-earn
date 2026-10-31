"""Experimental local task states and output review for disposable data.

This service does not execute tasks or establish human identity, business
correctness, external delivery, or production readiness. The trusted caller
must use a fixed auth/state pair. Output digests can expose guessable content.
"""
import hashlib
import hmac
import json
import secrets
import unicodedata

from .auth import AccessDenied, AuthService
from .opportunity_drafts import DraftError, _digest, _key, _record_id, _title
from .state_authorization import _authorized_transaction
from .state_storage import StateStorage, StateStorageError, _has_paired_completion, _is_managed_pair


_RESOURCE = "task_workflow"


class WorkflowError(Exception):
    """Invalid input, conflicting operation key, or disallowed transition."""


def _input(validator, value):
    try:
        return validator(value)
    except DraftError as exc:
        raise WorkflowError("Invalid task workflow input.") from exc


def _output(value):
    if not isinstance(value, str) or not 1 <= len(value) <= 4096 or not value.strip():
        raise WorkflowError("Invalid output.")
    try:
        data = value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise WorkflowError("Invalid output.") from exc
    if len(data) > 4096 or any(unicodedata.category(ch) == "Cc" and ch not in "\n\t" for ch in value):
        raise WorkflowError("Invalid output.")
    return hashlib.sha256(data).digest(), len(data)


def _request(action, values):
    return _digest(action, json.dumps(values, ensure_ascii=True, separators=(",", ":")))


def _version(conn):
    if conn.execute("PRAGMA user_version").fetchone()[0] not in (8, 9, 10, 11, 12, 13):
        raise StateStorageError("State schema version 8, 9, 10, 11, 12 or 13 is required for task workflows.")


def _owned(conn, owner, ident):
    row = conn.execute("SELECT * FROM task_workflows WHERE id=? AND owner_id=?", (ident, owner)).fetchone()
    if row is None:
        raise AccessDenied("Access denied.")
    if conn.execute("PRAGMA user_version").fetchone()[0] >= 11:
        association = conn.execute("SELECT queue_id FROM task_associations WHERE owner_id=? AND workflow_id=?", (owner, ident)).fetchone()
        if association is not None:
            _has_paired_completion(conn, owner, association["queue_id"])
    _is_managed_pair(conn, owner, workflow_id=ident)
    return row


def _retry(conn, owner, key, digest):
    row = conn.execute("SELECT request_hash, resource_type, resource_id FROM state_operations WHERE owner_id=? AND operation_key=?", (owner, key)).fetchone()
    if row is None:
        return None
    if row["resource_type"] != _RESOURCE or not hmac.compare_digest(row["request_hash"], digest):
        raise WorkflowError("Operation key was already used for a different request.")
    if conn.execute("SELECT 1 FROM task_workflows WHERE id=? AND owner_id=?", (row["resource_id"], owner)).fetchone() is None:
        raise StateStorageError("Stored operation result is unavailable.")
    return row["resource_id"]


def _audit(conn, now, owner, event, ident):
    conn.execute("INSERT INTO state_audit(occurred_at, actor_id, event, resource_type, resource_id) VALUES(?,?,?,?,?)",
                 (now, owner, event, _RESOURCE, ident))


def _remember(conn, owner, key, digest, ident):
    conn.execute("INSERT INTO state_operations(owner_id, operation_key, request_hash, resource_type, resource_id) VALUES(?,?,?,?,?)",
                 (owner, key, digest, _RESOURCE, ident))


def _submit_in_transaction(conn, owner, now, row, output_hash, size, key, digest):
    """Apply the ordinary workflow submission in an authorized transaction."""
    if row["status"] != "in_progress":
        raise WorkflowError("Transition is not allowed.")
    ident = row["id"]
    conn.execute("UPDATE task_workflows SET status='pending_review', output_hash=?, output_size=?, updated_at=? WHERE id=? AND owner_id=?",
                 (output_hash, size, now, ident, owner))
    _audit(conn, now, owner, "workflow_submitted", ident)
    _remember(conn, owner, key, digest, ident)
    return ident


def _review_in_transaction(conn, owner, now, row, output_hash, size, approved, key, digest):
    """Apply a local review transition in an authorized transaction.

    Caller must validate the decision/confirmation and check any lease policy.
    """
    if row["status"] != "pending_review" or row["output_size"] != size or not hmac.compare_digest(row["output_hash"], output_hash):
        raise WorkflowError("Review does not match the submitted output.")
    ident = row["id"]
    status = "completed" if approved else "in_progress"
    stored_hash, stored_size = (output_hash, size) if approved else (None, None)
    conn.execute("UPDATE task_workflows SET status=?, output_hash=?, output_size=?, updated_at=? WHERE id=? AND owner_id=?",
                 (status, stored_hash, stored_size, now, ident, owner))
    _audit(conn, now, owner, "workflow_approved" if approved else "workflow_rejected", ident)
    _remember(conn, owner, key, digest, ident)
    return ident


class TaskWorkflows:
    """Local reviewed task state, independent of the scheduling queue."""

    def __init__(self, auth: AuthService, state: StateStorage):
        self._auth = auth
        self._state = state

    def create(self, token: str, title: str, *, operation_key: str) -> str:
        title, key = _input(_title, title), _input(_key, operation_key)
        digest = _request("workflow_create", [title])
        with _authorized_transaction(self._auth, self._state, token) as (conn, owner, now):
            _version(conn)
            existing = _retry(conn, owner, key, digest)
            if existing is not None:
                return existing
            ident = secrets.token_hex(16)
            conn.execute("INSERT INTO task_workflows VALUES (?,?,?,'planned',NULL,NULL,?,?)", (ident, owner, title, now, now))
            _audit(conn, now, owner, "workflow_created", ident)
            _remember(conn, owner, key, digest, ident)
            return ident

    def get(self, token: str, record_id: str) -> dict:
        ident = _input(_record_id, record_id)
        with _authorized_transaction(self._auth, self._state, token) as (conn, owner, _now):
            _version(conn)
            row = dict(_owned(conn, owner, ident))
            del row["owner_id"]
            if row["output_hash"] is not None:
                row["output_hash"] = row["output_hash"].hex()
            return row

    def start(self, token: str, record_id: str, *, operation_key: str) -> str:
        return self._simple(token, record_id, operation_key, "start")

    def cancel(self, token: str, record_id: str, *, operation_key: str) -> str:
        return self._simple(token, record_id, operation_key, "cancel")

    def _simple(self, token, record_id, operation_key, action):
        ident, key = _input(_record_id, record_id), _input(_key, operation_key)
        digest = _request("workflow_" + action, [ident])
        with _authorized_transaction(self._auth, self._state, token) as (conn, owner, now):
            _version(conn)
            row = _owned(conn, owner, ident)
            existing = _retry(conn, owner, key, digest)
            if existing is not None:
                return existing
            if _is_managed_pair(conn, owner, workflow_id=ident):
                raise WorkflowError("Managed workflows require paired operations.")
            if action == "start" and row["status"] == "planned":
                status, event = "in_progress", "workflow_started"
            elif action == "cancel" and row["status"] in ("planned", "in_progress", "pending_review"):
                status, event = "cancelled", "workflow_cancelled"
            else:
                raise WorkflowError("Transition is not allowed.")
            conn.execute("UPDATE task_workflows SET status=?, output_hash=NULL, output_size=NULL, updated_at=? WHERE id=? AND owner_id=?",
                         (status, now, ident, owner))
            _audit(conn, now, owner, event, ident)
            _remember(conn, owner, key, digest, ident)
            return ident

    def submit(self, token: str, record_id: str, output: str, *, operation_key: str) -> str:
        ident, key = _input(_record_id, record_id), _input(_key, operation_key)
        output_hash, size = _output(output)
        digest = _request("workflow_submit", [ident, output_hash.hex(), size])
        with _authorized_transaction(self._auth, self._state, token) as (conn, owner, now):
            _version(conn)
            row = _owned(conn, owner, ident)
            existing = _retry(conn, owner, key, digest)
            if existing is not None:
                return existing
            if _is_managed_pair(conn, owner, workflow_id=ident):
                raise WorkflowError("Managed workflows require paired operations.")
            return _submit_in_transaction(conn, owner, now, row, output_hash, size, key, digest)

    def review(self, token: str, record_id: str, output: str, *, approved: bool,
               confirmed: bool = False, operation_key: str) -> str:
        if confirmed is not True or type(approved) is not bool:
            raise WorkflowError("Explicit review confirmation and a boolean decision are required.")
        ident, key = _input(_record_id, record_id), _input(_key, operation_key)
        output_hash, size = _output(output)
        digest = _request("workflow_review", [ident, output_hash.hex(), size, approved])
        with _authorized_transaction(self._auth, self._state, token) as (conn, owner, now):
            _version(conn)
            row = _owned(conn, owner, ident)
            existing = _retry(conn, owner, key, digest)
            if existing is not None:
                return existing
            if _is_managed_pair(conn, owner, workflow_id=ident):
                raise WorkflowError("Managed workflows require paired operations.")
            return _review_in_transaction(conn, owner, now, row, output_hash, size, approved, key, digest)

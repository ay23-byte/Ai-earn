"""Owned queue/workflow associations, not execution or authority.

Associations are immutable local metadata. Opt-in paired operations use a
single state transaction, but the parents can still change independently; a
link does not prove work, human review, delivery or payment.
This internal service requires a trusted fixed auth/state pair and disposable data.
"""
import hmac
import secrets

from .auth import AccessDenied, AuthService
from .opportunity_drafts import DraftError, _digest, _key, _record_id
from .state_authorization import _authorized_transaction
from .state_storage import StateStorage, StateStorageError, _has_paired_completion
from .task_queue import (
    QueueError, _claim_in_transaction, _reschedule_in_transaction, _integer as _queue_integer,
    _owned as _owned_queue, _audit as _queue_audit,
    _remember as _queue_remember, _request as _queue_request, _retry as _queue_retry,
)
from .task_workflows import (
    WorkflowError, _owned as _owned_workflow, _audit as _workflow_audit,
    _remember as _workflow_remember, _request as _workflow_request,
    _retry as _workflow_retry, _output as _workflow_output,
    _submit_in_transaction as _workflow_submit_in_transaction,
    _review_in_transaction as _workflow_review_in_transaction,
)


_RESOURCE = "task_association"


class AssociationError(Exception):
    """Invalid input, conflicting operation key, or disallowed association."""


def _input(validator, value):
    try:
        return validator(value)
    except DraftError as exc:
        raise AssociationError("Invalid association input.") from exc


def _version(conn):
    if conn.execute("PRAGMA user_version").fetchone()[0] not in (10, 11):
        raise StateStorageError("State schema version 10 or 11 is required for associations.")


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


def _audit(conn, now, owner, ident, event="task_association_created"):
    conn.execute("INSERT INTO state_audit(occurred_at, actor_id, event, resource_type, resource_id) VALUES(?,?,?,?,?)",
                 (now, owner, event, _RESOURCE, ident))


def _remember(conn, owner, key, digest, ident):
    conn.execute("INSERT INTO state_operations(owner_id, operation_key, request_hash, resource_type, resource_id) VALUES(?,?,?,?,?)",
                 (owner, key, digest, _RESOURCE, ident))


def _completion_audit(conn, now, owner, ident):
    conn.execute("INSERT INTO state_audit(occurred_at,actor_id,event,resource_type,resource_id) VALUES (?,?,?,?,?)",
                 (now, owner, "paired_completion_created", "paired_completion", ident))


def _pair_child_key():
    # These are internal idempotency records, not credentials. Check the shared
    # owner keyspace before use: caller-provided keys are not reserved.
    return "internal_pair_" + secrets.token_hex(32)


class TaskAssociations:
    """Historical references and opt-in paired mutations; no worker fence."""

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

    def start_pair(self, token: str, record_id: str, *, operation_key: str, lease_seconds: int) -> str:
        """Atomically lease a queued item and start its planned local workflow.

        A returned lease ID may be stale, including on an exact retry. It does
        not authorize or fence execution or external effects; parent methods
        remain independently callable and can bypass this opt-in operation.
        """
        ident = _input(_record_id, record_id)
        key = _input(_key, operation_key)
        try:
            duration = _queue_integer(lease_seconds, 1, 3600)
        except QueueError as exc:
            raise AssociationError("Invalid association input.") from exc
        digest = _queue_request("association_start_pair", [ident, duration])
        with _authorized_transaction(self._auth, self._state, token) as (conn, owner, now):
            _version(conn)
            association, queue, workflow = _owned_rows(conn, owner, ident)
            try:
                existing = _queue_retry(conn, owner, key, digest, "queue_lease")
            except QueueError as exc:
                raise AssociationError("Operation key was already used for a different request.") from exc
            if existing is not None:
                lease = conn.execute("SELECT queue_id FROM queue_leases WHERE id=? AND owner_id=?", (existing, owner)).fetchone()
                if lease is None or lease["queue_id"] != association["queue_id"]:
                    raise StateStorageError("Stored operation result is inconsistent.")
                return existing
            if workflow["status"] != "planned":
                raise AssociationError("A queued item and planned workflow are required.")
            workflow_key = _pair_child_key()
            if (workflow_key == key or conn.execute(
                    "SELECT 1 FROM state_operations WHERE owner_id=? AND operation_key=?", (owner, workflow_key)
                    ).fetchone() is not None):
                raise StateStorageError("Internal operation key collision.")
            try:
                lease_id, until = _claim_in_transaction(conn, owner, now, queue, duration, key, digest)
            except QueueError as exc:
                raise AssociationError("Task is not ready for a lease.") from exc
            workflow_id = association["workflow_id"]
            conn.execute("UPDATE task_workflows SET status='in_progress', output_hash=NULL, output_size=NULL, updated_at=? WHERE id=? AND owner_id=?",
                         (now, workflow_id, owner))
            _workflow_audit(conn, now, owner, "workflow_started", workflow_id)
            _workflow_remember(conn, owner, workflow_key, _workflow_request("workflow_start", [workflow_id]), workflow_id)
            if int(self._auth.clock()) >= until:
                raise AssociationError("Lease expired before commit.")
            return lease_id

    def retry_pair(self, token: str, record_id: str, lease_id: str, *, operation_key: str) -> str:
        """Release a current lease for a failed in-progress local attempt."""
        return self._reschedule_pair(token, record_id, lease_id, operation_key, expired=False)

    def recover_pair(self, token: str, record_id: str, lease_id: str, *, operation_key: str) -> str:
        """Recover an expired linked lease without changing workflow output."""
        return self._reschedule_pair(token, record_id, lease_id, operation_key, expired=True)

    def _reschedule_pair(self, token, record_id, lease_id, operation_key, *, expired):
        ident, lease_id, key = _input(_record_id, record_id), _input(_record_id, lease_id), _input(_key, operation_key)
        digest = _queue_request("association_recover_pair" if expired else "association_retry_pair", [ident, lease_id])
        with _authorized_transaction(self._auth, self._state, token) as (conn, owner, now):
            _version(conn)
            association, queue, workflow = _owned_rows(conn, owner, ident)
            try:
                existing = _queue_retry(conn, owner, key, digest, "task_queue")
            except QueueError as exc:
                raise AssociationError("Operation key was already used for a different request.") from exc
            if existing is not None:
                if existing != association["queue_id"]:
                    raise StateStorageError("Stored operation result is inconsistent.")
                return existing
            allowed = ("in_progress", "pending_review") if expired else ("in_progress",)
            if workflow["status"] not in allowed:
                raise AssociationError("Local workflow state does not permit rescheduling.")
            try:
                result = _reschedule_in_transaction(conn, owner, now, queue, lease_id, key, digest, expired=expired)
            except QueueError as exc:
                raise AssociationError("Lease is stale or transition is not allowed.") from exc
            if (int(self._auth.clock()) >= queue["lease_until"]) != expired:
                raise AssociationError("Lease timing changed before commit.")
            return result

    def resume_pair(self, token: str, record_id: str, *, operation_key: str, lease_seconds: int) -> str:
        """Claim a ready linked queue item without changing its active workflow.

        Exact retries may return an inactive lease. This opt-in operation cannot
        prevent independent parent calls or fence work outside the local database.
        """
        ident, key = _input(_record_id, record_id), _input(_key, operation_key)
        try:
            duration = _queue_integer(lease_seconds, 1, 3600)
        except QueueError as exc:
            raise AssociationError("Invalid association input.") from exc
        digest = _queue_request("association_resume_pair", [ident, duration])
        with _authorized_transaction(self._auth, self._state, token) as (conn, owner, now):
            _version(conn)
            association, queue, workflow = _owned_rows(conn, owner, ident)
            try:
                existing = _queue_retry(conn, owner, key, digest, "queue_lease")
            except QueueError as exc:
                raise AssociationError("Operation key was already used for a different request.") from exc
            if existing is not None:
                lease = conn.execute("SELECT queue_id FROM queue_leases WHERE id=? AND owner_id=?", (existing, owner)).fetchone()
                if lease is None or lease["queue_id"] != association["queue_id"]:
                    raise StateStorageError("Stored operation result is inconsistent.")
                return existing
            if workflow["status"] not in ("in_progress", "pending_review"):
                raise AssociationError("Local workflow state does not permit resumption.")
            try:
                lease_id, until = _claim_in_transaction(conn, owner, now, queue, duration, key, digest)
            except QueueError as exc:
                raise AssociationError("Task is not ready for a lease.") from exc
            if int(self._auth.clock()) >= until:
                raise AssociationError("Lease expired before commit.")
            return lease_id

    def submit_pair(self, token: str, record_id: str, lease_id: str, output: str, *, operation_key: str) -> str:
        """Submit output for local review while the linked lease is current.

        An exact retry acknowledges an earlier mutation, not an active lease or
        permission to do work. Independent workflow APIs can bypass this check.
        """
        return self._review_operation(token, record_id, lease_id, output, operation_key, action="submit")

    def review_pair(self, token: str, record_id: str, lease_id: str, output: str, *,
                    approved: bool, confirmed: bool = False, operation_key: str) -> str:
        """Record an explicit local decision; this does not prove human review."""
        if confirmed is not True or type(approved) is not bool:
            raise AssociationError("Explicit review confirmation and a boolean decision are required.")
        return self._review_operation(token, record_id, lease_id, output, operation_key, action="review", approved=approved)

    def _review_operation(self, token, record_id, lease_id, output, operation_key, *, action, approved=None):
        ident, lease_id, key = _input(_record_id, record_id), _input(_record_id, lease_id), _input(_key, operation_key)
        try:
            output_hash, size = _workflow_output(output)
        except WorkflowError as exc:
            raise AssociationError("Invalid association input.") from exc
        values = [ident, lease_id, output_hash.hex(), size]
        if action == "review":
            values.append(approved)
        digest = _workflow_request("association_" + action + "_pair", values)
        with _authorized_transaction(self._auth, self._state, token) as (conn, owner, now):
            _version(conn)
            association, queue, workflow = _owned_rows(conn, owner, ident)
            try:
                existing = _workflow_retry(conn, owner, key, digest)
            except WorkflowError as exc:
                raise AssociationError("Operation key was already used for a different request.") from exc
            if existing is not None:
                if existing != association["workflow_id"]:
                    raise StateStorageError("Stored operation result is inconsistent.")
                return existing
            if (queue["status"] != "leased" or queue["current_lease_id"] != lease_id
                    or now >= queue["lease_until"]):
                raise AssociationError("A current unexpired lease is required.")
            try:
                if action == "submit":
                    result = _workflow_submit_in_transaction(conn, owner, now, workflow, output_hash, size, key, digest)
                else:
                    result = _workflow_review_in_transaction(conn, owner, now, workflow, output_hash, size, approved, key, digest)
            except WorkflowError as exc:
                raise AssociationError("Local workflow transition is not allowed.") from exc
            if int(self._auth.clock()) >= queue["lease_until"]:
                raise AssociationError("Lease expired before commit.")
            return result

    def complete_pair(self, token: str, record_id: str, lease_id: str, output: str, *,
                      confirmed: bool = False, operation_key: str) -> str:
        """Approve submitted output and close the linked queue locally (v11).

        This records a local decision, not human identity, correct work, an
        external-effect fence or payment. Exact retries acknowledge history.
        """
        if confirmed is not True:
            raise AssociationError("Explicit review confirmation is required.")
        ident, lease_id, key = _input(_record_id, record_id), _input(_record_id, lease_id), _input(_key, operation_key)
        try:
            output_hash, size = _workflow_output(output)
        except WorkflowError as exc:
            raise AssociationError("Invalid association input.") from exc
        digest = _workflow_request("association_complete_pair", [ident, lease_id, output_hash.hex(), size])
        with _authorized_transaction(self._auth, self._state, token) as (conn, owner, now):
            if conn.execute("PRAGMA user_version").fetchone()[0] != 11:
                raise StateStorageError("State schema version 11 is required for paired completion.")
            association, queue, workflow = _owned_rows(conn, owner, ident)
            existing = conn.execute("SELECT request_hash,resource_type,resource_id FROM state_operations WHERE owner_id=? AND operation_key=?", (owner, key)).fetchone()
            if existing is not None:
                if existing["resource_type"] != "paired_completion" or not hmac.compare_digest(existing["request_hash"], digest):
                    raise AssociationError("Operation key was already used for a different request.")
                if existing["resource_id"] != ident or not _has_paired_completion(conn, owner, association["queue_id"]):
                    raise StateStorageError("Stored operation result is inconsistent.")
                return ident
            if _has_paired_completion(conn, owner, association["queue_id"]):
                raise AssociationError("A local completion already exists.")
            if (queue["status"] != "leased" or queue["current_lease_id"] != lease_id
                    or now >= queue["lease_until"]):
                raise AssociationError("A current unexpired lease is required.")
            workflow_key = _pair_child_key()
            if (workflow_key == key or conn.execute("SELECT 1 FROM state_operations WHERE owner_id=? AND operation_key=?", (owner, workflow_key)).fetchone() is not None):
                raise StateStorageError("Internal operation key collision.")
            workflow_id = association["workflow_id"]
            workflow_digest = _workflow_request("workflow_review", [workflow_id, output_hash.hex(), size, True])
            try:
                _workflow_review_in_transaction(conn, owner, now, workflow, output_hash, size, True, workflow_key, workflow_digest)
            except WorkflowError as exc:
                raise AssociationError("Local workflow transition is not allowed.") from exc
            conn.execute("INSERT INTO paired_completions VALUES (?,?,?,?,?,?)", (ident, owner, association["queue_id"], workflow_id, lease_id, now))
            _completion_audit(conn, now, owner, ident)
            conn.execute("INSERT INTO state_operations VALUES (?,?,?,?,?)", (owner, key, digest, "paired_completion", ident))
            if int(self._auth.clock()) >= queue["lease_until"]:
                raise AssociationError("Lease expired before commit.")
            return ident

    def abandon_pair(self, token: str, record_id: str, *, confirmed: bool = False,
                     operation_key: str) -> str:
        """Explicitly abandon an unfinished, no-longer-schedulable local pair.

        This discards any pending output hash/size; it does not undo external
        work or confirm payment. Exhausted/cancelled queue history is retained.
        """
        if confirmed is not True:
            raise AssociationError("Explicit abandonment confirmation is required.")
        ident, key = _input(_record_id, record_id), _input(_key, operation_key)
        digest = _digest("task_association_abandon_pair", ident)
        with _authorized_transaction(self._auth, self._state, token) as (conn, owner, now):
            _version(conn)
            association, queue, workflow = _owned_rows(conn, owner, ident)
            existing = _retry(conn, owner, key, digest)
            if existing is not None:
                if existing != ident:
                    raise StateStorageError("Stored operation result is inconsistent.")
                return existing
            if _has_paired_completion(conn, owner, association["queue_id"]):
                raise AssociationError("Completed pairs cannot be abandoned.")
            deadline_expired = (queue["status"] == "queued" and queue["deadline_at"] != 0
                                and now >= queue["deadline_at"])
            if (queue["status"] not in ("exhausted", "cancelled") and not deadline_expired
                    or workflow["status"] not in ("planned", "in_progress", "pending_review")):
                raise AssociationError("An unfinished, no-longer-schedulable pair is required.")
            workflow_key = _pair_child_key()
            queue_key = _pair_child_key() if deadline_expired else None
            keys = [workflow_key] + ([queue_key] if queue_key is not None else [])
            if (key in keys or len(set(keys)) != len(keys)
                    or any(conn.execute("SELECT 1 FROM state_operations WHERE owner_id=? AND operation_key=?", (owner, child)).fetchone() is not None for child in keys)):
                raise StateStorageError("Internal operation key collision.")
            queue_id, workflow_id = association["queue_id"], association["workflow_id"]
            if deadline_expired:
                conn.execute("UPDATE task_queue SET status='cancelled', updated_at=? WHERE id=? AND owner_id=?",
                             (now, queue_id, owner))
                _queue_audit(conn, now, owner, "queue_cancelled", queue_id)
                _queue_remember(conn, owner, queue_key, _queue_request("queue_cancel", [queue_id]), "task_queue", queue_id)
            conn.execute("UPDATE task_workflows SET status='cancelled', output_hash=NULL, output_size=NULL, updated_at=? WHERE id=? AND owner_id=?",
                         (now, workflow_id, owner))
            _workflow_audit(conn, now, owner, "workflow_cancelled", workflow_id)
            _workflow_remember(conn, owner, workflow_key, _workflow_request("workflow_cancel", [workflow_id]), workflow_id)
            _audit(conn, now, owner, ident, "task_association_pair_abandoned")
            _remember(conn, owner, key, digest, ident)
            if deadline_expired and int(self._auth.clock()) < queue["deadline_at"]:
                raise AssociationError("Deadline timing changed before commit.")
            return ident

    def cancel_pair(self, token: str, record_id: str, *, operation_key: str) -> str:
        """Cancel a queued item and planned workflow together, not external work.

        Only this operation is coordinated. Existing parent services can still
        mutate independently; no worker, external effect or review is fenced.
        """
        ident = _input(_record_id, record_id)
        key = _input(_key, operation_key)
        digest = _digest("task_association_cancel_pair", ident)
        with _authorized_transaction(self._auth, self._state, token) as (conn, owner, now):
            _version(conn)
            association, queue, workflow = _owned_rows(conn, owner, ident)
            existing = _retry(conn, owner, key, digest)
            if existing is not None:
                if existing != ident:
                    raise StateStorageError("Stored operation result is inconsistent.")
                return existing
            if queue["status"] != "queued" or workflow["status"] != "planned":
                raise AssociationError("A queued item and planned workflow are required.")
            queue_key, workflow_key = _pair_child_key(), _pair_child_key()
            if (queue_key == workflow_key or key in (queue_key, workflow_key)
                    or conn.execute("SELECT 1 FROM state_operations WHERE owner_id=? AND operation_key IN (?,?) LIMIT 1",
                                    (owner, queue_key, workflow_key)).fetchone() is not None):
                raise StateStorageError("Internal operation key collision.")
            queue_id, workflow_id = association["queue_id"], association["workflow_id"]
            conn.execute("UPDATE task_queue SET status='cancelled', updated_at=? WHERE id=? AND owner_id=?",
                         (now, queue_id, owner))
            _queue_audit(conn, now, owner, "queue_cancelled", queue_id)
            _queue_remember(conn, owner, queue_key, _queue_request("queue_cancel", [queue_id]), "task_queue", queue_id)
            conn.execute("UPDATE task_workflows SET status='cancelled', output_hash=NULL, output_size=NULL, updated_at=? WHERE id=? AND owner_id=?",
                         (now, workflow_id, owner))
            _workflow_audit(conn, now, owner, "workflow_cancelled", workflow_id)
            _workflow_remember(conn, owner, workflow_key, _workflow_request("workflow_cancel", [workflow_id]), workflow_id)
            _audit(conn, now, owner, ident, "task_association_pair_cancelled")
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
            complete = _has_paired_completion(conn, owner, association["queue_id"])
            active = queue["status"] == "leased" and now < queue["lease_until"] and not complete
            return {
                "association_id": association["id"],
                "queue_id": association["queue_id"],
                "workflow_id": association["workflow_id"],
                "observed_at": now,
                "queue_status": "completed" if complete else queue["status"],
                "workflow_status": workflow["status"],
                "queue_ready_at_observation": ready,
                "lease_active_at_observation": active,
                "deadline_passed_at_observation": deadline_passed,
            }

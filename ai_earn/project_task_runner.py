"""Durable project task runner — lease-fenced, audited, retry-aware.

Owner-scoped, session-authorized, transactional. Every external action has:
execution record (task_execution_logs + state_audit), idempotency (operation_key),
clear state (project_tasks.status), audit (state_audit), retry policy (attempts/max_attempts/backoff),
cancellation (cancel). No uncontrolled autonomy: requires manual review (pending_review->approved) before scheduling.
"""

from __future__ import annotations

import hmac
import json
import secrets
import threading

from .auth import AccessDenied, AuthService
from .opportunity_drafts import _digest, _key, _record_id
from .state_authorization import _authorized_transaction
from .state_storage import StateStorage, StateStorageError

_MAX_TIME = 4102444800


class ProjectTaskRunnerError(Exception):
    pass


def _version(conn):
    ver = conn.execute("PRAGMA user_version").fetchone()[0]
    if ver < 24:
        raise StateStorageError("State schema version 24 is required for project task runner.")


def _request(action, values):
    return _digest(action, json.dumps(values, ensure_ascii=True, separators=(",", ":")))


def _check_dependencies_completed(conn, owner, task_id):
    rows = conn.execute(
        "SELECT depends_on_task_id FROM task_dependencies WHERE owner_id=? AND task_id=?",
        (owner, task_id),
    ).fetchall()
    for (dep,) in rows:
        r = conn.execute(
            "SELECT status FROM project_tasks WHERE id=? AND owner_id=?", (dep, owner)
        ).fetchone()
        if r is None or r["status"] != "completed":
            return False
    return True


def _claim_in_transaction(conn, owner, now, task, duration, key, digest):
    # task is row dict
    if task["status"] != "scheduled":
        raise ProjectTaskRunnerError("Task is not scheduled.")
    if task["available_at"] and task["available_at"] > now:
        raise ProjectTaskRunnerError("Task not yet available.")
    if task["deadline_at"] and task["deadline_at"] <= now:
        raise ProjectTaskRunnerError("Task deadline passed.")
    if task["lease_id"] is not None and task["lease_until"] and now < task["lease_until"]:
        raise ProjectTaskRunnerError("Task already leased.")
    if not _check_dependencies_completed(conn, owner, task["id"]):
        raise ProjectTaskRunnerError("Dependencies not satisfied.")
    until = now + duration
    if until > _MAX_TIME:
        raise ProjectTaskRunnerError("Invalid lease timing.")
    if task["deadline_at"] and until > task["deadline_at"]:
        until = task["deadline_at"]
    lease_id = secrets.token_hex(16)
    conn.execute(
        "UPDATE project_tasks SET status='in_progress', lease_id=?, lease_until=?, attempts=attempts+1, updated_at=? WHERE id=? AND owner_id=?",
        (lease_id, until, now, task["id"], owner),
    )
    conn.execute(
        "INSERT INTO state_audit(occurred_at, actor_id, event, resource_type, resource_id) VALUES(?,?,?,?,?)",
        (now, owner, "project_task_started", "project_task", task["id"]),
    )
    # Remember claim operation as project_task transition for idempotency and history consistency
    conn.execute(
        "INSERT INTO state_operations(owner_id, operation_key, request_hash, resource_type, resource_id) VALUES(?,?,?,?,?)",
        (owner, key, digest, "project_task", task["id"]),
    )
    # Log
    conn.execute(
        "INSERT INTO task_execution_logs(id, owner_id, task_id, message, created_at) VALUES(?,?,?,?,?)",
        (
            secrets.token_hex(16),
            owner,
            task["id"],
            f"Claimed lease {lease_id[:8]} until {until}",
            now,
        ),
    )
    return lease_id, until


def _complete_in_transaction(conn, owner, now, task, lease_id, key, digest):
    if (
        task["status"] != "in_progress"
        or task["lease_id"] != lease_id
        or now >= task["lease_until"]
    ):
        raise ProjectTaskRunnerError("Lease is stale or task not in progress.")
    conn.execute(
        "UPDATE project_tasks SET status='completed', lease_id=NULL, lease_until=NULL, updated_at=? WHERE id=? AND owner_id=?",
        (now, task["id"], owner),
    )
    conn.execute(
        "INSERT INTO state_audit(occurred_at, actor_id, event, resource_type, resource_id) VALUES(?,?,?,?,?)",
        (now, owner, "project_task_completed", "project_task", task["id"]),
    )
    conn.execute(
        "INSERT INTO state_operations(owner_id, operation_key, request_hash, resource_type, resource_id) VALUES(?,?,?,?,?)",
        (owner, key, digest, "project_task", task["id"]),
    )
    conn.execute(
        "INSERT INTO task_execution_logs(id, owner_id, task_id, message, created_at) VALUES(?,?,?,?,?)",
        (secrets.token_hex(16), owner, task["id"], f"Completed via lease {lease_id[:8]}", now),
    )


def _fail_in_transaction(
    conn, owner, now, task, lease_id, key, digest, *, error_msg: str, recoverable: bool
):
    if (
        task["status"] != "in_progress"
        or task["lease_id"] != lease_id
        or now >= task["lease_until"]
    ):
        raise ProjectTaskRunnerError("Lease is stale or task not in progress.")
    attempts = task["attempts"]
    max_attempts = task["max_attempts"]
    backoff = task["backoff_seconds"]
    if not recoverable or attempts >= max_attempts:
        # terminal failure
        conn.execute(
            "UPDATE project_tasks SET status='failed', lease_id=NULL, lease_until=NULL, available_at=0, updated_at=? WHERE id=? AND owner_id=?",
            (now, task["id"], owner),
        )
        conn.execute(
            "INSERT INTO state_audit(occurred_at, actor_id, event, resource_type, resource_id) VALUES(?,?,?,?,?)",
            (now, owner, "project_task_failed", "project_task", task["id"]),
        )
        conn.execute(
            "INSERT INTO state_operations(owner_id, operation_key, request_hash, resource_type, resource_id) VALUES(?,?,?,?,?)",
            (owner, key, digest, "project_task", task["id"]),
        )
        conn.execute(
            "INSERT INTO task_execution_logs(id, owner_id, task_id, message, created_at) VALUES(?,?,?,?,?)",
            (
                secrets.token_hex(16),
                owner,
                task["id"],
                f"Failed (terminal) via lease {lease_id[:8]}: {error_msg[:200]}",
                now,
            ),
        )
    else:
        available = now + backoff
        conn.execute(
            "UPDATE project_tasks SET status='failed', lease_id=NULL, lease_until=NULL, available_at=?, updated_at=? WHERE id=? AND owner_id=?",
            (available, now, task["id"], owner),
        )
        conn.execute(
            "INSERT INTO state_audit(occurred_at, actor_id, event, resource_type, resource_id) VALUES(?,?,?,?,?)",
            (now, owner, "project_task_failed", "project_task", task["id"]),
        )
        conn.execute(
            "INSERT INTO state_operations(owner_id, operation_key, request_hash, resource_type, resource_id) VALUES(?,?,?,?,?)",
            (owner, key, digest, "project_task", task["id"]),
        )
        conn.execute(
            "INSERT INTO task_execution_logs(id, owner_id, task_id, message, created_at) VALUES(?,?,?,?,?)",
            (
                secrets.token_hex(16),
                owner,
                task["id"],
                f"Failed (retryable) via lease {lease_id[:8]}: {error_msg[:200]} available_at {available}",
                now,
            ),
        )


class ProjectTaskRunner:
    def __init__(self, auth: AuthService, state: StateStorage):
        self._auth = auth
        self._state = state

    def claim_next(self, token: str, *, operation_key: str, lease_seconds: int = 60) -> dict | None:
        key = _key(operation_key)
        duration = lease_seconds
        if type(duration) is not int or not 1 <= duration <= 3600:
            raise ProjectTaskRunnerError("Invalid lease duration.")
        digest = _request("project_task_claim", [duration])
        with _authorized_transaction(self._auth, self._state, token) as (conn, owner, now):
            _version(conn)
            # Check existing operation idempotency
            row = conn.execute(
                "SELECT request_hash, resource_type, resource_id FROM state_operations WHERE owner_id=? AND operation_key=?",
                (owner, key),
            ).fetchone()
            if row is not None:
                if row["resource_type"] != "project_task" or not hmac.compare_digest(
                    row["request_hash"], digest
                ):
                    raise ProjectTaskRunnerError(
                        "Operation key was already used for a different request."
                    )
                task_id = row["resource_id"]
                task_row = conn.execute(
                    "SELECT * FROM project_tasks WHERE id=? AND owner_id=?", (task_id, owner)
                ).fetchone()
                if task_row is None:
                    raise StateStorageError("Stored operation result is unavailable.")
                return dict(task_row)
            # Find highest priority scheduled ready task
            # Order by priority DESC, deadline earliest (0 means no deadline last), available_at earliest
            # Also need available_at <= now
            candidates = conn.execute(
                "SELECT * FROM project_tasks WHERE owner_id=? AND status='scheduled' AND (available_at=0 OR available_at<=?) AND (deadline_at=0 OR deadline_at>?) ORDER BY priority DESC, CASE WHEN deadline_at=0 THEN 4102444800 ELSE deadline_at END ASC, available_at ASC, id COLLATE BINARY",
                (owner, now, now),
            ).fetchall()
            for task in candidates:
                if not _check_dependencies_completed(conn, owner, task["id"]):
                    continue
                if (
                    task["lease_id"] is not None
                    and task["lease_until"]
                    and now < task["lease_until"]
                ):
                    continue
                lease_id, until = _claim_in_transaction(
                    conn, owner, now, dict(task), duration, key, digest
                )
                # Re-fetch
                updated = conn.execute(
                    "SELECT * FROM project_tasks WHERE id=? AND owner_id=?", (task["id"], owner)
                ).fetchone()
                return dict(updated)
            return None

    def complete(self, token: str, task_id: str, lease_id: str, *, operation_key: str) -> str:
        tid = _record_id(task_id)
        lid = _record_id(lease_id)
        key = _key(operation_key)
        digest = _request("project_task_complete", [tid, lid])
        with _authorized_transaction(self._auth, self._state, token) as (conn, owner, now):
            _version(conn)
            row = conn.execute(
                "SELECT request_hash, resource_type, resource_id FROM state_operations WHERE owner_id=? AND operation_key=?",
                (owner, key),
            ).fetchone()
            if row is not None:
                if row["resource_type"] != "project_task" or not hmac.compare_digest(
                    row["request_hash"], digest
                ):
                    raise ProjectTaskRunnerError(
                        "Operation key was already used for a different request."
                    )
                return tid
            task = conn.execute(
                "SELECT * FROM project_tasks WHERE id=? AND owner_id=?", (tid, owner)
            ).fetchone()
            if task is None:
                raise AccessDenied("Access denied.")
            if task["lease_id"] != lid:
                raise ProjectTaskRunnerError("Lease mismatch.")
            _complete_in_transaction(conn, owner, now, dict(task), lid, key, digest)
            if int(self._auth.clock()) >= task["lease_until"]:
                raise ProjectTaskRunnerError("Lease expired before commit.")
            return tid

    def fail(
        self,
        token: str,
        task_id: str,
        lease_id: str,
        *,
        operation_key: str,
        error_message: str = "",
        recoverable: bool = True,
    ) -> str:
        tid = _record_id(task_id)
        lid = _record_id(lease_id)
        key = _key(operation_key)
        digest = _request("project_task_fail", [tid, lid, error_message[:200], recoverable])
        with _authorized_transaction(self._auth, self._state, token) as (conn, owner, now):
            _version(conn)
            row = conn.execute(
                "SELECT request_hash, resource_type, resource_id FROM state_operations WHERE owner_id=? AND operation_key=?",
                (owner, key),
            ).fetchone()
            if row is not None:
                if row["resource_type"] != "project_task" or not hmac.compare_digest(
                    row["request_hash"], digest
                ):
                    raise ProjectTaskRunnerError(
                        "Operation key was already used for a different request."
                    )
                return tid
            task = conn.execute(
                "SELECT * FROM project_tasks WHERE id=? AND owner_id=?", (tid, owner)
            ).fetchone()
            if task is None:
                raise AccessDenied("Access denied.")
            if task["lease_id"] != lid:
                raise ProjectTaskRunnerError("Lease mismatch.")
            _fail_in_transaction(
                conn,
                owner,
                now,
                dict(task),
                lid,
                key,
                digest,
                error_msg=error_message,
                recoverable=recoverable,
            )
            if int(self._auth.clock()) >= task["lease_until"]:
                raise ProjectTaskRunnerError("Lease expired before commit.")
            return tid

    def retry(self, token: str, task_id: str, *, operation_key: str) -> str:
        tid = _record_id(task_id)
        key = _key(operation_key)
        digest = _request("project_task_retry", [tid])
        with _authorized_transaction(self._auth, self._state, token) as (conn, owner, now):
            _version(conn)
            row = conn.execute(
                "SELECT request_hash, resource_type, resource_id FROM state_operations WHERE owner_id=? AND operation_key=?",
                (owner, key),
            ).fetchone()
            if row is not None:
                if row["resource_type"] != "project_task" or not hmac.compare_digest(
                    row["request_hash"], digest
                ):
                    raise ProjectTaskRunnerError(
                        "Operation key was already used for a different request."
                    )
                return tid
            task = conn.execute(
                "SELECT * FROM project_tasks WHERE id=? AND owner_id=?", (tid, owner)
            ).fetchone()
            if task is None:
                raise AccessDenied("Access denied.")
            if task["status"] != "failed":
                raise ProjectTaskRunnerError("Task not in failed state.")
            if task["available_at"] and task["available_at"] > now:
                raise ProjectTaskRunnerError("Task not yet available for retry.")
            if task["attempts"] >= task["max_attempts"]:
                raise ProjectTaskRunnerError("Max attempts reached.")
            # Check deadline
            if task["deadline_at"] and task["deadline_at"] <= now:
                raise ProjectTaskRunnerError("Deadline passed.")
            if not _check_dependencies_completed(conn, owner, tid):
                raise ProjectTaskRunnerError("Dependencies not satisfied.")
            conn.execute(
                "UPDATE project_tasks SET status='scheduled', updated_at=? WHERE id=? AND owner_id=?",
                (now, tid, owner),
            )
            conn.execute(
                "INSERT INTO state_audit(occurred_at, actor_id, event, resource_type, resource_id) VALUES(?,?,?,?,?)",
                (now, owner, "project_task_retry_scheduled", "project_task", tid),
            )
            conn.execute(
                "INSERT INTO state_operations(owner_id, operation_key, request_hash, resource_type, resource_id) VALUES(?,?,?,?,?)",
                (owner, key, digest, "project_task", tid),
            )
            conn.execute(
                "INSERT INTO task_execution_logs(id, owner_id, task_id, message, created_at) VALUES(?,?,?,?,?)",
                (
                    secrets.token_hex(16),
                    owner,
                    tid,
                    f"Retried after failure (attempt {task['attempts'] + 1})",
                    now,
                ),
            )
            return tid

    def recover(self, token: str, task_id: str, lease_id: str, *, operation_key: str) -> str:
        tid = _record_id(task_id)
        lid = _record_id(lease_id)
        key = _key(operation_key)
        digest = _request("project_task_recover", [tid, lid])
        with _authorized_transaction(self._auth, self._state, token) as (conn, owner, now):
            _version(conn)
            row = conn.execute(
                "SELECT request_hash, resource_type, resource_id FROM state_operations WHERE owner_id=? AND operation_key=?",
                (owner, key),
            ).fetchone()
            if row is not None:
                if row["resource_type"] != "project_task" or not hmac.compare_digest(
                    row["request_hash"], digest
                ):
                    raise ProjectTaskRunnerError(
                        "Operation key was already used for a different request."
                    )
                return tid
            task = conn.execute(
                "SELECT * FROM project_tasks WHERE id=? AND owner_id=?", (tid, owner)
            ).fetchone()
            if task is None:
                raise AccessDenied("Access denied.")
            if task["lease_id"] != lid or task["status"] != "in_progress":
                raise ProjectTaskRunnerError("Task not in leased state.")
            if now < task["lease_until"]:
                raise ProjectTaskRunnerError("Lease not yet expired.")
            # Recover: always mark failed; available_at set for retry backoff if recoverable
            available = (
                now + task["backoff_seconds"] if task["attempts"] < task["max_attempts"] else 0
            )
            if task["attempts"] >= task["max_attempts"] or (
                task["deadline_at"] and task["deadline_at"] <= now
            ):
                conn.execute(
                    "UPDATE project_tasks SET status='failed', lease_id=NULL, lease_until=NULL, available_at=0, updated_at=? WHERE id=? AND owner_id=?",
                    (now, tid, owner),
                )
            else:
                conn.execute(
                    "UPDATE project_tasks SET status='failed', lease_id=NULL, lease_until=NULL, available_at=?, updated_at=? WHERE id=? AND owner_id=?",
                    (available, now, tid, owner),
                )
            conn.execute(
                "INSERT INTO state_audit(occurred_at, actor_id, event, resource_type, resource_id) VALUES(?,?,?,?,?)",
                (now, owner, "project_task_failed", "project_task", tid),
            )
            # Also log as recovered?
            conn.execute(
                "INSERT INTO state_operations(owner_id, operation_key, request_hash, resource_type, resource_id) VALUES(?,?,?,?,?)",
                (owner, key, digest, "project_task", tid),
            )
            conn.execute(
                "INSERT INTO task_execution_logs(id, owner_id, task_id, message, created_at) VALUES(?,?,?,?,?)",
                (secrets.token_hex(16), owner, tid, f"Recovered lease {lid[:8]} after expiry", now),
            )
            return tid

    def cancel(self, token: str, task_id: str, *, operation_key: str) -> str:
        tid = _record_id(task_id)
        key = _key(operation_key)
        digest = _request("project_task_cancel", [tid])
        with _authorized_transaction(self._auth, self._state, token) as (conn, owner, now):
            _version(conn)
            row = conn.execute(
                "SELECT request_hash, resource_type, resource_id FROM state_operations WHERE owner_id=? AND operation_key=?",
                (owner, key),
            ).fetchone()
            if row is not None:
                if row["resource_type"] != "project_task" or not hmac.compare_digest(
                    row["request_hash"], digest
                ):
                    raise ProjectTaskRunnerError(
                        "Operation key was already used for a different request."
                    )
                return tid
            task = conn.execute(
                "SELECT * FROM project_tasks WHERE id=? AND owner_id=?", (tid, owner)
            ).fetchone()
            if task is None:
                raise AccessDenied("Access denied.")
            if task["status"] in ("completed", "cancelled"):
                raise ProjectTaskRunnerError("Transition is not allowed.")
            # Allow cancel from any non-terminal except completed
            # Determine event name based on current status
            event_map = {
                "draft": "project_task_cancelled",
                "pending_review": "project_task_cancelled",
                "approved": "project_task_cancelled",
                "scheduled": "project_task_cancelled",
                "in_progress": "project_task_cancelled",
                "failed": "project_task_cancelled",
                "rejected": "project_task_cancelled",
            }
            event = event_map.get(task["status"])
            if event is None:
                raise ProjectTaskRunnerError("Transition is not allowed.")
            # If leased and not expired, allow cancel but clear lease
            conn.execute(
                "UPDATE project_tasks SET status='cancelled', lease_id=NULL, lease_until=NULL, updated_at=? WHERE id=? AND owner_id=?",
                (now, tid, owner),
            )
            conn.execute(
                "INSERT INTO state_audit(occurred_at, actor_id, event, resource_type, resource_id) VALUES(?,?,?,?,?)",
                (now, owner, event, "project_task", tid),
            )
            conn.execute(
                "INSERT INTO state_operations(owner_id, operation_key, request_hash, resource_type, resource_id) VALUES(?,?,?,?,?)",
                (owner, key, digest, "project_task", tid),
            )
            conn.execute(
                "INSERT INTO task_execution_logs(id, owner_id, task_id, message, created_at) VALUES(?,?,?,?,?)",
                (
                    secrets.token_hex(16),
                    owner,
                    tid,
                    f"Cancelled from {task['status']} via {event}",
                    now,
                ),
            )
            return tid

    def run_once(
        self, token: str, handler, *, operation_key: str, lease_seconds: int = 60
    ) -> dict | None:
        claimed = self.claim_next(token, operation_key=operation_key, lease_seconds=lease_seconds)
        if claimed is None:
            return None
        task_id = claimed["id"]
        lease_id = claimed["lease_id"]
        # Build context
        ctx = {"task_id": task_id, "lease_id": lease_id, "task": claimed}
        try:
            output = handler(ctx)
            if not isinstance(output, str):
                raise ProjectTaskRunnerError("Handler must return string.")
            # Use output as log message (bounded)
            if len(output) > 2000:
                raise ProjectTaskRunnerError("Output too long.")
            # Complete
            self.complete(token, task_id, lease_id, operation_key=operation_key + "_complete")
            # Log handler output as execution log
            with _authorized_transaction(self._auth, self._state, token) as (conn, owner, now):
                conn.execute(
                    "INSERT INTO task_execution_logs(id, owner_id, task_id, message, created_at) VALUES(?,?,?,?,?)",
                    (secrets.token_hex(16), owner, task_id, f"Handler output: {output[:500]}", now),
                )
            return {"task_id": task_id, "lease_id": lease_id, "completed": True}
        except Exception as exc:
            # Determine recoverable
            recoverable = not isinstance(exc, ProjectTaskRunnerError)  # simple heuristic
            err_msg = str(exc)[:200]
            try:
                self.fail(
                    token,
                    task_id,
                    lease_id,
                    operation_key=operation_key + "_fail",
                    error_message=err_msg,
                    recoverable=recoverable,
                )
            except Exception:
                # If fail itself fails due to lease expiry, leave for recover
                pass
            raise ProjectTaskRunnerError(f"Handler failed: {exc}") from exc

    # Background polling similar to ManagedTaskRunner
    def start_polling(
        self,
        token_provider,
        handler,
        *,
        lease_seconds=60,
        poll_interval=1.0,
        operation_key_prefix="pt_runner",
    ):
        if not callable(token_provider) or not callable(handler):
            raise ProjectTaskRunnerError("Invalid polling args.")
        self._stop_event = threading.Event()
        self._running = True

        def _loop():
            counter = 0
            while not getattr(self, "_stop_event", threading.Event()).is_set():
                try:
                    token = token_provider()
                    op_key = f"{operation_key_prefix}_{counter}"
                    counter += 1
                    result = self.claim_next(
                        token, operation_key=op_key, lease_seconds=lease_seconds
                    )
                    if result is None:
                        self._stop_event.wait(poll_interval)
                    else:
                        # execute handler in same thread
                        try:
                            ctx = {
                                "task_id": result["id"],
                                "lease_id": result["lease_id"],
                                "task": result,
                            }
                            handler(ctx)
                            self.complete(
                                token,
                                result["id"],
                                result["lease_id"],
                                operation_key=op_key + "_complete",
                            )
                        except Exception as e:
                            try:
                                self.fail(
                                    token,
                                    result["id"],
                                    result["lease_id"],
                                    operation_key=op_key + "_fail",
                                    error_message=str(e)[:200],
                                    recoverable=True,
                                )
                            except Exception:
                                pass
                        self._stop_event.wait(0.1)
                except Exception:
                    self._stop_event.wait(poll_interval)
            self._running = False

        self._thread = threading.Thread(target=_loop, daemon=True)
        self._thread.start()

    def stop(self, *, wait=True, timeout=None):
        if hasattr(self, "_stop_event"):
            self._stop_event.set()
            if wait and hasattr(self, "_thread"):
                self._thread.join(timeout=timeout)

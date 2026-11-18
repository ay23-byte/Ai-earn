"""Project tasks — Phase 5 task management and safe execution (v23).

Owner-scoped, session-authorized, transactional, idempotent, audited.
Supports: create from opportunity, subtasks, priorities, deadlines,
estimated/actual time, dependencies, execution logs, manual review,
retry/cancellation, recovery after restart. No uncontrolled autonomous execution.
All external actions have execution record, idempotency, state, audit,
retry policy, cancellation strategy.
"""
from __future__ import annotations

import builtins
import hmac
import json
import secrets
import sqlite3

from .auth import AccessDenied, AuthService
from .opportunity_drafts import DraftError, _digest as _base_digest, _key, _record_id, _title
from .state_authorization import _authorized_transaction
from .state_storage import StateStorage, StateStorageError

_RESOURCE = "project_task"
_MAX_DESC = 2000
_MAX_LOG_MSG = 2000
_MAX_TIME = 4102444800
_MAX_DURATION = 31536000  # 1 year in seconds
_STATUSES = frozenset({"draft", "pending_review", "approved", "scheduled", "in_progress", "completed", "failed", "cancelled", "rejected"})

# Transitions: (current_status, event) -> next_status, None is creation
_TRANSITIONS: dict[tuple[object, str], str] = {
    (None, "project_task_created"): "draft",
    ("draft", "project_task_submitted"): "pending_review",
    ("draft", "project_task_scheduled"): "scheduled",
    ("draft", "project_task_started"): "in_progress",
    ("draft", "project_task_cancelled"): "cancelled",
    ("pending_review", "project_task_approved"): "approved",
    ("pending_review", "project_task_rejected"): "rejected",
    ("pending_review", "project_task_cancelled"): "cancelled",
    ("approved", "project_task_scheduled"): "scheduled",
    ("approved", "project_task_started"): "in_progress",
    ("approved", "project_task_completed"): "completed",
    ("approved", "project_task_cancelled"): "cancelled",
    ("scheduled", "project_task_started"): "in_progress",
    ("scheduled", "project_task_cancelled"): "cancelled",
    ("scheduled", "project_task_failed"): "failed",
    ("in_progress", "project_task_pending_review"): "pending_review",
    ("in_progress", "project_task_completed"): "completed",
    ("in_progress", "project_task_failed"): "failed",
    ("in_progress", "project_task_cancelled"): "cancelled",
    ("failed", "project_task_retried"): "in_progress",
    ("failed", "project_task_retry_scheduled"): "scheduled",
    ("failed", "project_task_cancelled"): "cancelled",
    ("rejected", "project_task_reworked"): "draft",
    ("rejected", "project_task_cancelled"): "cancelled",
}

class ProjectTaskError(Exception):
    pass

def _input(validator, value):
    try:
        return validator(value)
    except (DraftError, ProjectTaskError) as exc:
        raise ProjectTaskError(str(exc)) from exc
    except Exception as exc:
        raise ProjectTaskError("Invalid project task input.") from exc

def _description(value: str) -> str:
    if not isinstance(value, str) or len(value) > _MAX_DESC:
        raise ProjectTaskError("Invalid description.")
    if any(ord(c) < 32 or ord(c) == 127 for c in value if c not in "\n\t"):
        raise ProjectTaskError("Invalid description.")
    return value

def _priority(value: int) -> int:
    if type(value) is not int or not 0 <= value <= 9:
        raise ProjectTaskError("Priority must be 0..9.")
    return value

def _deadline(value: int) -> int:
    if type(value) is not int or not 0 <= value <= _MAX_TIME:
        raise ProjectTaskError("Invalid deadline.")
    return value

def _duration(value: int) -> int:
    if type(value) is not int or not 0 <= value <= _MAX_DURATION:
        raise ProjectTaskError("Invalid duration.")
    return value

def _log_message(value: str) -> str:
    if not isinstance(value, str) or not 1 <= len(value) <= _MAX_LOG_MSG:
        raise ProjectTaskError("Invalid log message.")
    if any(ord(c) < 32 or ord(c) == 127 for c in value if c not in "\n\t"):
        raise ProjectTaskError("Invalid log message.")
    return value

def _version(conn: sqlite3.Connection) -> None:
    from .state_storage import SUPPORTED_STATE_VERSIONS
    ver = conn.execute("PRAGMA user_version").fetchone()[0]
    if ver not in SUPPORTED_STATE_VERSIONS or ver < 23:
        raise StateStorageError("State schema version 23 is required for project tasks.")

def _retry(conn: sqlite3.Connection, owner: str, key: str, digest: bytes) -> str | None:
    row = conn.execute("SELECT request_hash, resource_type, resource_id FROM state_operations WHERE owner_id=? AND operation_key=?", (owner, key)).fetchone()
    if row is None:
        return None
    if row["resource_type"] != _RESOURCE or not hmac.compare_digest(row["request_hash"], digest):
        raise ProjectTaskError("Operation key was already used for a different request.")
    if conn.execute("SELECT 1 FROM project_tasks WHERE id=? AND owner_id=?", (row["resource_id"], owner)).fetchone() is None:
        raise StateStorageError("Stored operation result is unavailable.")
    return row["resource_id"]

def _audit(conn: sqlite3.Connection, now: int, owner: str, event: str, record_id: str) -> None:
    conn.execute("INSERT INTO state_audit(occurred_at, actor_id, event, resource_type, resource_id) VALUES(?,?,?,?,?)", (now, owner, event, _RESOURCE, record_id))

def _remember(conn: sqlite3.Connection, owner: str, key: str, digest: bytes, record_id: str) -> None:
    conn.execute("INSERT INTO state_operations(owner_id, operation_key, request_hash, resource_type, resource_id) VALUES(?,?,?,?,?)", (owner, key, digest, _RESOURCE, record_id))

def _request(action: str, values: list) -> bytes:
    return _base_digest(action, json.dumps(values, ensure_ascii=True, separators=(",", ":")))

def _owned(conn: sqlite3.Connection, owner: str, record_id: str) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM project_tasks WHERE id=? AND owner_id=?", (record_id, owner)).fetchone()
    if row is None:
        raise AccessDenied("Access denied.")
    events = conn.execute("SELECT event FROM state_audit WHERE actor_id=? AND resource_type=? AND resource_id=? ORDER BY id", (owner, _RESOURCE, record_id)).fetchall()
    if not events:
        raise StateStorageError("Project task history is inconsistent.")
    replayed = None
    for (event,) in events:
        step = (replayed, event)
        if step not in _TRANSITIONS:
            raise StateStorageError("Project task history is inconsistent.")
        replayed = _TRANSITIONS[step]
    if replayed != row["status"]:
        raise StateStorageError("Project task history is inconsistent.")
    count = conn.execute("SELECT count(*) FROM state_operations WHERE owner_id=? AND resource_type=? AND resource_id=?", (owner, _RESOURCE, record_id)).fetchone()[0]
    if count != len(events):
        raise StateStorageError("Project task history is inconsistent.")
    return row

def _view(row: sqlite3.Row) -> dict:
    d = dict(row)
    d["verification"] = "unverified"
    return d

def _check_cycle(conn: sqlite3.Connection, owner: str, task_id: str, depends_on: str) -> None:
    # Check for cycle via DFS traversing dependencies
    if task_id == depends_on:
        raise ProjectTaskError("Task cannot depend on itself.")
    # Build graph of dependencies for owner
    visited = set()
    stack = [depends_on]
    while stack:
        cur = stack.pop()
        if cur in visited:
            continue
        visited.add(cur)
        if cur == task_id:
            raise ProjectTaskError("Dependency would create a cycle.")
        rows = conn.execute("SELECT depends_on_task_id FROM task_dependencies WHERE owner_id=? AND task_id=?", (owner, cur)).fetchall()
        for (dep,) in rows:
            if dep not in visited:
                stack.append(dep)

class ProjectTasks:
    """Owner-scoped project task management with subtasks, dependencies, logs."""

    def __init__(self, auth: AuthService, state: StateStorage):
        self._auth = auth
        self._state = state

    def create(
        self,
        token: str,
        title: str,
        *,
        operation_key: str,
        description: str = "",
        opportunity_id: str | None = None,
        parent_task_id: str | None = None,
        priority: int = 5,
        deadline_at: int = 0,
        estimated_duration_seconds: int = 0,
    ) -> str:
        title = _input(_title, title)
        description = _input(_description, description)
        priority = _input(_priority, priority)
        deadline = _input(_deadline, deadline_at)
        estimated = _input(_duration, estimated_duration_seconds)
        key = _input(_key, operation_key)
        # Validate opportunity_id and parent_task_id format if provided
        if opportunity_id is not None:
            opportunity_id = _input(_record_id, opportunity_id)
        if parent_task_id is not None:
            parent_task_id = _input(_record_id, parent_task_id)
        digest = _request("project_task_create", [title, description, priority, deadline, estimated, opportunity_id or "", parent_task_id or ""])
        with _authorized_transaction(self._auth, self._state, token) as (conn, owner, now):
            _version(conn)
            existing = _retry(conn, owner, key, digest)
            if existing is not None:
                return existing
            if deadline != 0 and deadline <= now:
                raise ProjectTaskError("Deadline must be in the future.")
            # Validate opportunity exists and belongs to owner if provided
            if opportunity_id is not None:
                row = conn.execute("SELECT 1 FROM opportunities WHERE id=? AND owner_id=?", (opportunity_id, owner)).fetchone()
                if row is None:
                    raise ProjectTaskError("Opportunity not found.")
            # Validate parent exists and belongs to owner if provided
            if parent_task_id is not None:
                parent = conn.execute("SELECT 1 FROM project_tasks WHERE id=? AND owner_id=?", (parent_task_id, owner)).fetchone()
                if parent is None:
                    raise ProjectTaskError("Parent task not found.")
            ident = secrets.token_hex(16)
            conn.execute(
                "INSERT INTO project_tasks(id, owner_id, opportunity_id, parent_task_id, title, description, priority, deadline_at, estimated_duration_seconds, actual_duration_seconds, status, created_at, updated_at) VALUES(?,?,?,?,?,?,?,?,?,?, 'draft', ?, ?)",
                (ident, owner, opportunity_id, parent_task_id, title, description, priority, deadline, estimated, 0, now, now),
            )
            _audit(conn, now, owner, "project_task_created", ident)
            _remember(conn, owner, key, digest, ident)
            # Initial execution log: creation record
            log_id = secrets.token_hex(16)
            conn.execute("INSERT INTO task_execution_logs(id, owner_id, task_id, message, created_at) VALUES(?,?,?,?,?)", (log_id, owner, ident, f"Task created: {title[:100]}", now))
            # Audit for log creation (not counted in _owned check, but we ensure logs are separate)
            return ident

    def create_from_opportunity(self, token: str, opportunity_id: str, *, operation_key: str, title: str | None = None, description: str = "", priority: int = 5, deadline_at: int = 0, estimated_duration_seconds: int = 0) -> str:
        # Fetch opportunity to derive title if not provided
        opp_id = _input(_record_id, opportunity_id)
        key = _input(_key, operation_key)
        with _authorized_transaction(self._auth, self._state, token) as (conn, owner, now):
            _version(conn)
            row = conn.execute("SELECT title, source_url, budget_paise, deadline_at, skill_tags FROM opportunities WHERE id=? AND owner_id=?", (opp_id, owner)).fetchone()
            if row is None:
                raise ProjectTaskError("Opportunity not found.")
            derived_title = title if title is not None else f"Task for {row['title'][:120]}"
            # Use the same create logic but need to avoid double transaction; instead call create with same token but we already in transaction
            # So we will directly insert here reusing logic
            derived_title = _input(_title, derived_title)
            description = _input(_description, description)
            priority = _input(_priority, priority)
            deadline = _input(_deadline, deadline_at if deadline_at != 0 else (row["deadline_at"] if row["deadline_at"] != 0 else 0))
            estimated = _input(_duration, estimated_duration_seconds)
            digest = _request("project_task_create", [derived_title, description, priority, deadline, estimated, opp_id, ""])
            existing = _retry(conn, owner, key, digest)
            if existing is not None:
                return existing
            if deadline != 0 and deadline <= now:
                raise ProjectTaskError("Deadline must be in the future.")
            ident = secrets.token_hex(16)
            conn.execute(
                "INSERT INTO project_tasks(id, owner_id, opportunity_id, parent_task_id, title, description, priority, deadline_at, estimated_duration_seconds, actual_duration_seconds, status, created_at, updated_at) VALUES(?,?,?,?,?,?,?,?,?,?, 'draft', ?, ?)",
                (ident, owner, opp_id, None, derived_title, description, priority, deadline, estimated, 0, now, now),
            )
            _audit(conn, now, owner, "project_task_created", ident)
            _remember(conn, owner, key, digest, ident)
            log_id = secrets.token_hex(16)
            conn.execute("INSERT INTO task_execution_logs(id, owner_id, task_id, message, created_at) VALUES(?,?,?,?,?)", (log_id, owner, ident, f"Task created from opportunity {opp_id}: {derived_title[:80]}", now))
            return ident

    def create_subtask(self, token: str, parent_task_id: str, title: str, *, operation_key: str, description: str = "", priority: int = 5, deadline_at: int = 0, estimated_duration_seconds: int = 0) -> str:
        parent = _input(_record_id, parent_task_id)
        return self.create(token, title, operation_key=operation_key, description=description, parent_task_id=parent, priority=priority, deadline_at=deadline_at, estimated_duration_seconds=estimated_duration_seconds)

    def get(self, token: str, record_id: str) -> dict:
        ident = _input(_record_id, record_id)
        with _authorized_transaction(self._auth, self._state, token) as (conn, owner, _now):
            _version(conn)
            row = _owned(conn, owner, ident)
            view = _view(row)
            # Add subtasks count and dependencies
            sub_cnt = conn.execute("SELECT count(*) FROM project_tasks WHERE owner_id=? AND parent_task_id=?", (owner, ident)).fetchone()[0]
            dep_cnt = conn.execute("SELECT count(*) FROM task_dependencies WHERE owner_id=? AND task_id=?", (owner, ident)).fetchone()[0]
            log_cnt = conn.execute("SELECT count(*) FROM task_execution_logs WHERE owner_id=? AND task_id=?", (owner, ident)).fetchone()[0]
            view["subtask_count"] = sub_cnt
            view["dependency_count"] = dep_cnt
            view["execution_log_count"] = log_cnt
            return view

    def list(self, token: str, *, after_id: str | None = None, limit: int = 20, status: str | None = None, priority: int | None = None, opportunity_id: str | None = None, parent_task_id: str | None = None) -> builtins.list[dict]:
        cursor = "" if after_id is None else _input(_record_id, after_id)
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ProjectTaskError("Invalid page size.")
        if status is not None and status not in _STATUSES:
            raise ProjectTaskError("Invalid status filter.")
        if priority is not None:
            _input(_priority, priority)
        if opportunity_id is not None:
            opportunity_id = _input(_record_id, opportunity_id)
        if parent_task_id is not None:
            parent_task_id = _input(_record_id, parent_task_id)
        with _authorized_transaction(self._auth, self._state, token) as (conn, owner, _now):
            _version(conn)
            query = "SELECT id FROM project_tasks WHERE owner_id=? AND id>? COLLATE BINARY"
            params: list = [owner, cursor]
            if status is not None:
                query += " AND status=?"
                params.append(status)
            if priority is not None:
                query += " AND priority=?"
                params.append(priority)
            if opportunity_id is not None:
                query += " AND opportunity_id=?"
                params.append(opportunity_id)
            if parent_task_id is not None:
                query += " AND parent_task_id=?"
                params.append(parent_task_id)
            query += " ORDER BY id COLLATE BINARY LIMIT ?"
            params.append(limit)
            rows = conn.execute(query, tuple(params)).fetchall()
            return [_view(_owned(conn, owner, r["id"])) for r in rows]

    def _transition(self, token: str, record_id: str, target_status: str, event: str, operation_key: str) -> str:
        ident = _input(_record_id, record_id)
        key = _input(_key, operation_key)
        if target_status not in _STATUSES:
            raise ProjectTaskError("Invalid status.")
        digest = _request("project_task_transition", [ident, target_status])
        with _authorized_transaction(self._auth, self._state, token) as (conn, owner, now):
            _version(conn)
            row = _owned(conn, owner, ident)
            existing = _retry(conn, owner, key, digest)
            if existing is not None:
                if existing != ident:
                    raise StateStorageError("Stored operation result is inconsistent.")
                return ident
            current = row["status"]
            expected = _TRANSITIONS.get((current, event))
            if expected != target_status:
                raise ProjectTaskError("Transition is not allowed.")
            # Dependency check: cannot start if dependencies not completed
            if event in ("project_task_started", "project_task_scheduled") and target_status in ("in_progress", "scheduled"):
                # Check dependencies
                deps = conn.execute("SELECT depends_on_task_id FROM task_dependencies WHERE owner_id=? AND task_id=?", (owner, ident)).fetchall()
                for (dep_id,) in deps:
                    dep_row = conn.execute("SELECT status FROM project_tasks WHERE id=? AND owner_id=?", (dep_id, owner)).fetchone()
                    if dep_row is None or dep_row["status"] != "completed":
                        raise ProjectTaskError("Cannot start: dependencies not completed.")
            conn.execute("UPDATE project_tasks SET status=?, updated_at=? WHERE id=? AND owner_id=?", (target_status, now, ident, owner))
            _audit(conn, now, owner, event, ident)
            _remember(conn, owner, key, digest, ident)
            # Log transition
            log_id = secrets.token_hex(16)
            conn.execute("INSERT INTO task_execution_logs(id, owner_id, task_id, message, created_at) VALUES(?,?,?,?,?)", (log_id, owner, ident, f"Status {current} -> {target_status} via {event}", now))
            return ident

    def submit_for_review(self, token: str, record_id: str, *, operation_key: str) -> str:
        return self._transition(token, record_id, "pending_review", "project_task_submitted", operation_key)

    def approve(self, token: str, record_id: str, *, operation_key: str) -> str:
        return self._transition(token, record_id, "approved", "project_task_approved", operation_key)

    def reject(self, token: str, record_id: str, *, operation_key: str) -> str:
        return self._transition(token, record_id, "rejected", "project_task_rejected", operation_key)

    def start(self, token: str, record_id: str, *, operation_key: str) -> str:
        # Try direct draft->in_progress or scheduled->in_progress or approved->in_progress
        # Determine current and use appropriate event
        ident = _input(_record_id, record_id)
        key = _input(_key, operation_key)
        with _authorized_transaction(self._auth, self._state, token) as (conn, owner, now):
            _version(conn)
            row = _owned(conn, owner, ident)
            current = row["status"]
            # Choose event based on current
            if current == "draft":
                event, target = "project_task_started", "in_progress"
            elif current == "scheduled":
                event, target = "project_task_started", "in_progress"
            elif current == "approved":
                event, target = "project_task_started", "in_progress"
            else:
                raise ProjectTaskError("Transition is not allowed.")
            digest = _request("project_task_transition", [ident, target])
            existing = _retry(conn, owner, key, digest)
            if existing is not None:
                return existing
            expected = _TRANSITIONS.get((current, event))
            if expected != target:
                raise ProjectTaskError("Transition is not allowed.")
            # Dependency check
            deps = conn.execute("SELECT depends_on_task_id FROM task_dependencies WHERE owner_id=? AND task_id=?", (owner, ident)).fetchall()
            for (dep_id,) in deps:
                dep_row = conn.execute("SELECT status FROM project_tasks WHERE id=? AND owner_id=?", (dep_id, owner)).fetchone()
                if dep_row is None or dep_row["status"] != "completed":
                    raise ProjectTaskError("Cannot start: dependencies not completed.")
            conn.execute("UPDATE project_tasks SET status=?, updated_at=? WHERE id=? AND owner_id=?", (target, now, ident, owner))
            _audit(conn, now, owner, event, ident)
            _remember(conn, owner, key, digest, ident)
            log_id = secrets.token_hex(16)
            conn.execute("INSERT INTO task_execution_logs(id, owner_id, task_id, message, created_at) VALUES(?,?,?,?,?)", (log_id, owner, ident, f"Status {current} -> {target} via {event}", now))
            return ident

    def complete(self, token: str, record_id: str, *, operation_key: str) -> str:
        # complete from in_progress or approved
        ident = _input(_record_id, record_id)
        key = _input(_key, operation_key)
        with _authorized_transaction(self._auth, self._state, token) as (conn, owner, now):
            _version(conn)
            row = _owned(conn, owner, ident)
            current = row["status"]
            if current == "in_progress":
                event, target = "project_task_completed", "completed"
            elif current == "approved":
                event, target = "project_task_completed", "completed"
            else:
                raise ProjectTaskError("Transition is not allowed.")
            digest = _request("project_task_transition", [ident, target])
            existing = _retry(conn, owner, key, digest)
            if existing is not None:
                return existing
            expected = _TRANSITIONS.get((current, event))
            if expected != target:
                raise ProjectTaskError("Transition is not allowed.")
            conn.execute("UPDATE project_tasks SET status=?, updated_at=? WHERE id=? AND owner_id=?", (target, now, ident, owner))
            _audit(conn, now, owner, event, ident)
            _remember(conn, owner, key, digest, ident)
            log_id = secrets.token_hex(16)
            conn.execute("INSERT INTO task_execution_logs(id, owner_id, task_id, message, created_at) VALUES(?,?,?,?,?)", (log_id, owner, ident, f"Status {current} -> {target} via {event}", now))
            return ident

    def fail(self, token: str, record_id: str, *, operation_key: str) -> str:
        ident = _input(_record_id, record_id)
        key = _input(_key, operation_key)
        with _authorized_transaction(self._auth, self._state, token) as (conn, owner, now):
            _version(conn)
            row = _owned(conn, owner, ident)
            current = row["status"]
            if current not in ("scheduled", "in_progress"):
                raise ProjectTaskError("Transition is not allowed.")
            event, target = "project_task_failed", "failed"
            digest = _request("project_task_transition", [ident, target])
            existing = _retry(conn, owner, key, digest)
            if existing is not None:
                return existing
            expected = _TRANSITIONS.get((current, event))
            if expected != target:
                raise ProjectTaskError("Transition is not allowed.")
            conn.execute("UPDATE project_tasks SET status=?, updated_at=? WHERE id=? AND owner_id=?", (target, now, ident, owner))
            _audit(conn, now, owner, event, ident)
            _remember(conn, owner, key, digest, ident)
            log_id = secrets.token_hex(16)
            conn.execute("INSERT INTO task_execution_logs(id, owner_id, task_id, message, created_at) VALUES(?,?,?,?,?)", (log_id, owner, ident, f"Status {current} -> {target} via {event}", now))
            return ident

    def cancel(self, token: str, record_id: str, *, operation_key: str) -> str:
        ident = _input(_record_id, record_id)
        key = _input(_key, operation_key)
        digest = _request("project_task_transition", [ident, "cancelled"])
        with _authorized_transaction(self._auth, self._state, token) as (conn, owner, now):
            _version(conn)
            row = _owned(conn, owner, ident)
            existing = _retry(conn, owner, key, digest)
            if existing is not None:
                return existing
            current = row["status"]
            if current in ("completed", "cancelled"):
                raise ProjectTaskError("Transition is not allowed.")
            # Generic cancel event mapping
            # Need to map current to correct event key
            event_map = {
                "draft": "project_task_cancelled",
                "pending_review": "project_task_cancelled",
                "approved": "project_task_cancelled",
                "scheduled": "project_task_cancelled",
                "in_progress": "project_task_cancelled",
                "failed": "project_task_cancelled",
                "rejected": "project_task_cancelled",
            }
            event = event_map.get(current)
            if event is None or _TRANSITIONS.get((current, event)) != "cancelled":
                raise ProjectTaskError("Transition is not allowed.")
            conn.execute("UPDATE project_tasks SET status='cancelled', updated_at=? WHERE id=? AND owner_id=?", (now, ident, owner))
            _audit(conn, now, owner, event, ident)
            _remember(conn, owner, key, digest, ident)
            log_id = secrets.token_hex(16)
            conn.execute("INSERT INTO task_execution_logs(id, owner_id, task_id, message, created_at) VALUES(?,?,?,?,?)", (log_id, owner, ident, f"Status {current} -> cancelled via {event}", now))
            return ident

    def retry(self, token: str, record_id: str, *, operation_key: str) -> str:
        return self._transition(token, record_id, "in_progress", "project_task_retried", operation_key)

    def retry_scheduled(self, token: str, record_id: str, *, operation_key: str) -> str:
        return self._transition(token, record_id, "scheduled", "project_task_retry_scheduled", operation_key)

    def rework(self, token: str, record_id: str, *, operation_key: str) -> str:
        return self._transition(token, record_id, "draft", "project_task_reworked", operation_key)

    def set_estimated_duration(self, token: str, record_id: str, seconds: int, *, operation_key: str) -> str:
        ident = _input(_record_id, record_id)
        secs = _input(_duration, seconds)
        key = _input(_key, operation_key)
        digest = _request("project_task_set_estimated", [ident, secs])
        with _authorized_transaction(self._auth, self._state, token) as (conn, owner, now):
            _version(conn)
            _owned(conn, owner, ident)
            # idempotency via distinct resource type for timing
            row = conn.execute("SELECT request_hash, resource_type, resource_id FROM state_operations WHERE owner_id=? AND operation_key=?", (owner, key)).fetchone()
            if row is not None:
                if row["resource_type"] == "project_task_timing" and hmac.compare_digest(row["request_hash"], digest):
                    return ident
                raise ProjectTaskError("Operation key was already used for a different request.")
            conn.execute("UPDATE project_tasks SET estimated_duration_seconds=?, updated_at=? WHERE id=? AND owner_id=?", (secs, now, ident, owner))
            conn.execute("INSERT INTO state_audit(occurred_at, actor_id, event, resource_type, resource_id) VALUES(?,?,?,?,?)", (now, owner, "project_task_estimated_updated", "project_task_timing", ident))
            conn.execute("INSERT INTO state_operations(owner_id, operation_key, request_hash, resource_type, resource_id) VALUES(?,?,?,?,?)", (owner, key, digest, "project_task_timing", ident))
            log_id = secrets.token_hex(16)
            conn.execute("INSERT INTO task_execution_logs(id, owner_id, task_id, message, created_at) VALUES(?,?,?,?,?)", (log_id, owner, ident, f"Estimated duration set to {secs}s", now))
            return ident

    def record_actual_duration(self, token: str, record_id: str, seconds: int, *, operation_key: str) -> str:
        ident = _input(_record_id, record_id)
        secs = _input(_duration, seconds)
        key = _input(_key, operation_key)
        digest = _request("project_task_set_actual", [ident, secs])
        with _authorized_transaction(self._auth, self._state, token) as (conn, owner, now):
            _version(conn)
            _owned(conn, owner, ident)
            row = conn.execute("SELECT request_hash, resource_type, resource_id FROM state_operations WHERE owner_id=? AND operation_key=?", (owner, key)).fetchone()
            if row is not None:
                if row["resource_type"] == "project_task_timing" and hmac.compare_digest(row["request_hash"], digest):
                    return ident
                raise ProjectTaskError("Operation key was already used for a different request.")
            conn.execute("UPDATE project_tasks SET actual_duration_seconds=?, updated_at=? WHERE id=? AND owner_id=?", (secs, now, ident, owner))
            conn.execute("INSERT INTO state_audit(occurred_at, actor_id, event, resource_type, resource_id) VALUES(?,?,?,?,?)", (now, owner, "project_task_actual_updated", "project_task_timing", ident))
            conn.execute("INSERT INTO state_operations(owner_id, operation_key, request_hash, resource_type, resource_id) VALUES(?,?,?,?,?)", (owner, key, digest, "project_task_timing", ident))
            log_id = secrets.token_hex(16)
            conn.execute("INSERT INTO task_execution_logs(id, owner_id, task_id, message, created_at) VALUES(?,?,?,?,?)", (log_id, owner, ident, f"Actual duration recorded {secs}s", now))
            return ident

    def add_dependency(self, token: str, task_id: str, depends_on_task_id: str, *, operation_key: str) -> str:
        tid = _input(_record_id, task_id)
        dep = _input(_record_id, depends_on_task_id)
        key = _input(_key, operation_key)
        digest = _request("task_dependency_add", [tid, dep])
        with _authorized_transaction(self._auth, self._state, token) as (conn, owner, now):
            _version(conn)
            # Verify both tasks exist and owned
            if conn.execute("SELECT 1 FROM project_tasks WHERE id=? AND owner_id=?", (tid, owner)).fetchone() is None:
                raise ProjectTaskError("Task not found.")
            if conn.execute("SELECT 1 FROM project_tasks WHERE id=? AND owner_id=?", (dep, owner)).fetchone() is None:
                raise ProjectTaskError("Dependency task not found.")
            # Check existing
            existing = conn.execute("SELECT id FROM task_dependencies WHERE owner_id=? AND task_id=? AND depends_on_task_id=?", (owner, tid, dep)).fetchone()
            if existing is not None:
                # Check operation key idempotency
                row = conn.execute("SELECT request_hash, resource_type, resource_id FROM state_operations WHERE owner_id=? AND operation_key=?", (owner, key)).fetchone()
                if row is not None and row["resource_type"] == "task_dependency" and hmac.compare_digest(row["request_hash"], digest):
                    return existing["id"]
                raise ProjectTaskError("Dependency already exists.")
            # Check operation key reuse
            row = conn.execute("SELECT request_hash, resource_type FROM state_operations WHERE owner_id=? AND operation_key=?", (owner, key)).fetchone()
            if row is not None:
                raise ProjectTaskError("Operation key was already used for a different request.")
            _check_cycle(conn, owner, tid, dep)
            ident = secrets.token_hex(16)
            conn.execute("INSERT INTO task_dependencies(id, owner_id, task_id, depends_on_task_id, created_at) VALUES(?,?,?,?,?)", (ident, owner, tid, dep, now))
            # Audit as dependency creation (not counted in project_task audit, but separate)
            conn.execute("INSERT INTO state_audit(occurred_at, actor_id, event, resource_type, resource_id) VALUES(?,?,?,?,?)", (now, owner, "task_dependency_created", "task_dependency", ident))
            conn.execute("INSERT INTO state_operations(owner_id, operation_key, request_hash, resource_type, resource_id) VALUES(?,?,?,?,?)", (owner, key, digest, "task_dependency", ident))
            # Log
            log_id = secrets.token_hex(16)
            conn.execute("INSERT INTO task_execution_logs(id, owner_id, task_id, message, created_at) VALUES(?,?,?,?,?)", (log_id, owner, tid, f"Dependency added: depends on {dep[:8]}", now))
            return ident

    def remove_dependency(self, token: str, dependency_id: str, *, operation_key: str) -> str:
        did = _input(_record_id, dependency_id)
        key = _input(_key, operation_key)
        digest = _request("task_dependency_remove", [did])
        with _authorized_transaction(self._auth, self._state, token) as (conn, owner, now):
            _version(conn)
            row = conn.execute("SELECT task_id FROM task_dependencies WHERE id=? AND owner_id=?", (did, owner)).fetchone()
            if row is None:
                raise AccessDenied("Access denied.")
            # Idempotency: check if already deleted via state_operations? For simplicity, allow idempotent delete via same key
            op = conn.execute("SELECT request_hash, resource_type, resource_id FROM state_operations WHERE owner_id=? AND operation_key=?", (owner, key)).fetchone()
            if op is not None:
                if op["resource_type"] != "task_dependency" or not hmac.compare_digest(op["request_hash"], digest):
                    raise ProjectTaskError("Operation key was already used for a different request.")
                return did
            conn.execute("DELETE FROM task_dependencies WHERE id=? AND owner_id=?", (did, owner))
            conn.execute("INSERT INTO state_audit(occurred_at, actor_id, event, resource_type, resource_id) VALUES(?,?,?,?,?)", (now, owner, "task_dependency_removed", "task_dependency", did))
            conn.execute("INSERT INTO state_operations(owner_id, operation_key, request_hash, resource_type, resource_id) VALUES(?,?,?,?,?)", (owner, key, digest, "task_dependency", did))
            log_id = secrets.token_hex(16)
            conn.execute("INSERT INTO task_execution_logs(id, owner_id, task_id, message, created_at) VALUES(?,?,?,?,?)", (log_id, owner, row["task_id"], f"Dependency removed {did[:8]}", now))
            return did

    def list_dependencies(self, token: str, task_id: str) -> builtins.list[dict]:
        tid = _input(_record_id, task_id)
        with _authorized_transaction(self._auth, self._state, token) as (conn, owner, _now):
            _version(conn)
            if conn.execute("SELECT 1 FROM project_tasks WHERE id=? AND owner_id=?", (tid, owner)).fetchone() is None:
                raise AccessDenied("Access denied.")
            rows = conn.execute("SELECT * FROM task_dependencies WHERE owner_id=? AND task_id=? ORDER BY created_at", (owner, tid)).fetchall()
            return [dict(r) for r in rows]

    def log_execution(self, token: str, task_id: str, message: str, *, operation_key: str) -> str:
        tid = _input(_record_id, task_id)
        msg = _input(_log_message, message)
        key = _input(_key, operation_key)
        digest = _request("task_log", [tid, msg])
        with _authorized_transaction(self._auth, self._state, token) as (conn, owner, now):
            _version(conn)
            if conn.execute("SELECT 1 FROM project_tasks WHERE id=? AND owner_id=?", (tid, owner)).fetchone() is None:
                raise AccessDenied("Access denied.")
            existing = conn.execute("SELECT resource_id FROM state_operations WHERE owner_id=? AND operation_key=? AND resource_type='task_execution_log'", (owner, key)).fetchone()
            if existing is not None:
                # Check hash
                row = conn.execute("SELECT request_hash FROM state_operations WHERE owner_id=? AND operation_key=?", (owner, key)).fetchone()
                if row is not None and hmac.compare_digest(row["request_hash"], digest):
                    return existing["resource_id"]
                raise ProjectTaskError("Operation key was already used for a different request.")
            ident = secrets.token_hex(16)
            conn.execute("INSERT INTO task_execution_logs(id, owner_id, task_id, message, created_at) VALUES(?,?,?,?,?)", (ident, owner, tid, msg, now))
            conn.execute("INSERT INTO state_audit(occurred_at, actor_id, event, resource_type, resource_id) VALUES(?,?,?,?,?)", (now, owner, "task_execution_logged", "task_execution_log", ident))
            conn.execute("INSERT INTO state_operations(owner_id, operation_key, request_hash, resource_type, resource_id) VALUES(?,?,?,?,?)", (owner, key, digest, "task_execution_log", ident))
            return ident

    def list_logs(self, token: str, task_id: str, *, limit: int = 50) -> builtins.list[dict]:
        tid = _input(_record_id, task_id)
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ProjectTaskError("Invalid page size.")
        with _authorized_transaction(self._auth, self._state, token) as (conn, owner, _now):
            _version(conn)
            if conn.execute("SELECT 1 FROM project_tasks WHERE id=? AND owner_id=?", (tid, owner)).fetchone() is None:
                raise AccessDenied("Access denied.")
            rows = conn.execute("SELECT * FROM task_execution_logs WHERE owner_id=? AND task_id=? ORDER BY created_at, id LIMIT ?", (owner, tid, limit)).fetchall()
            return [dict(r) for r in rows]

    def schedule(self, token: str, task_id: str, deadline_at: int, *, operation_key: str) -> str:
        # Schedule is a status transition to scheduled with deadline update if needed
        tid = _input(_record_id, task_id)
        dl = _input(_deadline, deadline_at)
        key = _input(_key, operation_key)
        digest = _request("project_task_schedule", [tid, dl])
        with _authorized_transaction(self._auth, self._state, token) as (conn, owner, now):
            _version(conn)
            row = _owned(conn, owner, tid)
            existing = _retry(conn, owner, key, digest)
            if existing is not None:
                return existing
            if dl != 0 and dl <= now:
                raise ProjectTaskError("Deadline must be in the future.")
            # Update deadline and status if needed
            # Only allow schedule from draft/approved
            current = row["status"]
            if current not in ("draft", "approved"):
                raise ProjectTaskError("Transition is not allowed.")
            conn.execute("UPDATE project_tasks SET deadline_at=?, status='scheduled', updated_at=? WHERE id=? AND owner_id=?", (dl if dl != 0 else row["deadline_at"], now, tid, owner))
            # Need to create audit for both deadline update and status - we will audit status change
            _audit(conn, now, owner, "project_task_scheduled", tid)
            _remember(conn, owner, key, digest, tid)
            log_id = secrets.token_hex(16)
            conn.execute("INSERT INTO task_execution_logs(id, owner_id, task_id, message, created_at) VALUES(?,?,?,?,?)", (log_id, owner, tid, f"Scheduled with deadline {dl}", now))
            return tid


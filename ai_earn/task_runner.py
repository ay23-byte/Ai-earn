"""Experimental durable managed-task runner — lease-fenced, audited, thread-pool.

This runner is a thin, owner-scoped wrapper around the v12+ managed pair
primitives. It does not execute external effects, does not prove human review,
and does not fence external systems — lease fencing is via the existing
`submit_pair`/`retry_pair` checks (`current_lease_id` + `lease_until`).

Use only disposable data with a trusted fixed auth/state pair. No HTTP API,
no production deployment, no payment handling.
"""
from __future__ import annotations

import concurrent.futures
import threading
from typing import Callable

from .auth import AuthService
from .state_storage import StateStorage, StateStorageError
from .task_associations import TaskAssociations, AssociationError
from .task_queue import QueueError
from .events import Event, write_event
import sys

# Allowlist extensions for runner — keep minimal, no secrets in logs
# Add new events to the central allowlist in events.py? For now reuse existing
# INTERNAL_ERROR for failures and expose runner via explicit methods.
# If events.py is extended, these will be allowlisted there.


class RunnerError(Exception):
    """Runner configuration or execution error."""


def _limited_output(value: str) -> str:
    if not isinstance(value, str):
        raise RunnerError("Handler must return a string.")
    # Workflow output is bounded 1..4096 UTF-8 bytes, no control chars is
    # validated by task_workflows; we pre-validate length here for early error.
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise RunnerError("Invalid output encoding.") from exc
    if not 1 <= len(encoded) <= 4096:
        raise RunnerError("Output must be 1..4096 UTF-8 bytes.")
    if any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise RunnerError("Output contains control characters.")
    return value


class ManagedTaskRunner:
    """Lease-fenced runner for managed pairs (v12+).

    The runner owns a ThreadPoolExecutor and runs a poll loop in a background
    thread. Each iteration atomically claims the highest-priority ready managed
    pair (`claim_next_managed`), executes the caller-provided handler, and
    lease-fence submits the output (`submit_pair`). On handler failure it
    lease-fence retries (`retry_pair`) to respect backoff; if lease already
    expired it leaves the record for explicit `recover_pair` (not auto-retried).

    The handler signature is ``handler(context: dict) -> str`` where context
    contains ``association_id``, ``queue_id``, ``workflow_id``, ``lease_id``,
    ``title`` etc. Handler must be pure and bounded; never perform external
    effects inside handler without separate fencing.
    """

    def __init__(self, auth: AuthService, state: StateStorage, *, max_workers: int = 4):
        if type(max_workers) is not int or not 1 <= max_workers <= 16:
            raise RunnerError("max_workers must be 1..16")
        self._auth = auth
        self._state = state
        self._associations = TaskAssociations(auth, state)
        self._executor = concurrent.futures.ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="managed-runner")
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._running = False

    def _claim_and_execute_once(
        self,
        token: str,
        handler: Callable[[dict], str],
        *,
        lease_seconds: int = 60,
        operation_key: str,
    ) -> dict | None:
        """Claim next ready managed pair and execute handler synchronously.

        Returns ``{"association_id":..., "lease_id":..., "submitted": bool}`` or
        ``None`` if no ready work. Raises ``RunnerError`` for handler failures
        after attempting lease-fenced retry. Lease expiry during handler or
        before submit is treated as a fence failure and triggers retry if
        possible, otherwise bubbles as ``AssociationError``.

        This method is the durable core — it holds no state beyond the single
        authorized transaction per operation, so a crash before commit rolls
        back and a retry with the same operation_key acknowledges history.
        """
        if not callable(handler):
            raise RunnerError("Handler must be callable.")
        # claim_next_managed is atomic and validates version, ownership,
        # priority ordering, and lease fencing.
        claimed = self._associations.claim_next_managed(token, lease_seconds=lease_seconds, operation_key=operation_key)
        if claimed is None:
            return None
        association_id = claimed["association_id"]
        lease_id = claimed["lease_id"]

        # Build context for handler — fetch current association view for titles etc.
        # Use associations.inspect for point-in-time observation (no mutation).
        try:
            observed = self._associations.inspect(token, association_id)
        except Exception:
            # If inspect fails (e.g., lease expired between claim and inspect),
            # we still attempt handler with minimal context; handler may fail and
            # we will retry.
            observed = {"association_id": association_id, "lease_id": lease_id}

        ctx = {
            "association_id": association_id,
            "lease_id": lease_id,
            "queue": observed.get("queue", {}),
            "workflow": observed.get("workflow", {}),
        }

        # Execute handler in current thread (caller may wrap this in executor)
        try:
            output = handler(ctx)
            output = _limited_output(output)
        except Exception as exc:
            # Handler failure → attempt lease-fenced retry (backoff) if lease still valid.
            # If retry fails due to lease expiry or other state, propagate.
            try:
                self._associations.retry_pair(token, association_id, lease_id, operation_key=operation_key + "_retry")
            except (AssociationError, QueueError, StateStorageError, RunnerError):
                # If retry itself fails (e.g., lease expired, already retried with same key),
                # we surface the original handler error as RunnerError with context.
                raise RunnerError(f"Handler failed and retry could not be applied: {exc}") from exc
            raise RunnerError("Handler failed; task requeued via retry_pair.") from exc

        # Submit output while lease is still current — submit_pair rechecks lease_until vs now
        try:
            submit_key = operation_key + "_submit"
            # Ensure submit key is within 1..128 and distinct from claim key
            if len(submit_key) > 128:
                submit_key = submit_key[:128]
            self._associations.submit_pair(token, association_id, lease_id, output, operation_key=submit_key)
            return {"association_id": association_id, "lease_id": lease_id, "submitted": True}
        except (AssociationError, QueueError, StateStorageError) as exc:
            # Lease fence failure (expired, queue state changed) — try retry if handler succeeded but lease expired before commit
            # We do not automatically recover after expiry; caller may use recover_pair explicitly.
            raise RunnerError(f"Submit failed (lease fence): {exc}") from exc

    def run_once(
        self,
        token: str,
        handler: Callable[[dict], str],
        *,
        lease_seconds: int = 60,
        operation_key: str,
    ) -> dict | None:
        """Synchronous single iteration — claim, handle, submit."""
        return self._claim_and_execute_once(token, handler, lease_seconds=lease_seconds, operation_key=operation_key)

    def submit_background(
        self,
        token: str,
        handler: Callable[[dict], str],
        *,
        lease_seconds: int = 60,
        operation_key: str,
    ) -> concurrent.futures.Future:
        """Submit one claim-execute-submit iteration to the thread pool.

        Returns a Future that resolves to the same dict as `run_once` or
        raises RunnerError. The token and handler are captured by the future;
        ensure token remains valid for the future's execution window (30-min
        session). Caller must handle operation_key uniqueness.
        """
        if self._executor is None:
            raise RunnerError("Runner has been shut down.")
        return self._executor.submit(self._claim_and_execute_once, token, handler, lease_seconds=lease_seconds, operation_key=operation_key)

    def start_polling(
        self,
        token_provider: Callable[[], str],
        handler: Callable[[dict], str],
        *,
        lease_seconds: int = 60,
        poll_interval: float = 1.0,
        operation_key_prefix: str = "runner_poll",
    ) -> None:
        """Start background polling loop in a dedicated thread.

        `token_provider` is called each iteration to obtain a fresh valid token
        (to handle 30-min expiry). The loop claims and executes tasks until
        `stop()` is called. Each iteration uses a distinct operation_key
        ``f"{prefix}_{counter}"``.

        This is best-effort polling — it does not guarantee exactly-once
        execution and does not fence external effects.
        """
        if not callable(token_provider) or not callable(handler):
            raise RunnerError("token_provider and handler must be callable.")
        if type(poll_interval) is not int and type(poll_interval) is not float:
            raise RunnerError("Invalid poll_interval.")
        if not 0.1 <= poll_interval <= 60:
            raise RunnerError("poll_interval must be 0.1..60")
        if not isinstance(operation_key_prefix, str) or not 1 <= len(operation_key_prefix) <= 80:
            raise RunnerError("Invalid operation_key_prefix.")
        with self._lock:
            if self._running:
                raise RunnerError("Runner already polling.")
            self._stop_event.clear()
            self._running = True

        def _loop():
            counter = 0
            while not self._stop_event.is_set():
                try:
                    token = token_provider()
                    op_key = f"{operation_key_prefix}_{counter}"
                    counter += 1
                    # Use synchronous iteration inside polling thread to preserve ordering
                    result = self._claim_and_execute_once(token, handler, lease_seconds=lease_seconds, operation_key=op_key)
                    if result is None:
                        # No ready work — back off
                        self._stop_event.wait(poll_interval)
                    else:
                        # Successful work — small delay to avoid tight loop
                        self._stop_event.wait(0.1)
                except RunnerError:
                    # Log via allowlisted event (stderr) without leaking secrets
                    try:
                        write_event(Event.INTERNAL_ERROR, sys.stderr)
                    except Exception:
                        pass
                    # Back off on error
                    self._stop_event.wait(poll_interval)
                except Exception:
                    # Unexpected — log and back off, but keep polling until stopped
                    try:
                        write_event(Event.INTERNAL_ERROR, sys.stderr)
                    except Exception:
                        pass
                    self._stop_event.wait(poll_interval)
            with self._lock:
                self._running = False

        self._thread = threading.Thread(target=_loop, name="managed-runner-poll", daemon=True)
        self._thread.start()

    def stop(self, *, wait: bool = True, timeout: float | None = None) -> None:
        """Signal polling loop to stop and optionally wait."""
        self._stop_event.set()
        if wait and self._thread is not None:
            self._thread.join(timeout=timeout)

    def shutdown(self, *, wait: bool = True) -> None:
        """Stop polling and shutdown the executor."""
        self.stop(wait=wait)
        self._executor.shutdown(wait=wait, cancel_futures=False)
        # Prevent further submit_background
        # Keep executor reference for error messaging; don't set to None
        with self._lock:
            self._running = False

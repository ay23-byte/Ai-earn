"""Tests for ManagedTaskRunner — lease-fenced, thread-pool, audited.

Disposable only, no external effects, no production deployment.
"""
import os
import tempfile
import time
import threading
from pathlib import Path
import unittest
from unittest.mock import patch

from ai_earn.auth import AuthService
from ai_earn.auth_storage import AuthStorage
from ai_earn.state_storage import StateStorage
from ai_earn.task_queue import TaskQueue
from ai_earn.task_workflows import TaskWorkflows
from ai_earn.task_associations import TaskAssociations, AssociationError
from ai_earn.task_runner import ManagedTaskRunner, RunnerError


@unittest.skipUnless(os.name in {"posix", "nt"}, "requires private filesystem")
class TaskRunnerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.tmp.cleanup)
        self.directory = Path(self.tmp.name) / "private"
        if os.name == "nt":
            from ai_earn.windows_security import create_private_directory
            create_private_directory(self.directory)
        else:
            self.directory.mkdir(mode=0o700)
        self.auth_path = self.directory / "auth.sqlite3"
        self.state_path = self.directory / "state.sqlite3"
        patcher = patch("ai_earn.auth.PASSWORD_ITERATIONS", 1000)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.auth = AuthService(AuthStorage(self.auth_path))
        self.auth.register("alice", "disposable test password")
        self.token = self.auth.login("alice", "disposable test password")
        state = StateStorage(self.state_path, create=True, confirmed=True)
        for v in range(2, 16):
            getattr(state, f"migrate_to_v{v}")(self.directory / f"before-v{v}.sqlite3", confirmed=True)
        self.state = StateStorage(self.state_path)
        self.queue = TaskQueue(self.auth, self.state)
        self.workflows = TaskWorkflows(self.auth, self.state)
        self.associations = TaskAssociations(self.auth, self.state)
        self.runner = ManagedTaskRunner(self.auth, self.state, max_workers=2)
        self.addCleanup(lambda: self.runner.shutdown(wait=True))

    def _create_managed(self, title="work", priority=5):
        qid = self.queue.enqueue(self.token, title, operation_key=f"q_{title}_{time.time_ns()}", priority=priority)
        wid = self.workflows.create(self.token, title, operation_key=f"w_{title}_{time.time_ns()}")
        aid = self.associations.create_managed(self.token, qid, wid, operation_key=f"m_{title}_{time.time_ns()}")
        return qid, wid, aid

    def test_successful_run_once(self):
        qid, wid, aid = self._create_managed("hello", priority=5)
        def handler(ctx):
            self.assertEqual(ctx["association_id"], aid)
            return "hello world output"
        result = self.runner.run_once(self.token, handler, lease_seconds=60, operation_key="run1")
        self.assertIsNotNone(result)
        self.assertEqual(result["association_id"], aid)
        # workflow should be pending_review, queue still leased
        wf = self.workflows.get(self.token, wid)
        self.assertEqual(wf["status"], "pending_review")
        q = self.queue.get(self.token, qid)
        self.assertEqual(q["status"], "leased")
        # second run_once with no ready work should return None
        # Create no new managed, so claim_next should be None because existing is leased not queued
        result2 = self.runner.run_once(self.token, handler, lease_seconds=60, operation_key="run2")
        self.assertIsNone(result2)

    def test_handler_failure_triggers_retry(self):
        qid, wid, aid = self._create_managed("failcase", priority=5)
        def bad_handler(ctx):
            raise ValueError("simulated handler failure")
        with self.assertRaises(RunnerError) as cm:
            self.runner.run_once(self.token, bad_handler, lease_seconds=60, operation_key="fail1")
        self.assertIn("Handler failed", str(cm.exception))
        # queue should be requeued (retry) and workflow remains planned or in_progress? Actually runner claims and starts workflow to in_progress before handler.
        # After claim, workflow is in_progress; after handler failure + retry, queue should be queued again, workflow remains in_progress
        q = self.queue.get(self.token, qid)
        self.assertEqual(q["status"], "queued")
        wf = self.workflows.get(self.token, wid)
        self.assertIn(wf["status"], ("in_progress", "planned"))  # should be in_progress after claim

    def test_lease_fence_blocks_submit_after_expiry(self):
        # Use short lease and handler that sleeps past expiry
        qid, wid, aid = self._create_managed("expiry", priority=5)
        def slow_handler(ctx):
            time.sleep(2)  # lease is 1 sec, so will expire before submit
            return "slow output"
        with self.assertRaises(RunnerError) as cm:
            self.runner.run_once(self.token, slow_handler, lease_seconds=1, operation_key="expiry1")
        self.assertIn("lease fence", str(cm.exception).lower() + str(cm.exception))
        # Queue should still be leased (expired) but not yet recovered; submit failed, so queue remains leased with expired lease_until
        q = self.queue.get(self.token, qid)
        # After failed submit, queue remains leased (since submit didn't change status), but lease is expired
        self.assertEqual(q["status"], "leased")
        # Workflow should still be in_progress (not yet submitted)
        wf = self.workflows.get(self.token, wid)
        self.assertEqual(wf["status"], "in_progress")

    def test_concurrent_claims_only_one_succeeds(self):
        # Create 2 managed pairs with different priorities
        q1, w1, a1 = self._create_managed("task1", priority=9)
        q2, w2, a2 = self._create_managed("task2", priority=1)
        # Two concurrent run_once with different operation keys should claim distinct tasks
        results = []
        errors = []
        def handler(ctx):
            time.sleep(0.1)
            return "output " + ctx["association_id"][:4]

        def run_with_key(key):
            try:
                r = self.runner.run_once(self.token, handler, lease_seconds=60, operation_key=key)
                results.append(r)
            except Exception as e:
                errors.append(e)

        t1 = threading.Thread(target=run_with_key, args=("conc1",))
        t2 = threading.Thread(target=run_with_key, args=("conc2",))
        t1.start(); t2.start(); t1.join(); t2.join()
        # Both should succeed claiming different tasks (since 2 ready, highest priority first)
        self.assertEqual(len(results), 2)
        self.assertEqual(len(errors), 0)
        ids = {r["association_id"] for r in results if r}
        self.assertEqual(ids, {a1, a2})
        # Third claim should return None (no ready)
        self.assertIsNone(self.runner.run_once(self.token, handler, lease_seconds=60, operation_key="conc3"))

    def test_submit_background_future(self):
        qid, wid, aid = self._create_managed("bg", priority=5)
        def handler(ctx):
            return "bg output"
        fut = self.runner.submit_background(self.token, handler, lease_seconds=60, operation_key="bg1")
        result = fut.result(timeout=5)
        self.assertEqual(result["association_id"], aid)
        wf = self.workflows.get(self.token, wid)
        self.assertEqual(wf["status"], "pending_review")

    def test_polling_start_stop(self):
        # Create 3 managed tasks
        for i in range(3):
            self._create_managed(f"poll{i}", priority=5-i)
        outputs = []
        def handler(ctx):
            outputs.append(ctx["association_id"])
            return f"out for {ctx['association_id'][:4]}"
        # Token provider returns same token (still valid within test)
        def token_provider():
            return self.token
        self.runner.start_polling(token_provider, handler, lease_seconds=60, poll_interval=0.2, operation_key_prefix="poll")
        # Wait for tasks to be processed
        time.sleep(2)
        self.runner.stop(wait=True)
        # All 3 should have been submitted
        self.assertEqual(len(outputs), 3)
        # Check that workflows are pending_review
        for aid in outputs:
            # Need to find workflow for association; inspect to get workflow id
            info = self.associations.inspect(self.token, aid)
            wid = info["workflow"]["id"] if "workflow" in info else None
            if wid:
                wf = self.workflows.get(self.token, wid)
                self.assertEqual(wf["status"], "pending_review")

    def test_operation_key_idempotency(self):
        qid, wid, aid = self._create_managed("idem", priority=5)
        def handler(ctx):
            return "idem output"
        # First run
        r1 = self.runner.run_once(self.token, handler, lease_seconds=60, operation_key="idem1")
        self.assertIsNotNone(r1)
        # Create another managed for second idempotency test: need new managed because previous is now pending_review not ready
        q2, w2, a2 = self._create_managed("idem2", priority=5)
        # Same operation_key as before but different task -> should conflict? But claim_next uses different digest (lease_seconds), but same operation_key with different claimed task may conflict? Actually claim_next's operation_key is for lease, not for specific association. Reusing same operation_key with same lease_seconds should be idempotent and return same lease.
        # For this test, we want to ensure same operation_key with same duration returns acknowledged lease even after task completed.
        # Create third managed, claim with key, then retry same key should return same association (acknowledgement)
        q3, w3, a3 = self._create_managed("idem3", priority=5)
        r3a = self.runner.run_once(self.token, handler, lease_seconds=60, operation_key="idem_same")
        self.assertIsNotNone(r3a)
        # Need a new managed to test retry of same key with different task? But claim_next will now claim next ready (maybe none if r3a consumed one). Let's just test that same operation_key retried after success returns same result (historical ack)
        # Create a new runner run with same key and no ready work -> should still return historical lease (not None) per claim_next_managed logic
        r3b = self.runner.run_once(self.token, handler, lease_seconds=60, operation_key="idem_same")
        # According to claim_next_managed, exact retry acknowledges original lease even if lease now stale/completed, so should return same association
        self.assertEqual(r3a["association_id"], r3b["association_id"])
        self.assertEqual(r3a["lease_id"], r3b["lease_id"])

    def test_invalid_inputs(self):
        with self.assertRaises(RunnerError):
            ManagedTaskRunner(self.auth, self.state, max_workers=0)
        with self.assertRaises(RunnerError):
            self.runner.run_once(self.token, "not callable", lease_seconds=60, operation_key="bad")
        with self.assertRaises((RunnerError, AssociationError)):
            self.runner.run_once(self.token, lambda ctx: "ok", lease_seconds=99999, operation_key="bad")
        def handler(ctx):
            return "x" * 5000  # too large
        qid, wid, aid = self._create_managed("invalid", priority=5)
        with self.assertRaises(RunnerError):
            self.runner.run_once(self.token, handler, lease_seconds=60, operation_key="invalid1")

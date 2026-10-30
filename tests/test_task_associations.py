"""Owned local associations, not task execution or external authorization."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, contextmanager
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

from ai_earn.auth import AccessDenied, AuthService, SESSION_SECONDS
from ai_earn.auth_storage import AuthStorage
from ai_earn.invoice_drafts import InvoiceDrafts
from ai_earn.order_drafts import OrderDrafts
from ai_earn.payment_observations import PaymentObservations
from ai_earn.state_storage import StateStorage, StateStorageError, _copy_database
from ai_earn.task_associations import TaskAssociations, AssociationError
from ai_earn.task_queue import TaskQueue
from ai_earn.task_workflows import TaskWorkflows, WorkflowError


class Clock:
    now = 1_800_000_000
    def __call__(self):
        return self.now


@unittest.skipUnless(os.name in {"posix", "nt"}, "requires a supported private filesystem")
class TaskAssociationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.directory = Path(self.tmp.name) / "private"
        if os.name == "nt":
            from ai_earn.windows_security import create_private_directory
            create_private_directory(self.directory)
        else:
            self.directory.mkdir(mode=0o700)
        patcher = patch("ai_earn.auth.PASSWORD_ITERATIONS", 1000)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.clock = Clock()
        self.auth = AuthService(AuthStorage(self.directory / "auth.sqlite3"), self.clock)
        for name in ("alice", "bobby"):
            self.auth.register(name, "disposable test password")
        self.alice = self.auth.login("alice", "disposable test password")
        self.bob = self.auth.login("bobby", "disposable test password")
        self.state = StateStorage(self.directory / "state.sqlite3", create=True, confirmed=True)
        for version in range(2, 10):
            getattr(self.state, f"migrate_to_v{version}")(self.directory / f"v{version-1}.sqlite3", confirmed=True)
        self.queue = TaskQueue(self.auth, self.state)
        self.workflows = TaskWorkflows(self.auth, self.state)
        self.item = self.queue.enqueue(self.alice, "Synthetic", operation_key="queue")
        self.workflow = self.workflows.create(self.alice, "Synthetic", operation_key="workflow")
        orders = OrderDrafts(self.auth, self.state)
        self.order = orders.create(self.alice, "Synthetic", 123, operation_key="order")
        invoice = InvoiceDrafts(self.auth, self.state).create(self.alice, self.order, operation_key="invoice")
        self.observation = PaymentObservations(self.auth, self.state).create(self.alice, invoice, operation_key="placeholder")
        self.state.migrate_to_v10(self.directory / "v9.sqlite3", confirmed=True)
        self.associations = TaskAssociations(self.auth, self.state)

    def create(self, key="associate"):
        return self.associations.create(self.alice, self.item, self.workflow, operation_key=key)

    def test_migration_preserves_previous_records_and_retries(self):
        with StateStorage(self.directory / "v9.sqlite3").transaction() as conn:
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 9)
            self.assertIsNone(conn.execute("SELECT name FROM sqlite_master WHERE name='task_associations'").fetchone())
        self.assertEqual(self.queue.enqueue(self.alice, "Synthetic", operation_key="queue"), self.item)
        self.assertEqual(self.workflows.create(self.alice, "Synthetic", operation_key="workflow"), self.workflow)
        self.assertEqual(PaymentObservations(self.auth, self.state).get(self.alice, self.observation)["status"], "unverified")
        with self.state.transaction() as conn:
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 10)

    def test_create_get_exact_retry_and_metadata_only_behavior(self):
        ident = self.create()
        self.assertEqual(self.create(), ident)
        row = self.associations.get(self.alice, ident)
        self.assertEqual((row["queue_id"], row["workflow_id"]), (self.item, self.workflow))
        self.assertEqual(set(row), {"id", "queue_id", "workflow_id", "created_at"})
        self.queue.cancel(self.alice, self.item, operation_key="cancel-queue")
        self.workflows.start(self.alice, self.workflow, operation_key="start")
        self.assertEqual(self.create(), ident)
        self.assertEqual(self.associations.get(self.alice, ident), row)
        self.assertEqual(self.queue.get(self.alice, self.item)["status"], "cancelled")
        self.assertEqual(self.workflows.get(self.alice, self.workflow)["status"], "in_progress")
        with self.state.transaction() as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM state_audit WHERE event='task_association_created'").fetchone()[0], 1)
            self.assertEqual(conn.execute("SELECT count(*) FROM state_operations WHERE operation_key='associate'").fetchone()[0], 1)

    def test_status_view_reports_separate_local_states_without_side_effects(self):
        ident = self.create()
        with self.state.transaction() as conn:
            counts = (conn.execute("SELECT count(*) FROM state_audit").fetchone()[0],
                      conn.execute("SELECT count(*) FROM state_operations").fetchone()[0])
        view = self.associations.inspect(self.alice, ident)
        self.assertEqual(view, {
            "association_id": ident, "queue_id": self.item, "workflow_id": self.workflow,
            "observed_at": self.clock.now, "queue_status": "queued", "workflow_status": "planned",
            "queue_ready_at_observation": True, "lease_active_at_observation": False,
            "deadline_passed_at_observation": False,
        })
        with self.state.transaction() as conn:
            self.assertEqual(counts, (conn.execute("SELECT count(*) FROM state_audit").fetchone()[0],
                                      conn.execute("SELECT count(*) FROM state_operations").fetchone()[0]))
        self.assertNotIn(self.alice, str(view))
        self.assertNotIn("Synthetic", str(view))

    def test_status_view_shows_independent_lifecycle_and_lease_expiry(self):
        ident = self.create()
        lease = self.queue.claim(self.alice, self.item, operation_key="claim", lease_seconds=60)
        self.workflows.start(self.alice, self.workflow, operation_key="start")
        self.workflows.submit(self.alice, self.workflow, "synthetic output", operation_key="submit")
        self.workflows.review(self.alice, self.workflow, "synthetic output", approved=True, confirmed=True, operation_key="review")
        view = self.associations.inspect(self.alice, ident)
        self.assertEqual((view["queue_status"], view["workflow_status"]), ("leased", "completed"))
        self.assertTrue(view["lease_active_at_observation"])
        self.assertFalse(view["queue_ready_at_observation"])
        self.clock.now += 60
        view = self.associations.inspect(self.alice, ident)
        self.assertEqual(view["queue_status"], "leased")  # No automatic recovery.
        self.assertFalse(view["lease_active_at_observation"])
        self.queue.recover(self.alice, self.item, lease, operation_key="recover")
        view = self.associations.inspect(self.alice, ident)
        self.assertEqual((view["queue_status"], view["workflow_status"]), ("queued", "completed"))
        self.assertFalse(view["queue_ready_at_observation"])  # Backoff.
        self.clock.now += 60
        self.assertTrue(self.associations.inspect(self.alice, ident)["queue_ready_at_observation"])

    def test_status_view_deadline_and_cancellation_are_observations_only(self):
        queued = self.queue.enqueue(self.alice, "Deadline", operation_key="deadline-q", deadline_at=self.clock.now + 5)
        workflow = self.workflows.create(self.alice, "Deadline", operation_key="deadline-w")
        ident = self.associations.create(self.alice, queued, workflow, operation_key="deadline-link")
        self.assertTrue(self.associations.inspect(self.alice, ident)["queue_ready_at_observation"])
        self.clock.now += 5
        view = self.associations.inspect(self.alice, ident)
        self.assertTrue(view["deadline_passed_at_observation"])
        self.assertFalse(view["queue_ready_at_observation"])
        self.queue.cancel(self.alice, queued, operation_key="deadline-cancel")
        self.assertEqual(self.associations.inspect(self.alice, ident)["queue_status"], "cancelled")
        self.assertEqual(self.associations.inspect(self.alice, ident)["workflow_status"], "planned")

    def test_status_view_denies_other_owners_missing_and_expired_sessions(self):
        ident = self.create()
        for token, record in ((self.bob, ident), (self.alice, "f"*32), ("", ident)):
            with self.assertRaises(AccessDenied):
                self.associations.inspect(token, record)
        with self.assertRaises(AssociationError):
            self.associations.inspect(self.alice, "bad")
        self.auth.logout(self.bob)
        with self.assertRaises(AccessDenied):
            self.associations.inspect(self.bob, ident)
        self.clock.now += SESSION_SECONDS
        with self.assertRaises(AccessDenied):
            self.associations.inspect(self.alice, ident)

    def test_status_view_refuses_corruption_and_survives_backup(self):
        ident = self.create()
        backup = self.directory / "view-backup.sqlite3"
        self.state.backup_to(backup, confirmed=True)
        self.assertEqual(TaskAssociations(self.auth, StateStorage(backup)).inspect(self.alice, ident),
                         self.associations.inspect(self.alice, ident))
        with self.state.transaction() as conn:
            conn.execute("UPDATE task_queue SET attempts=1 WHERE id=?", (self.item,))
        with self.assertRaises(StateStorageError):
            self.associations.inspect(self.alice, ident)
        with self.assertRaises(AccessDenied):
            self.associations.inspect(self.bob, ident)

    def test_status_view_refuses_session_expired_during_read(self):
        import ai_earn.task_associations as module
        ident = self.create()
        original = module._owned_workflow
        def expire(*args):
            result = original(*args)
            self.clock.now += SESSION_SECONDS
            return result
        with patch.object(module, "_owned_workflow", side_effect=expire):
            with self.assertRaises(AccessDenied):
                self.associations.inspect(self.alice, ident)
        with self.state.transaction() as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM task_associations WHERE id=?", (ident,)).fetchone()[0], 1)
            self.assertEqual(conn.execute("SELECT count(*) FROM state_audit WHERE resource_type='task_association'").fetchone()[0], 1)

    def test_status_view_holds_lock_during_owner_scoped_read(self):
        import ai_earn.task_associations as module
        ident = self.create()
        entered, release, writer_started, writer_done = (threading.Event() for _ in range(4))
        original = module._owned_workflow
        def hold(*args):
            row = original(*args)
            entered.set()
            if not release.wait(10):
                raise RuntimeError("timed out waiting for test barrier")
            return row
        def cancel():
            writer_started.set()
            self.queue.cancel(self.alice, self.item, operation_key="writer-cancel")
            writer_done.set()
        with patch.object(module, "_owned_workflow", side_effect=hold):
            with ThreadPoolExecutor(max_workers=2) as pool:
                reader = pool.submit(self.associations.inspect, self.alice, ident)
                try:
                    self.assertTrue(entered.wait(10))
                    writer = pool.submit(cancel)
                    self.assertTrue(writer_started.wait(10))
                    self.assertFalse(writer_done.wait(0.1))
                finally:
                    release.set()
                view = reader.result(timeout=15)
                writer.result(timeout=15)
        self.assertEqual(view["queue_status"], "queued")
        self.assertEqual(self.associations.inspect(self.alice, ident)["queue_status"], "cancelled")

    def test_cancel_pair_is_atomic_idempotent_and_survives_restore(self):
        ident = self.create()
        self.assertEqual(self.associations.cancel_pair(self.alice, ident, operation_key="cancel-pair"), ident)
        self.assertEqual(self.associations.cancel_pair(self.alice, ident, operation_key="cancel-pair"), ident)
        self.assertEqual(self.queue.get(self.alice, self.item)["status"], "cancelled")
        self.assertEqual(self.workflows.get(self.alice, self.workflow)["status"], "cancelled")
        with self.state.transaction() as conn:
            for event in ("queue_cancelled", "workflow_cancelled", "task_association_pair_cancelled"):
                self.assertEqual(conn.execute("SELECT count(*) FROM state_audit WHERE event=?", (event,)).fetchone()[0], 1)
            self.assertEqual(conn.execute("SELECT count(*) FROM state_operations WHERE operation_key='cancel-pair'").fetchone()[0], 1)
            self.assertEqual(conn.execute("SELECT count(*) FROM state_operations WHERE operation_key LIKE 'internal_pair_%'").fetchone()[0], 2)
        backup = self.directory / "cancel-pair-backup.sqlite3"
        restored = self.directory / "cancel-pair-restore.sqlite3"
        self.state.backup_to(backup, confirmed=True)
        StateStorage(backup).backup_to(restored, confirmed=True)
        self.assertEqual(TaskAssociations(self.auth, StateStorage(restored)).cancel_pair(self.alice, ident, operation_key="cancel-pair"), ident)

    def test_cancel_pair_checks_ownership_and_key_conflicts(self):
        ident = self.create()
        for token, record in ((self.bob, ident), (self.alice, "f" * 32), ("", ident)):
            with self.assertRaises(AccessDenied):
                self.associations.cancel_pair(token, record, operation_key="pair")
        for record, key in (("bad", "pair"), (ident, ""), (ident, "a b")):
            with self.assertRaises(AssociationError):
                self.associations.cancel_pair(self.alice, record, operation_key=key)
        for key in ("associate", "queue", "workflow"):
            with self.assertRaises(AssociationError):
                self.associations.cancel_pair(self.alice, ident, operation_key=key)
        self.assertEqual(self.queue.get(self.alice, self.item)["status"], "queued")
        self.assertEqual(self.workflows.get(self.alice, self.workflow)["status"], "planned")

    def test_cancel_pair_refuses_independently_changed_parents(self):
        ident = self.create()
        self.workflows.start(self.alice, self.workflow, operation_key="start")
        with self.assertRaises(AssociationError):
            self.associations.cancel_pair(self.alice, ident, operation_key="pair")
        self.assertEqual(self.queue.get(self.alice, self.item)["status"], "queued")
        self.assertEqual(self.workflows.get(self.alice, self.workflow)["status"], "in_progress")
        self.queue.cancel(self.alice, self.item, operation_key="independent")
        with self.assertRaises(AssociationError):
            self.associations.cancel_pair(self.alice, ident, operation_key="pair")
        with self.state.transaction() as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM state_operations WHERE operation_key='pair'").fetchone()[0], 0)

    def test_cancel_pair_rolls_back_partial_failure_and_expired_session(self):
        ident = self.create()
        with patch("ai_earn.task_associations._workflow_audit", side_effect=RuntimeError("injected")):
            with self.assertRaises(RuntimeError):
                self.associations.cancel_pair(self.alice, ident, operation_key="pair")
        self.assertEqual(self.queue.get(self.alice, self.item)["status"], "queued")
        self.assertEqual(self.workflows.get(self.alice, self.workflow)["status"], "planned")
        with self.state.transaction() as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM state_operations WHERE operation_key='pair' OR operation_key LIKE 'internal_pair_%'").fetchone()[0], 0)
        import ai_earn.task_associations as module
        original = module._workflow_audit
        def expire(*args):
            original(*args)
            self.clock.now += SESSION_SECONDS
        with patch.object(module, "_workflow_audit", side_effect=expire):
            with self.assertRaises(AccessDenied):
                self.associations.cancel_pair(self.alice, ident, operation_key="pair")
        with self.state.transaction() as conn:
            self.assertEqual(conn.execute("SELECT status FROM task_queue WHERE id=?", (self.item,)).fetchone()[0], "queued")
            self.assertEqual(conn.execute("SELECT status FROM task_workflows WHERE id=?", (self.workflow,)).fetchone()[0], "planned")
            self.assertEqual(conn.execute("SELECT count(*) FROM state_operations WHERE operation_key='pair' OR operation_key LIKE 'internal_pair_%'").fetchone()[0], 0)

    def test_cancel_pair_internal_key_collisions_fail_without_changes(self):
        ident = self.create()
        for keys in (("queue", "internal_pair_other"), ("pair", "internal_pair_other"), ("same", "same")):
            with self.subTest(keys=keys), patch("ai_earn.task_associations._pair_child_key", side_effect=keys):
                with self.assertRaises(StateStorageError):
                    self.associations.cancel_pair(self.alice, ident, operation_key="pair")
        self.assertEqual(self.queue.get(self.alice, self.item)["status"], "queued")
        self.assertEqual(self.workflows.get(self.alice, self.workflow)["status"], "planned")

    def test_cancel_pair_concurrent_identical_retries_and_competing_independent_call(self):
        ident = self.create()
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: self.associations.cancel_pair(self.alice, ident, operation_key="pair"), range(2)))
        self.assertEqual(results, [ident, ident])
        self.assertEqual(self.queue.get(self.alice, self.item)["status"], "cancelled")
        self.assertEqual(self.workflows.get(self.alice, self.workflow)["status"], "cancelled")
        other_q = self.queue.enqueue(self.alice, "other", operation_key="other-q")
        other_w = self.workflows.create(self.alice, "other", operation_key="other-w")
        other = self.associations.create(self.alice, other_q, other_w, operation_key="other-link")
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(self.associations.cancel_pair, self.alice, other, operation_key="other-pair"),
                       pool.submit(self.workflows.start, self.alice, other_w, operation_key="other-start")]
            outcomes = []
            for future in futures:
                try:
                    future.result(timeout=15)
                    outcomes.append("ok")
                except (AssociationError, WorkflowError):
                    outcomes.append("denied")
        self.assertCountEqual(outcomes, ["ok", "denied"])
        queue_status = self.queue.get(self.alice, other_q)["status"]
        workflow_status = self.workflows.get(self.alice, other_w)["status"]
        self.assertIn((queue_status, workflow_status), (("cancelled", "cancelled"), ("queued", "in_progress")))

    def test_cancel_pair_retries_conflict_with_other_pair_and_leases(self):
        first = self.create()
        q = self.queue.enqueue(self.alice, "other", operation_key="other-q")
        w = self.workflows.create(self.alice, "other", operation_key="other-w")
        other = self.associations.create(self.alice, q, w, operation_key="other-link")
        self.associations.cancel_pair(self.alice, first, operation_key="pair")
        with self.assertRaises(AssociationError):
            self.associations.cancel_pair(self.alice, other, operation_key="pair")
        self.queue.claim(self.alice, q, operation_key="other-claim", lease_seconds=60)
        with self.assertRaises(AssociationError):
            self.associations.cancel_pair(self.alice, other, operation_key="other-pair")
        self.assertEqual(self.queue.get(self.alice, q)["status"], "leased")
        self.assertEqual(self.workflows.get(self.alice, w)["status"], "planned")

    def test_cancel_pair_ambiguous_auth_exit_retry(self):
        ident = self.create()
        original = self.auth.storage.transaction
        @contextmanager
        def fail_after_state_commit():
            with original() as conn:
                yield conn
                raise RuntimeError("injected auth exit failure")
        with patch.object(self.auth.storage, "transaction", fail_after_state_commit):
            with self.assertRaises(RuntimeError):
                self.associations.cancel_pair(self.alice, ident, operation_key="pair")
        self.assertEqual(self.associations.cancel_pair(self.alice, ident, operation_key="pair"), ident)
        self.assertEqual(self.queue.get(self.alice, self.item)["status"], "cancelled")
        self.assertEqual(self.workflows.get(self.alice, self.workflow)["status"], "cancelled")

    def test_cancel_pair_controlled_process_crash_before_and_after_commit(self):
        ident = self.create()
        code = r"""
from contextlib import contextmanager
from pathlib import Path
import json, os, sys
import ai_earn.task_associations as module
from ai_earn.auth import AuthService
from ai_earn.auth_storage import AuthStorage
from ai_earn.state_storage import StateStorage
p = json.load(sys.stdin)
auth = AuthService(AuthStorage(Path(p['auth']), create=False), lambda: 1800000000)
service = module.TaskAssociations(auth, StateStorage(Path(p['state'])))
if p['point'] == 'before':
    original = module._workflow_audit
    def crash(*args):
        original(*args)
        os._exit(73)
    module._workflow_audit = crash
else:
    original_transaction = auth.storage.transaction
    @contextmanager
    def crash_after_state_commit():
        with original_transaction() as conn:
            yield conn
            os._exit(74)
    auth.storage.transaction = crash_after_state_commit
service.cancel_pair(p['token'], p['association'], operation_key='pair')
"""
        def run(point):
            payload = dict(auth=str(self.auth.storage.path), state=str(self.state.path), token=self.alice,
                           association=ident, point=point)
            return subprocess.run([sys.executable, "-c", code], input=json.dumps(payload), text=True,
                                  capture_output=True, timeout=25)
        self.assertEqual(run("before").returncode, 73)
        self.assertEqual(self.queue.get(self.alice, self.item)["status"], "queued")
        self.assertEqual(self.workflows.get(self.alice, self.workflow)["status"], "planned")
        with self.state.transaction() as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM state_operations WHERE operation_key='pair' OR operation_key LIKE 'internal_pair_%'").fetchone()[0], 0)
        self.assertEqual(run("after").returncode, 74)
        self.assertEqual(self.associations.cancel_pair(self.alice, ident, operation_key="pair"), ident)
        self.assertEqual(self.queue.get(self.alice, self.item)["status"], "cancelled")
        self.assertEqual(self.workflows.get(self.alice, self.workflow)["status"], "cancelled")
        self.state.backup_to(self.directory / "cancel-pair-crash-backup.sqlite3", confirmed=True)

    def test_start_pair_claims_and_starts_atomically_with_stale_retry(self):
        ident = self.create()
        lease = self.associations.start_pair(self.alice, ident, operation_key="pair-start", lease_seconds=30)
        self.assertEqual(self.associations.start_pair(self.alice, ident, operation_key="pair-start", lease_seconds=30), lease)
        self.assertTrue(self.queue.get_lease(self.alice, lease)["active"])
        self.assertEqual(self.queue.get(self.alice, self.item)["status"], "leased")
        self.assertEqual(self.workflows.get(self.alice, self.workflow)["status"], "in_progress")
        self.clock.now += 30
        self.assertEqual(self.associations.start_pair(self.alice, ident, operation_key="pair-start", lease_seconds=30), lease)
        self.assertFalse(self.queue.get_lease(self.alice, lease)["active"])
        backup = self.directory / "start-pair-backup"
        restored = self.directory / "start-pair-restore"
        self.state.backup_to(backup, confirmed=True)
        StateStorage(backup).backup_to(restored, confirmed=True)
        self.assertEqual(TaskAssociations(self.auth, StateStorage(restored)).start_pair(self.alice, ident, operation_key="pair-start", lease_seconds=30), lease)
        self.assertFalse(TaskQueue(self.auth, StateStorage(restored)).get_lease(self.alice, lease)["active"])

    def test_start_pair_authorization_validation_and_shared_keys(self):
        ident = self.create()
        for token, record in ((self.bob, ident), (self.alice, "f"*32), ("", ident)):
            with self.assertRaises(AccessDenied):
                self.associations.start_pair(token, record, operation_key="pair-start", lease_seconds=30)
        for seconds in (0, 3601, True, None, 2.5):
            with self.subTest(seconds=seconds), self.assertRaises(AssociationError):
                self.associations.start_pair(self.alice, ident, operation_key="pair-start", lease_seconds=seconds)
        for key in ("", "queue", "workflow", "associate"):
            with self.subTest(key=key), self.assertRaises(AssociationError):
                self.associations.start_pair(self.alice, ident, operation_key=key, lease_seconds=30)
        lease = self.associations.start_pair(self.alice, ident, operation_key="pair-start", lease_seconds=30)
        with self.assertRaises(AssociationError):
            self.associations.start_pair(self.alice, ident, operation_key="pair-start", lease_seconds=31)
        self.assertTrue(self.queue.get_lease(self.alice, lease)["active"])

    def test_start_pair_blocks_unready_cancelled_and_expired_queue(self):
        ident = self.create()
        self.workflows.start(self.alice, self.workflow, operation_key="independent")
        with self.assertRaises(AssociationError):
            self.associations.start_pair(self.alice, ident, operation_key="pair-start", lease_seconds=30)
        self.assertEqual(self.queue.get(self.alice, self.item)["status"], "queued")
        item = self.queue.enqueue(self.alice, "future", operation_key="future-q", available_at=self.clock.now + 10, deadline_at=self.clock.now + 15)
        workflow = self.workflows.create(self.alice, "future", operation_key="future-w")
        other = self.associations.create(self.alice, item, workflow, operation_key="future-link")
        with self.assertRaises(AssociationError):
            self.associations.start_pair(self.alice, other, operation_key="early", lease_seconds=10)
        self.clock.now += 15
        with self.assertRaises(AssociationError):
            self.associations.start_pair(self.alice, other, operation_key="late", lease_seconds=10)
        self.assertEqual(self.workflows.get(self.alice, workflow)["status"], "planned")

    def test_start_pair_failure_collision_expiry_rolls_back(self):
        ident = self.create()
        for collision in ("queue", "pair-start"):
            with self.subTest(collision=collision), patch("ai_earn.task_associations._pair_child_key", return_value=collision):
                with self.assertRaises(StateStorageError):
                    self.associations.start_pair(self.alice, ident, operation_key="pair-start", lease_seconds=30)
        with patch("ai_earn.task_associations._workflow_audit", side_effect=RuntimeError("injected")):
            with self.assertRaises(RuntimeError):
                self.associations.start_pair(self.alice, ident, operation_key="pair-start", lease_seconds=30)
        import ai_earn.task_associations as module
        original = module._workflow_audit
        def advance(*args):
            original(*args)
            self.clock.now += 30
        with patch.object(module, "_workflow_audit", side_effect=advance):
            with self.assertRaises(AssociationError):
                self.associations.start_pair(self.alice, ident, operation_key="pair-start", lease_seconds=30)
        with self.state.transaction() as conn:
            self.assertEqual(conn.execute("SELECT status FROM task_queue WHERE id=?", (self.item,)).fetchone()[0], "queued")
            self.assertEqual(conn.execute("SELECT status FROM task_workflows WHERE id=?", (self.workflow,)).fetchone()[0], "planned")
            self.assertEqual(conn.execute("SELECT count(*) FROM queue_leases WHERE queue_id=?", (self.item,)).fetchone()[0], 0)
            self.assertEqual(conn.execute("SELECT count(*) FROM state_operations WHERE operation_key='pair-start' OR operation_key LIKE 'internal_pair_%'").fetchone()[0], 0)

    def test_start_pair_competing_calls_and_direct_api_bypass(self):
        ident = self.create()
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: self.associations.start_pair(self.alice, ident, operation_key="same", lease_seconds=60), range(2)))
        self.assertEqual(results[0], results[1])
        with self.state.transaction() as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM queue_leases WHERE queue_id=?", (self.item,)).fetchone()[0], 1)
            self.assertEqual(conn.execute("SELECT count(*) FROM state_audit WHERE event='workflow_started' AND resource_id=?", (self.workflow,)).fetchone()[0], 1)
        q = self.queue.enqueue(self.alice, "direct", operation_key="direct-q")
        w = self.workflows.create(self.alice, "direct", operation_key="direct-w")
        other = self.associations.create(self.alice, q, w, operation_key="direct-link")
        self.queue.claim(self.alice, q, operation_key="direct-claim", lease_seconds=60)
        with self.assertRaises(AssociationError):
            self.associations.start_pair(self.alice, other, operation_key="no-bypass", lease_seconds=60)
        self.assertEqual(self.workflows.get(self.alice, w)["status"], "planned")

    def test_start_pair_caps_deadline_and_retries_after_recovery(self):
        q = self.queue.enqueue(self.alice, "deadline", operation_key="deadline-q", deadline_at=self.clock.now + 5, backoff_seconds=0)
        w = self.workflows.create(self.alice, "deadline", operation_key="deadline-w")
        ident = self.associations.create(self.alice, q, w, operation_key="deadline-link")
        lease = self.associations.start_pair(self.alice, ident, operation_key="pair-start", lease_seconds=60)
        self.assertEqual(self.queue.get_lease(self.alice, lease)["lease_until"], self.clock.now + 5)
        self.clock.now += 5
        self.queue.recover(self.alice, q, lease, operation_key="recover")
        self.assertFalse(self.queue.get_lease(self.alice, lease)["active"])
        self.assertEqual(self.associations.start_pair(self.alice, ident, operation_key="pair-start", lease_seconds=60), lease)
        self.assertEqual(self.queue.get(self.alice, q)["status"], "exhausted")
        self.assertEqual(self.workflows.get(self.alice, w)["status"], "in_progress")

    def test_start_pair_session_expiry_after_writes_rolls_back(self):
        ident = self.create()
        import ai_earn.task_associations as module
        original = module._workflow_audit
        def expire(*args):
            original(*args)
            self.clock.now += SESSION_SECONDS
        with patch.object(module, "_workflow_audit", side_effect=expire):
            # The lease outlives the session; the session recheck must roll back.
            with self.assertRaises(AccessDenied):
                self.associations.start_pair(self.alice, ident, operation_key="pair-start", lease_seconds=3600)
        with self.state.transaction() as conn:
            self.assertEqual(conn.execute("SELECT status FROM task_queue WHERE id=?", (self.item,)).fetchone()[0], "queued")
            self.assertEqual(conn.execute("SELECT status FROM task_workflows WHERE id=?", (self.workflow,)).fetchone()[0], "planned")
            self.assertEqual(conn.execute("SELECT count(*) FROM queue_leases WHERE queue_id=?", (self.item,)).fetchone()[0], 0)

    def test_start_pair_competes_with_direct_workflow_start(self):
        ident = self.create()
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(self.associations.start_pair, self.alice, ident, operation_key="pair-start", lease_seconds=60),
                       pool.submit(self.workflows.start, self.alice, self.workflow, operation_key="direct-start")]
            outcomes = []
            for f in futures:
                try:
                    f.result(timeout=15)
                    outcomes.append("ok")
                except (AssociationError, WorkflowError):
                    outcomes.append("denied")
        self.assertCountEqual(outcomes, ["ok", "denied"])
        self.assertEqual(self.workflows.get(self.alice, self.workflow)["status"], "in_progress")
        self.assertIn(self.queue.get(self.alice, self.item)["status"], ("queued", "leased"))
        self.state.backup_to(self.directory / "competing-start", confirmed=True)

    def test_start_pair_corrupt_retry_result_is_rejected(self):
        ident = self.create()
        lease = self.associations.start_pair(self.alice, ident, operation_key="pair-start", lease_seconds=60)
        q = self.queue.enqueue(self.alice, "other", operation_key="other-q")
        other_lease = self.queue.claim(self.alice, q, operation_key="other-claim", lease_seconds=60)
        with self.state.transaction() as conn:
            conn.execute("UPDATE state_operations SET resource_id=? WHERE operation_key='pair-start'", (other_lease,))
        with self.assertRaises(StateStorageError):
            self.associations.start_pair(self.alice, ident, operation_key="pair-start", lease_seconds=60)
        with self.state.transaction() as conn:
            conn.execute("UPDATE state_operations SET resource_id=? WHERE operation_key='pair-start'", (lease,))
        self.assertEqual(self.associations.start_pair(self.alice, ident, operation_key="pair-start", lease_seconds=60), lease)

    def test_start_pair_controlled_process_crash_before_and_after_commit(self):
        ident = self.create()
        code = r"""
from contextlib import contextmanager
from pathlib import Path
import json, os, sys
import ai_earn.task_associations as module
from ai_earn.auth import AuthService
from ai_earn.auth_storage import AuthStorage
from ai_earn.state_storage import StateStorage
p = json.load(sys.stdin)
auth = AuthService(AuthStorage(Path(p['auth']), create=False), lambda: 1800000000)
service = module.TaskAssociations(auth, StateStorage(Path(p['state'])))
if p['point'] == 'before':
    original = module._workflow_audit
    def crash(*args):
        original(*args)
        os._exit(73)
    module._workflow_audit = crash
else:
    original = auth.storage.transaction
    @contextmanager
    def crash_after_state_commit():
        with original() as conn:
            yield conn
            os._exit(74)
    auth.storage.transaction = crash_after_state_commit
service.start_pair(p['token'], p['association'], operation_key='pair-start', lease_seconds=60)
"""
        def run(point):
            return subprocess.run([sys.executable, "-c", code], input=json.dumps(dict(
                auth=str(self.auth.storage.path), state=str(self.state.path), token=self.alice,
                association=ident, point=point)), text=True, capture_output=True, timeout=25)
        self.assertEqual(run("before").returncode, 73)
        self.assertEqual(self.queue.get(self.alice, self.item)["status"], "queued")
        self.assertEqual(self.workflows.get(self.alice, self.workflow)["status"], "planned")
        with self.state.transaction() as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM queue_leases WHERE queue_id=?", (self.item,)).fetchone()[0], 0)
        self.assertEqual(run("after").returncode, 74)
        lease = self.associations.start_pair(self.alice, ident, operation_key="pair-start", lease_seconds=60)
        self.assertTrue(self.queue.get_lease(self.alice, lease)["active"])
        self.assertEqual(self.workflows.get(self.alice, self.workflow)["status"], "in_progress")
        self.state.backup_to(self.directory / "start-pair-crash", confirmed=True)

    def test_pair_review_lifecycle_and_historical_retry_after_expiry(self):
        ident = self.create()
        lease = self.associations.start_pair(self.alice, ident, operation_key="start-pair", lease_seconds=60)
        self.assertEqual(self.associations.submit_pair(self.alice, ident, lease, "first", operation_key="submit1"), self.workflow)
        self.assertEqual(self.associations.review_pair(self.alice, ident, lease, "first", approved=False, confirmed=True, operation_key="reject"), self.workflow)
        self.assertEqual(self.workflows.get(self.alice, self.workflow)["status"], "in_progress")
        self.associations.submit_pair(self.alice, ident, lease, "second", operation_key="submit2")
        self.associations.review_pair(self.alice, ident, lease, "second", approved=True, confirmed=True, operation_key="approve")
        self.assertEqual(self.workflows.get(self.alice, self.workflow)["status"], "completed")
        self.assertEqual(self.queue.get(self.alice, self.item)["status"], "leased")
        self.clock.now += 60
        self.assertEqual(self.associations.review_pair(self.alice, ident, lease, "second", approved=True, confirmed=True, operation_key="approve"), self.workflow)
        self.assertFalse(self.queue.get_lease(self.alice, lease)["active"])
        backup = self.directory / "review-pair-backup"
        self.state.backup_to(backup, confirmed=True)
        service = TaskAssociations(self.auth, StateStorage(backup))
        self.assertEqual(service.submit_pair(self.alice, ident, lease, "first", operation_key="submit1"), self.workflow)

    def test_pair_review_denies_invalid_owners_leases_outputs_and_keys(self):
        ident = self.create()
        lease = self.associations.start_pair(self.alice, ident, operation_key="start-pair", lease_seconds=60)
        for token, record in ((self.bob, ident), (self.alice, "f"*32), ("", ident)):
            with self.assertRaises(AccessDenied):
                self.associations.submit_pair(token, record, lease, "output", operation_key="submit")
        for invalid in ("bad", "f"*32):
            with self.assertRaises(AssociationError):
                self.associations.submit_pair(self.alice, ident, invalid, "output", operation_key="submit")
        for output in ("", " ", "x"*4097, None, "bad\x00"):
            with self.assertRaises(AssociationError):
                self.associations.submit_pair(self.alice, ident, lease, output, operation_key="submit")
        with self.assertRaises(AssociationError):
            self.associations.submit_pair(self.alice, ident, lease, "output", operation_key="queue")
        self.associations.submit_pair(self.alice, ident, lease, "output", operation_key="submit")
        for approved, confirmed, output in ((True, False, "output"), (True, 1, "output"), (1, True, "output"), (True, True, "different")):
            with self.subTest(approved=approved, confirmed=confirmed, output=output), self.assertRaises(AssociationError):
                self.associations.review_pair(self.alice, ident, lease, output, approved=approved, confirmed=confirmed, operation_key="review")
        with self.assertRaises(AssociationError):
            self.associations.submit_pair(self.alice, ident, lease, "different", operation_key="submit")
        self.assertEqual(self.workflows.get(self.alice, self.workflow)["status"], "pending_review")

    def test_pair_review_rejects_other_or_stale_lease_and_direct_bypass_remains(self):
        ident = self.create()
        lease = self.associations.start_pair(self.alice, ident, operation_key="start-pair", lease_seconds=30)
        q = self.queue.enqueue(self.alice, "other", operation_key="other-q")
        other_lease = self.queue.claim(self.alice, q, operation_key="other-claim", lease_seconds=60)
        with self.assertRaises(AssociationError):
            self.associations.submit_pair(self.alice, ident, other_lease, "output", operation_key="wrong-lease")
        self.clock.now += 30
        with self.assertRaises(AssociationError):
            self.associations.submit_pair(self.alice, ident, lease, "output", operation_key="expired")
        self.workflows.submit(self.alice, self.workflow, "direct bypass", operation_key="direct-submit")
        with self.assertRaises(AssociationError):
            self.associations.review_pair(self.alice, ident, lease, "direct bypass", approved=True, confirmed=True, operation_key="expired-review")
        self.assertEqual(self.workflows.get(self.alice, self.workflow)["status"], "pending_review")

    def test_pair_review_rolls_back_on_lease_expiry_and_injected_error(self):
        ident = self.create()
        lease = self.associations.start_pair(self.alice, ident, operation_key="start-pair", lease_seconds=60)
        import ai_earn.task_associations as module
        original = module._workflow_submit_in_transaction
        def expire(*args):
            original(*args)
            self.clock.now += 60
        with patch.object(module, "_workflow_submit_in_transaction", side_effect=expire):
            with self.assertRaises(AssociationError):
                self.associations.submit_pair(self.alice, ident, lease, "output", operation_key="submit")
        with self.state.transaction() as conn:
            self.assertEqual(conn.execute("SELECT status FROM task_workflows WHERE id=?", (self.workflow,)).fetchone()[0], "in_progress")
            self.assertEqual(conn.execute("SELECT count(*) FROM state_operations WHERE operation_key='submit'").fetchone()[0], 0)
        self.clock.now -= 60
        self.associations.submit_pair(self.alice, ident, lease, "output", operation_key="submit")
        with patch.object(module, "_workflow_review_in_transaction", side_effect=RuntimeError("injected")):
            with self.assertRaises(RuntimeError):
                self.associations.review_pair(self.alice, ident, lease, "output", approved=True, confirmed=True, operation_key="review")
        self.assertEqual(self.workflows.get(self.alice, self.workflow)["status"], "pending_review")

    def test_pair_review_concurrent_competing_decisions(self):
        ident = self.create()
        lease = self.associations.start_pair(self.alice, ident, operation_key="start-pair", lease_seconds=60)
        self.associations.submit_pair(self.alice, ident, lease, "output", operation_key="submit")
        def decide(decision):
            try:
                self.associations.review_pair(self.alice, ident, lease, "output", approved=decision, confirmed=True, operation_key="yes" if decision else "no")
                return True
            except AssociationError:
                return False
        with ThreadPoolExecutor(max_workers=2) as pool:
            self.assertEqual(sum(pool.map(decide, (True, False))), 1)
        self.assertIn(self.workflows.get(self.alice, self.workflow)["status"], ("completed", "in_progress"))
        self.state.backup_to(self.directory / "pair-review-concurrency", confirmed=True)

    def test_pair_submit_allows_new_lease_after_recovery_not_old_lease(self):
        ident = self.create()
        lease = self.associations.start_pair(self.alice, ident, operation_key="start", lease_seconds=5)
        self.clock.now += 5
        self.queue.recover(self.alice, self.item, lease, operation_key="recover")
        self.clock.now += 60  # default queue backoff
        new_lease = self.queue.claim(self.alice, self.item, operation_key="new-claim", lease_seconds=60)
        with self.assertRaises(AssociationError):
            self.associations.submit_pair(self.alice, ident, lease, "output", operation_key="stale")
        self.assertEqual(self.associations.submit_pair(self.alice, ident, new_lease, "output", operation_key="submit"), self.workflow)
        self.state.backup_to(self.directory / "pair-new-lease", confirmed=True)

    def test_pair_submit_session_expiry_after_writes_rolls_back(self):
        ident = self.create()
        lease = self.associations.start_pair(self.alice, ident, operation_key="start", lease_seconds=3600)
        import ai_earn.task_associations as module
        original = module._workflow_submit_in_transaction
        def expire(*args):
            result = original(*args)
            self.clock.now += SESSION_SECONDS
            return result
        with patch.object(module, "_workflow_submit_in_transaction", side_effect=expire):
            with self.assertRaises(AccessDenied):
                self.associations.submit_pair(self.alice, ident, lease, "output", operation_key="submit")
        with self.state.transaction() as conn:
            self.assertEqual(conn.execute("SELECT status FROM task_workflows WHERE id=?", (self.workflow,)).fetchone()[0], "in_progress")
            self.assertEqual(conn.execute("SELECT count(*) FROM state_operations WHERE operation_key='submit'").fetchone()[0], 0)

    def test_pair_review_rejects_wrong_owned_retry_result(self):
        ident = self.create()
        lease = self.associations.start_pair(self.alice, ident, operation_key="start", lease_seconds=60)
        self.associations.submit_pair(self.alice, ident, lease, "output", operation_key="submit")
        other = self.workflows.create(self.alice, "other", operation_key="other")
        with self.state.transaction() as conn:
            conn.execute("UPDATE state_operations SET resource_id=? WHERE operation_key='submit'", (other,))
        with self.assertRaises(StateStorageError):
            self.associations.submit_pair(self.alice, ident, lease, "output", operation_key="submit")
        with self.state.transaction() as conn:
            conn.execute("UPDATE state_operations SET resource_id=? WHERE operation_key='submit'", (self.workflow,))
        self.assertEqual(self.associations.submit_pair(self.alice, ident, lease, "output", operation_key="submit"), self.workflow)

    def test_pair_review_controlled_crash_before_and_after_commit(self):
        ident = self.create()
        lease = self.associations.start_pair(self.alice, ident, operation_key="start", lease_seconds=60)
        self.associations.submit_pair(self.alice, ident, lease, "output", operation_key="submit")
        code = r"""
from contextlib import contextmanager
from pathlib import Path
import json, os, sys
import ai_earn.task_associations as module
from ai_earn.auth import AuthService
from ai_earn.auth_storage import AuthStorage
from ai_earn.state_storage import StateStorage
p = json.load(sys.stdin)
auth = AuthService(AuthStorage(Path(p['auth']), create=False), lambda: 1800000000)
service = module.TaskAssociations(auth, StateStorage(Path(p['state'])))
if p['point'] == 'before':
    original = module._workflow_review_in_transaction
    def crash(*args):
        original(*args)
        os._exit(73)
    module._workflow_review_in_transaction = crash
else:
    original = auth.storage.transaction
    @contextmanager
    def crash_after_state_commit():
        with original() as conn:
            yield conn
            os._exit(74)
    auth.storage.transaction = crash_after_state_commit
service.review_pair(p['token'], p['association'], p['lease'], 'output', approved=True, confirmed=True, operation_key='review')
"""
        def run(point):
            return subprocess.run([sys.executable, "-c", code], input=json.dumps(dict(
                auth=str(self.auth.storage.path), state=str(self.state.path), token=self.alice,
                association=ident, lease=lease, point=point)), text=True, capture_output=True, timeout=25)
        self.assertEqual(run("before").returncode, 73)
        self.assertEqual(self.workflows.get(self.alice, self.workflow)["status"], "pending_review")
        with self.state.transaction() as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM state_operations WHERE operation_key='review'").fetchone()[0], 0)
        self.assertEqual(run("after").returncode, 74)
        self.assertEqual(self.associations.review_pair(self.alice, ident, lease, "output", approved=True, confirmed=True, operation_key="review"), self.workflow)
        self.assertEqual(self.workflows.get(self.alice, self.workflow)["status"], "completed")
        self.state.backup_to(self.directory / "pair-review-crash", confirmed=True)

    def test_paired_retry_resume_and_review_uses_new_lease(self):
        ident = self.create()
        old = self.associations.start_pair(self.alice, ident, operation_key="start", lease_seconds=30)
        self.assertEqual(self.associations.retry_pair(self.alice, ident, old, operation_key="retry"), self.item)
        self.assertEqual(self.associations.retry_pair(self.alice, ident, old, operation_key="retry"), self.item)
        with self.assertRaises(AssociationError):
            self.associations.resume_pair(self.alice, ident, operation_key="resume", lease_seconds=30)
        self.clock.now += 60
        new = self.associations.resume_pair(self.alice, ident, operation_key="resume", lease_seconds=30)
        self.assertNotEqual(old, new)
        self.assertEqual(self.associations.resume_pair(self.alice, ident, operation_key="resume", lease_seconds=30), new)
        with self.assertRaises(AssociationError):
            self.associations.submit_pair(self.alice, ident, old, "output", operation_key="stale")
        self.associations.submit_pair(self.alice, ident, new, "output", operation_key="submit")
        self.associations.review_pair(self.alice, ident, new, "output", approved=True, confirmed=True, operation_key="review")
        self.assertEqual(self.workflows.get(self.alice, self.workflow)["status"], "completed")
        backup = self.directory / "paired-recovery-backup"
        self.state.backup_to(backup, confirmed=True)
        self.assertEqual(TaskAssociations(self.auth, StateStorage(backup)).resume_pair(self.alice, ident, operation_key="resume", lease_seconds=30), new)

    def test_paired_recover_preserves_pending_review(self):
        q = self.queue.enqueue(self.alice, "deadline", operation_key="deadline-q", deadline_at=self.clock.now + 70, backoff_seconds=0)
        w = self.workflows.create(self.alice, "deadline", operation_key="deadline-w")
        ident = self.associations.create(self.alice, q, w, operation_key="deadline-link")
        old = self.associations.start_pair(self.alice, ident, operation_key="start", lease_seconds=5)
        self.associations.submit_pair(self.alice, ident, old, "output", operation_key="submit")
        with self.assertRaises(AssociationError):
            self.associations.retry_pair(self.alice, ident, old, operation_key="retry")
        self.clock.now += 5
        self.associations.recover_pair(self.alice, ident, old, operation_key="recover")
        self.assertEqual(self.workflows.get(self.alice, w)["status"], "pending_review")
        new = self.associations.resume_pair(self.alice, ident, operation_key="resume", lease_seconds=60)
        self.associations.review_pair(self.alice, ident, new, "output", approved=True, confirmed=True, operation_key="review")
        self.assertEqual(self.workflows.get(self.alice, w)["status"], "completed")
        self.state.backup_to(self.directory / "paired-pending-review-backup", confirmed=True)

    def test_paired_recovery_denials_and_stale_inputs(self):
        ident = self.create()
        old = self.associations.start_pair(self.alice, ident, operation_key="start", lease_seconds=5)
        for token, record in ((self.bob, ident), (self.alice, "f"*32), ("", ident)):
            with self.assertRaises(AccessDenied):
                self.associations.recover_pair(token, record, old, operation_key="recover")
        with self.assertRaises(AssociationError):
            self.associations.recover_pair(self.alice, ident, old, operation_key="early")
        with self.assertRaises(AssociationError):
            self.associations.retry_pair(self.alice, ident, "f"*32, operation_key="wrong")
        self.clock.now += 5
        with self.assertRaises(AssociationError):
            self.associations.retry_pair(self.alice, ident, old, operation_key="late")
        self.associations.recover_pair(self.alice, ident, old, operation_key="recover")
        with self.assertRaises(AssociationError):
            self.associations.retry_pair(self.alice, ident, old, operation_key="recover")
        with self.assertRaises(AssociationError):
            self.associations.resume_pair(self.alice, ident, operation_key="resume", lease_seconds=True)

    def test_paired_retry_clock_expiry_and_failure_roll_back(self):
        ident = self.create()
        lease = self.associations.start_pair(self.alice, ident, operation_key="start", lease_seconds=5)
        import ai_earn.task_associations as module
        original = module._reschedule_in_transaction
        def expire(*args, **kwargs):
            result = original(*args, **kwargs)
            self.clock.now += 5
            return result
        with patch.object(module, "_reschedule_in_transaction", side_effect=expire):
            with self.assertRaises(AssociationError):
                self.associations.retry_pair(self.alice, ident, lease, operation_key="retry")
        with self.state.transaction() as conn:
            self.assertEqual(conn.execute("SELECT status FROM task_queue WHERE id=?", (self.item,)).fetchone()[0], "leased")
            self.assertEqual(conn.execute("SELECT count(*) FROM state_operations WHERE operation_key='retry'").fetchone()[0], 0)
        self.assertEqual(self.associations.recover_pair(self.alice, ident, lease, operation_key="recover"), self.item)

    def test_paired_resume_concurrency_exhaustion_and_bypass(self):
        ident = self.create()
        old = self.associations.start_pair(self.alice, ident, operation_key="start", lease_seconds=5)
        self.clock.now += 5
        self.associations.recover_pair(self.alice, ident, old, operation_key="recover")
        self.clock.now += 60
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: self.associations.resume_pair(self.alice, ident, operation_key="resume", lease_seconds=5), range(2)))
        self.assertEqual(results[0], results[1])
        self.clock.now += 5
        self.associations.recover_pair(self.alice, ident, results[0], operation_key="recover2")
        self.clock.now += 60
        # Independent API is still callable; coordination is not enforced.
        third = self.queue.claim(self.alice, self.item, operation_key="direct-claim", lease_seconds=5)
        self.clock.now += 5
        self.associations.recover_pair(self.alice, ident, third, operation_key="recover3")
        self.assertEqual(self.queue.get(self.alice, self.item)["status"], "exhausted")
        with self.assertRaises(AssociationError):
            self.associations.resume_pair(self.alice, ident, operation_key="no-more", lease_seconds=5)
        self.state.backup_to(self.directory / "paired-exhausted-backup", confirmed=True)

    def test_paired_recovery_session_expiry_rolls_back(self):
        ident = self.create()
        lease = self.associations.start_pair(self.alice, ident, operation_key="start", lease_seconds=5)
        self.clock.now += 5
        import ai_earn.task_associations as module
        original = module._reschedule_in_transaction
        def expire(*args, **kwargs):
            result = original(*args, **kwargs)
            self.clock.now += SESSION_SECONDS
            return result
        with patch.object(module, "_reschedule_in_transaction", side_effect=expire):
            with self.assertRaises(AccessDenied):
                self.associations.recover_pair(self.alice, ident, lease, operation_key="recover")
        with self.state.transaction() as conn:
            self.assertEqual(conn.execute("SELECT status FROM task_queue WHERE id=?", (self.item,)).fetchone()[0], "leased")
            self.assertEqual(conn.execute("SELECT count(*) FROM state_operations WHERE operation_key='recover'").fetchone()[0], 0)

    def test_paired_recover_and_resume_controlled_crashes(self):
        ident = self.create()
        old = self.associations.start_pair(self.alice, ident, operation_key="start", lease_seconds=5)
        self.clock.now += 5
        code = r"""
from contextlib import contextmanager
from pathlib import Path
import json, os, sys
import ai_earn.task_associations as module
from ai_earn.auth import AuthService
from ai_earn.auth_storage import AuthStorage
from ai_earn.state_storage import StateStorage
p = json.load(sys.stdin)
auth = AuthService(AuthStorage(Path(p['auth']), create=False), lambda: p['now'])
service = module.TaskAssociations(auth, StateStorage(Path(p['state'])))
if p['point'] == 'before':
    name = '_reschedule_in_transaction' if p['action'] == 'recover' else '_claim_in_transaction'
    original = getattr(module, name)
    def crash(*args, **kwargs):
        original(*args, **kwargs)
        os._exit(73)
    setattr(module, name, crash)
else:
    original = auth.storage.transaction
    @contextmanager
    def crash_after_state_commit():
        with original() as conn:
            yield conn
            os._exit(74)
    auth.storage.transaction = crash_after_state_commit
if p['action'] == 'recover':
    service.recover_pair(p['token'], p['association'], p['lease'], operation_key='recover')
else:
    service.resume_pair(p['token'], p['association'], operation_key='resume', lease_seconds=30)
"""
        def run(action, point):
            return subprocess.run([sys.executable, "-c", code], input=json.dumps(dict(
                auth=str(self.auth.storage.path), state=str(self.state.path), token=self.alice,
                association=ident, lease=old, now=self.clock.now, action=action, point=point)),
                text=True, capture_output=True, timeout=25)
        self.assertEqual(run("recover", "before").returncode, 73)
        self.assertEqual(self.queue.get(self.alice, self.item)["status"], "leased")
        self.assertEqual(run("recover", "after").returncode, 74)
        self.assertEqual(self.associations.recover_pair(self.alice, ident, old, operation_key="recover"), self.item)
        self.clock.now += 60
        self.assertEqual(run("resume", "before").returncode, 73)
        self.assertEqual(self.queue.get(self.alice, self.item)["status"], "queued")
        self.assertEqual(run("resume", "after").returncode, 74)
        lease = self.associations.resume_pair(self.alice, ident, operation_key="resume", lease_seconds=30)
        self.assertTrue(self.queue.get_lease(self.alice, lease)["active"])
        self.assertEqual(self.workflows.get(self.alice, self.workflow)["status"], "in_progress")
        self.state.backup_to(self.directory / "paired-recovery-crash-backup", confirmed=True)

    def test_paired_resume_rejects_conflicts_and_foreign_owner(self):
        ident = self.create()
        lease = self.associations.start_pair(self.alice, ident, operation_key="start", lease_seconds=5)
        self.clock.now += 5
        self.associations.recover_pair(self.alice, ident, lease, operation_key="recover")
        self.clock.now += 60
        for token, record in ((self.bob, ident), (self.alice, "f"*32), ("", ident)):
            with self.assertRaises(AccessDenied):
                self.associations.resume_pair(token, record, operation_key="resume", lease_seconds=5)
        for key in ("start", "recover", "queue"):
            with self.assertRaises(AssociationError):
                self.associations.resume_pair(self.alice, ident, operation_key=key, lease_seconds=5)
        new = self.associations.resume_pair(self.alice, ident, operation_key="resume", lease_seconds=5)
        with self.assertRaises(AssociationError):
            self.associations.resume_pair(self.alice, ident, operation_key="resume", lease_seconds=6)
        self.clock.now += 5
        self.assertEqual(self.associations.resume_pair(self.alice, ident, operation_key="resume", lease_seconds=5), new)
        self.assertFalse(self.queue.get_lease(self.alice, new)["active"])

    def test_cross_owner_invalid_inputs_and_denial(self):
        bob_q = self.queue.enqueue(self.bob, "Other", operation_key="queue")
        bob_w = self.workflows.create(self.bob, "Other", operation_key="workflow")
        ident = self.create()
        for fn in (lambda: self.associations.get(self.bob, ident),
                   lambda: self.associations.create(self.bob, self.item, bob_w, operation_key="a"),
                   lambda: self.associations.create(self.bob, bob_q, self.workflow, operation_key="a"),
                   lambda: self.associations.create(self.alice, "f"*32, self.workflow, operation_key="a")):
            with self.assertRaises(AccessDenied):
                fn()
        for queue, workflow, key in (("bad", self.workflow, "x"), (self.item, "bad", "x"), (self.item, self.workflow, "")):
            with self.assertRaises(AssociationError):
                self.associations.create(self.alice, queue, workflow, operation_key=key)
        self.auth.logout(self.bob)
        with self.assertRaises(AccessDenied):
            self.associations.create(self.bob, bob_q, bob_w, operation_key="new")
        self.clock.now += SESSION_SECONDS
        with self.assertRaises(AccessDenied):
            self.associations.get(self.alice, ident)

    def test_distinct_key_conflicts_one_to_one_and_shared_keys(self):
        ident = self.create()
        other_q = self.queue.enqueue(self.alice, "Other", operation_key="other-q")
        other_w = self.workflows.create(self.alice, "Other", operation_key="other-w")
        for fn in (lambda: self.create("new"),
                   lambda: self.associations.create(self.alice, other_q, self.workflow, operation_key="new"),
                   lambda: self.associations.create(self.alice, self.item, other_w, operation_key="new"),
                   lambda: self.associations.create(self.alice, other_q, other_w, operation_key="associate"),
                   lambda: self.associations.create(self.alice, other_q, other_w, operation_key="queue")):
            with self.assertRaises(AssociationError):
                fn()
        self.assertEqual(self.create(), ident)

    def test_only_queued_and_planned_can_be_newly_associated(self):
        self.queue.cancel(self.alice, self.item, operation_key="cancel")
        with self.assertRaises(AssociationError):
            self.create()
        new_q = self.queue.enqueue(self.alice, "Other", operation_key="other")
        self.workflows.start(self.alice, self.workflow, operation_key="start")
        with self.assertRaises(AssociationError):
            self.associations.create(self.alice, new_q, self.workflow, operation_key="new")
        with self.state.transaction() as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM task_associations").fetchone()[0], 0)

    def test_concurrent_identical_and_competing_requests(self):
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: self.create(), range(2)))
        self.assertEqual(results[0], results[1])
        with self.state.transaction() as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM task_associations").fetchone()[0], 1)
            self.assertEqual(conn.execute("SELECT count(*) FROM state_audit WHERE event='task_association_created'").fetchone()[0], 1)

    def test_failures_roll_back_state_audit_and_retry(self):
        for helper in ("_audit", "_remember"):
            with self.subTest(helper=helper), patch("ai_earn.task_associations." + helper, side_effect=RuntimeError("injected")):
                with self.assertRaises(RuntimeError):
                    self.create()
            with self.state.transaction() as conn:
                self.assertEqual(conn.execute("SELECT count(*) FROM task_associations").fetchone()[0], 0)
                self.assertEqual(conn.execute("SELECT count(*) FROM state_operations WHERE operation_key='associate'").fetchone()[0], 0)
                self.assertEqual(conn.execute("SELECT count(*) FROM state_audit WHERE event='task_association_created'").fetchone()[0], 0)
        self.assertTrue(self.create())

    def test_backup_restore_preserves_association_and_retry(self):
        ident = self.create()
        backup = self.directory / "v10-backup.sqlite3"
        restored = self.directory / "v10-restored.sqlite3"
        self.state.backup_to(backup, confirmed=True)
        StateStorage(backup).backup_to(restored, confirmed=True)
        service = TaskAssociations(self.auth, StateStorage(restored))
        self.assertEqual(service.create(self.alice, self.item, self.workflow, operation_key="associate"), ident)
        self.assertEqual(service.get(self.alice, ident)["queue_id"], self.item)

    def test_direct_cross_owner_foreign_keys_fail_and_tampered_index_rejected(self):
        self.create()
        bob_q = self.queue.enqueue(self.bob, "Other", operation_key="queue")
        bob_w = self.workflows.create(self.bob, "Other", operation_key="workflow")
        alice_q = self.queue.enqueue(self.alice, "Another", operation_key="new-q")
        alice_w = self.workflows.create(self.alice, "Another", operation_key="new-w")
        with closing(sqlite3.connect(self.state.path)) as conn:
            conn.execute("PRAGMA foreign_keys=ON")
            owner = conn.execute("SELECT owner_id FROM task_queue WHERE id=?", (alice_q,)).fetchone()[0]
            for queue_id, workflow_id in ((bob_q, alice_w), (alice_q, bob_w)):
                with self.assertRaisesRegex(sqlite3.IntegrityError, "FOREIGN KEY constraint failed"):
                    conn.execute("INSERT INTO task_associations VALUES (?,?,?,?,?)", ("f"*32, owner, queue_id, workflow_id, self.clock.now))
        with closing(sqlite3.connect(self.state.path)) as conn, conn:
            conn.execute("DROP INDEX task_workflows_id_owner_idx")
        with self.assertRaises(StateStorageError):
            StateStorage(self.state.path)

    def test_competing_distinct_keys_only_one_creates_link(self):
        def create(key):
            try:
                return self.create(key)
            except AssociationError:
                return None
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(create, ("first", "second")))
        self.assertEqual(sum(value is not None for value in results), 1)
        with self.state.transaction() as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM task_associations").fetchone()[0], 1)
            self.assertEqual(conn.execute("SELECT count(*) FROM state_audit WHERE event='task_association_created'").fetchone()[0], 1)

    def test_session_expiry_after_writes_rolls_back(self):
        from ai_earn.task_associations import _remember
        def expire(*args):
            _remember(*args)
            self.clock.now += SESSION_SECONDS
        with patch("ai_earn.task_associations._remember", side_effect=expire):
            with self.assertRaises(AccessDenied):
                self.create()
        with self.state.transaction() as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM task_associations").fetchone()[0], 0)
            self.assertEqual(conn.execute("SELECT count(*) FROM state_operations WHERE operation_key='associate'").fetchone()[0], 0)
            self.assertEqual(conn.execute("SELECT count(*) FROM state_audit WHERE event='task_association_created'").fetchone()[0], 0)

    def test_ambiguous_auth_exit_exact_retry_is_safe(self):
        original = self.auth.storage.transaction
        @contextmanager
        def fail_after_commit():
            with original() as conn:
                yield conn
                raise RuntimeError("injected auth exit failure")
        with patch.object(self.auth.storage, "transaction", fail_after_commit):
            with self.assertRaises(RuntimeError):
                self.create()
        ident = self.create()
        self.assertEqual(self.associations.get(self.alice, ident)["queue_id"], self.item)
        with self.state.transaction() as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM task_associations").fetchone()[0], 1)
            self.assertEqual(conn.execute("SELECT count(*) FROM state_audit WHERE event='task_association_created'").fetchone()[0], 1)

    def test_migration_confirmation_backup_and_rollback(self):
        source = StateStorage(self.directory / "v9.sqlite3")
        dest = self.directory / "attempt-v9.sqlite3"
        with self.assertRaises(StateStorageError):
            source.migrate_to_v10(dest)
        self.assertFalse(dest.exists())
        with patch("ai_earn.state_storage._apply_v10", side_effect=RuntimeError("injected failure")):
            with self.assertRaises(RuntimeError):
                source.migrate_to_v10(dest, confirmed=True)
        self.assertTrue(dest.exists())
        for copy in (source, StateStorage(dest)):
            with copy.transaction() as conn:
                self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 9)
                self.assertIsNone(conn.execute("SELECT name FROM sqlite_master WHERE name='task_associations'").fetchone())
        original_backup = dest.read_bytes()
        with self.assertRaises(StateStorageError):
            source.migrate_to_v10(dest, confirmed=True)
        self.assertEqual(dest.read_bytes(), original_backup)
        with source.transaction() as conn:
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 9)

    def test_v10_migration_checks_newly_recognized_operation_types(self):
        source = StateStorage(self.directory / "v9.sqlite3")
        with source.transaction() as conn:
            conn.execute("INSERT INTO state_operations VALUES (?,?,?,?,?)", ("a"*32, "collision", b"x"*32, "task_association", "f"*32))
        backup = self.directory / "reserved-v9.sqlite3"
        with self.assertRaises(StateStorageError):
            source.migrate_to_v10(backup, confirmed=True)
        self.assertTrue(backup.exists())
        for checked in (source, StateStorage(backup)):
            with checked.transaction() as conn:
                self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 9)
                self.assertEqual(conn.execute("SELECT count(*) FROM state_operations WHERE operation_key='collision'").fetchone()[0], 1)

    def test_migration_refuses_inconsistent_v9_before_backup(self):
        source = StateStorage(self.directory / "v9.sqlite3")
        with source.transaction() as conn:
            conn.execute("DELETE FROM state_operations WHERE operation_key='queue'")
            conn.execute("DELETE FROM state_audit WHERE event='queue_enqueued' AND resource_id=?", (self.item,))
        backup = self.directory / "inconsistent-v9.sqlite3"
        with self.assertRaises(StateStorageError):
            source.migrate_to_v10(backup, confirmed=True)
        self.assertFalse(backup.exists())
        with source.transaction() as conn:
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 9)

    def test_wal_migration_preserves_active_lease_and_retry(self):
        source = StateStorage(self.directory / "v9.sqlite3")
        queue = TaskQueue(self.auth, source)
        lease = queue.claim(self.alice, self.item, operation_key="v9-claim", lease_seconds=60)
        with closing(sqlite3.connect(source.path)) as conn:
            self.assertEqual(conn.execute("PRAGMA journal_mode=WAL").fetchone()[0].lower(), "wal")
        backup = self.directory / "wal-v9.sqlite3"
        source.migrate_to_v10(backup, confirmed=True)
        reopened = TaskQueue(self.auth, StateStorage(source.path))
        self.assertEqual(reopened.claim(self.alice, self.item, operation_key="v9-claim", lease_seconds=60), lease)
        self.assertTrue(reopened.get_lease(self.alice, lease)["active"])
        with StateStorage(backup).transaction() as conn:
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 9)
            self.assertEqual(conn.execute("SELECT current_lease_id FROM task_queue WHERE id=?", (self.item,)).fetchone()[0], lease)

    def test_migration_child_crash_preserves_v9_backup(self):
        source = self.directory / "v9.sqlite3"
        dest = self.directory / "crashed-v9.sqlite3"
        code = r"""
import os, sys
from pathlib import Path
from ai_earn import state_storage as s
original = s._apply_v10
def crash(conn):
    original(conn)
    os._exit(73)
s._apply_v10 = crash
s.StateStorage(Path(sys.argv[1])).migrate_to_v10(Path(sys.argv[2]), confirmed=True)
"""
        result = subprocess.run([sys.executable, "-c", code, str(source), str(dest)], capture_output=True, timeout=25)
        self.assertEqual(result.returncode, 73)
        restored = self.directory / "crash-restore.sqlite3"
        StateStorage(dest).backup_to(restored, confirmed=True)
        with StateStorage(restored).transaction() as conn:
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 9)
            self.assertEqual(conn.execute("SELECT count(*) FROM task_queue WHERE id=?", (self.item,)).fetchone()[0], 1)

    def test_prior_services_work_in_v10(self):
        from ai_earn.opportunity_drafts import OpportunityDrafts
        from ai_earn.task_plans import TaskPlans
        opportunity = OpportunityDrafts(self.auth, self.state).create(self.alice, "Synthetic", operation_key="opp")
        plan = TaskPlans(self.auth, self.state).create(self.alice, "Synthetic", operation_key="plan")
        self.assertEqual(OpportunityDrafts(self.auth, self.state).get(self.alice, opportunity)["status"], "draft")
        self.assertEqual(TaskPlans(self.auth, self.state).get(self.alice, plan)["status"], "planned")
        self.assertEqual(OrderDrafts(self.auth, self.state).get(self.alice, self.order)["proposed_amount_paise"], 123)

    def test_missing_parent_refused_for_reads_and_retries(self):
        ident = self.create()
        with closing(sqlite3.connect(self.state.path)) as conn, conn:
            conn.execute("PRAGMA foreign_keys=OFF")
            conn.execute("DELETE FROM task_workflows WHERE id=?", (self.workflow,))
        with self.assertRaises(StateStorageError):
            self.associations.get(self.alice, ident)
        with self.assertRaises(StateStorageError):
            self.associations.inspect(self.alice, ident)
        with self.assertRaises(StateStorageError):
            self.create()
        with self.assertRaises(AccessDenied):
            self.associations.get(self.bob, ident)
        with self.assertRaises(AccessDenied):
            self.associations.inspect(self.bob, ident)

    def test_inconsistent_parent_queue_is_not_associated(self):
        with self.state.transaction() as conn:
            conn.execute("UPDATE task_queue SET attempts=1 WHERE id=?", (self.item,))
        with self.assertRaises(StateStorageError):
            self.create()
        with self.state.transaction() as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM task_associations").fetchone()[0], 0)

    def test_controlled_process_crashes_roll_back_or_retry_after_commit(self):
        code = r"""
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch
import json, os, sys
import ai_earn.task_associations as module
from ai_earn.auth import AuthService
from ai_earn.auth_storage import AuthStorage
from ai_earn.state_storage import StateStorage
from ai_earn.task_associations import TaskAssociations
p = json.load(sys.stdin)
auth = AuthService(AuthStorage(Path(p['auth']), create=False), lambda: 1800000000)
service = TaskAssociations(auth, StateStorage(Path(p['state'])))
def operation():
    service.create(p['token'], p['queue'], p['workflow'], operation_key=p['key'])
if p['point'] == 'before':
    original = module._remember
    def crash(*args):
        original(*args)
        os._exit(71)
    with patch.object(module, '_remember', side_effect=crash):
        operation()
else:
    original = auth.storage.transaction
    @contextmanager
    def crash():
        with original() as conn:
            yield conn
            os._exit(72)
    with patch.object(auth.storage, 'transaction', crash):
        operation()
os._exit(99)
"""
        # Use separate disposable records for the two crash points.
        second_q = self.queue.enqueue(self.alice, "Other", operation_key="crash-q")
        second_w = self.workflows.create(self.alice, "Other", operation_key="crash-w")
        for point, queue, workflow, code_expected in (("before", self.item, self.workflow, 71),
                                                       ("after", second_q, second_w, 72)):
            with self.subTest(point=point):
                key = "crash-" + point
                payload = json.dumps(dict(auth=str(self.auth.storage.path), state=str(self.state.path),
                                          token=self.alice, queue=queue, workflow=workflow, point=point, key=key))
                result = subprocess.run([sys.executable, "-c", code], input=payload, text=True,
                                        capture_output=True, timeout=25)
                self.assertEqual(result.returncode, code_expected)
                with self.state.transaction() as conn:
                    count = conn.execute("SELECT count(*) FROM state_operations WHERE operation_key=?", (key,)).fetchone()[0]
                    self.assertEqual(count, 0 if point == "before" else 1)
                # Retry the exact request with the same durable key.
                ident = TaskAssociations(self.auth, StateStorage(self.state.path)).create(self.alice, queue, workflow, operation_key=key)
                self.assertEqual(self.associations.get(self.alice, ident)["workflow_id"], workflow)
                with self.state.transaction() as conn:
                    self.assertEqual(conn.execute("SELECT count(*) FROM task_associations WHERE id=?", (ident,)).fetchone()[0], 1)
                    self.assertEqual(conn.execute("SELECT count(*) FROM state_audit WHERE event='task_association_created' AND resource_id=?", (ident,)).fetchone()[0], 1)

    def test_corrupt_copy_rejected_and_left_for_inspection(self):
        ident = self.create()
        def corrupt(source, dest):
            _copy_database(source, dest)
            dest.execute("DELETE FROM state_operations WHERE operation_key='associate'")
            dest.execute("DELETE FROM state_audit WHERE event='task_association_created' AND resource_id=?", (ident,))
            dest.commit()
        backup = self.directory / "bad-copy.sqlite3"
        with patch("ai_earn.state_storage._copy_database", side_effect=corrupt):
            with self.assertRaises(StateStorageError):
                self.state.backup_to(backup, confirmed=True)
        self.assertTrue(backup.exists())
        self.assertEqual(self.associations.get(self.alice, ident)["workflow_id"], self.workflow)

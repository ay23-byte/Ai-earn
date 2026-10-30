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
from ai_earn.task_workflows import TaskWorkflows


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

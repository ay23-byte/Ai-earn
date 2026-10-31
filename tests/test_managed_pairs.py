"""Disposable v12 managed pairs; not external execution or a security boundary."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from ai_earn.auth import AccessDenied, AuthService, SESSION_SECONDS
from ai_earn.auth_storage import AuthStorage
from ai_earn.state_storage import StateStorage, StateStorageError
from ai_earn.state_recovery_comparison import StateRecoveryComparison
from ai_earn.task_associations import AssociationError, TaskAssociations
from ai_earn.task_queue import QueueError, TaskQueue
from ai_earn.task_workflows import TaskWorkflows, WorkflowError


class Clock:
    now = 1_800_000_000
    def __call__(self):
        return self.now


@unittest.skipUnless(os.name in {"posix", "nt"}, "requires a supported private filesystem")
class ManagedPairTests(unittest.TestCase):
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
        for version in range(2, 12):
            getattr(self.state, f"migrate_to_v{version}")(self.directory / f"before-v{version}.sqlite3", confirmed=True)
        self.queue = TaskQueue(self.auth, self.state)
        self.workflows = TaskWorkflows(self.auth, self.state)
        self.associations = TaskAssociations(self.auth, self.state)
        self.item = self.queue.enqueue(self.alice, "Synthetic", operation_key="queue", backoff_seconds=0)
        self.workflow = self.workflows.create(self.alice, "Synthetic", operation_key="workflow")

    def upgrade(self):
        self.state.migrate_to_v12(self.directory / "before-v12.sqlite3", confirmed=True)

    def managed(self):
        self.upgrade()
        return self.associations.create_managed(self.alice, self.item, self.workflow, operation_key="managed")

    def test_migration_confirmation_backup_and_legacy_compatibility(self):
        target = self.directory / "before-v12.sqlite3"
        with self.assertRaises(StateStorageError):
            self.state.migrate_to_v12(target)
        self.assertFalse(target.exists())
        # An existing v11 association remains legacy and independently mutable.
        legacy = self.associations.create(self.alice, self.item, self.workflow, operation_key="legacy")
        self.upgrade()
        with StateStorage(target).transaction() as conn:
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 11)
            self.assertIsNone(conn.execute("SELECT 1 FROM sqlite_master WHERE name='managed_task_pairs'").fetchone())
        self.assertEqual(self.associations.create(self.alice, self.item, self.workflow, operation_key="legacy"), legacy)
        self.assertEqual(self.workflows.start(self.alice, self.workflow, operation_key="direct"), self.workflow)
        self.assertEqual(self.queue.claim(self.alice, self.item, operation_key="direct-claim", lease_seconds=10) != "", True)
        self.state.backup_to(self.directory / "legacy-v12.sqlite3", confirmed=True)

    def test_v11_refuses_managed_creation_without_mutation(self):
        with self.assertRaises(StateStorageError):
            self.associations.create_managed(self.alice, self.item, self.workflow, operation_key="managed")
        self.assertEqual(self.queue.get(self.alice, self.item)["status"], "queued")
        self.assertEqual(self.workflows.get(self.alice, self.workflow)["status"], "planned")

    def test_owned_pair_prevents_direct_bypass_and_completes(self):
        pair = self.managed()
        self.assertEqual(self.associations.create_managed(self.alice, self.item, self.workflow, operation_key="managed"), pair)
        for method in (
            lambda: self.queue.claim(self.alice, self.item, operation_key="direct-claim", lease_seconds=30),
            lambda: self.queue.cancel(self.alice, self.item, operation_key="direct-cancel"),
        ):
            with self.assertRaises(QueueError):
                method()
        for method in (
            lambda: self.workflows.start(self.alice, self.workflow, operation_key="direct-start"),
            lambda: self.workflows.cancel(self.alice, self.workflow, operation_key="direct-wcancel"),
            lambda: self.workflows.submit(self.alice, self.workflow, "output", operation_key="direct-submit"),
            lambda: self.workflows.review(self.alice, self.workflow, "output", confirmed=True, approved=True, operation_key="direct-review"),
        ):
            with self.assertRaises(WorkflowError):
                method()
        self.assertEqual(self.queue.ready(self.alice), [])
        self.assertTrue(self.associations.inspect(self.alice, pair)["queue_ready_at_observation"])
        lease = self.associations.start_pair(self.alice, pair, operation_key="start", lease_seconds=30)
        self.associations.submit_pair(self.alice, pair, lease, "output", operation_key="submit")
        with self.assertRaises(AssociationError):
            self.associations.review_pair(self.alice, pair, lease, "output", confirmed=True, approved=True, operation_key="review")
        self.assertEqual(self.workflows.get(self.alice, self.workflow)["status"], "pending_review")
        self.associations.complete_pair(self.alice, pair, lease, "output", confirmed=True, operation_key="complete")
        self.assertEqual(self.queue.get(self.alice, self.item)["status"], "completed")
        self.assertFalse(self.queue.get_lease(self.alice, lease)["active"])
        backup = self.directory / "managed-complete.sqlite3"
        self.state.backup_to(backup, confirmed=True)
        copy = StateStorage(backup)
        self.assertEqual(TaskAssociations(self.auth, copy).complete_pair(self.alice, pair, lease, "output", confirmed=True, operation_key="complete"), pair)
        self.assertEqual(TaskQueue(self.auth, copy).get(self.alice, self.item)["status"], "completed")
        counts = StateRecoveryComparison(self.auth, self.state, copy).compare_records(self.alice)["tables"]
        self.assertEqual(counts["managed_task_pairs"]["matching"], 1)

    def test_retry_recovery_resume_and_abandon_never_strands_pair(self):
        pair = self.managed()
        lease = self.associations.start_pair(self.alice, pair, operation_key="start", lease_seconds=5)
        with self.assertRaises(QueueError):
            self.queue.retry(self.alice, self.item, lease, operation_key="direct-retry")
        self.associations.retry_pair(self.alice, pair, lease, operation_key="retry")
        with self.assertRaises(WorkflowError):
            self.workflows.submit(self.alice, self.workflow, "output", operation_key="bypass-submit")
        self.assertEqual(self.associations.abandon_pair(self.alice, pair, confirmed=True, operation_key="abandon"), pair)
        self.assertEqual(self.queue.get(self.alice, self.item)["status"], "cancelled")
        self.assertEqual(self.workflows.get(self.alice, self.workflow)["status"], "cancelled")
        self.state.backup_to(self.directory / "abandoned.sqlite3", confirmed=True)

    def test_expired_pending_review_can_resume_or_be_abandoned_when_exhausted(self):
        self.upgrade()
        q = self.queue.enqueue(self.alice, "Once", operation_key="once-q", max_attempts=1, backoff_seconds=0)
        w = self.workflows.create(self.alice, "Once", operation_key="once-w")
        pair = self.associations.create_managed(self.alice, q, w, operation_key="once-pair")
        lease = self.associations.start_pair(self.alice, pair, operation_key="start", lease_seconds=5)
        self.associations.submit_pair(self.alice, pair, lease, "output", operation_key="submit")
        self.clock.now += 5
        with self.assertRaises(QueueError):
            self.queue.recover(self.alice, q, lease, operation_key="direct-recover")
        self.associations.recover_pair(self.alice, pair, lease, operation_key="recover")
        self.assertEqual(self.queue.get(self.alice, q)["status"], "exhausted")
        self.assertEqual(self.workflows.get(self.alice, w)["status"], "pending_review")
        self.associations.abandon_pair(self.alice, pair, confirmed=True, operation_key="abandon")
        self.assertEqual(self.queue.get(self.alice, q)["status"], "exhausted")
        self.assertEqual(self.workflows.get(self.alice, w)["status"], "cancelled")
        self.state.backup_to(self.directory / "exhausted.sqlite3", confirmed=True)

    def test_managed_creation_denials_and_conflicting_retries(self):
        self.upgrade()
        for token, q, w in ((self.bob, self.item, self.workflow), (self.alice, "f"*32, self.workflow), ("", self.item, self.workflow)):
            with self.assertRaises(AccessDenied):
                self.associations.create_managed(token, q, w, operation_key="managed")
        pair = self.associations.create_managed(self.alice, self.item, self.workflow, operation_key="managed")
        with self.assertRaises(AssociationError):
            self.associations.create(self.alice, self.item, self.workflow, operation_key="managed")
        with self.assertRaises(AssociationError):
            self.associations.create_managed(self.alice, self.item, self.workflow, operation_key="another")
        self.assertEqual(self.associations.get(self.alice, pair)["id"], pair)

    def test_creation_collision_and_failure_are_atomic(self):
        self.upgrade()
        for name, kwargs in (("_pair_child_key", {"return_value": "queue"}), ("_managed_audit", {"side_effect": RuntimeError("injected")})):
            with patch("ai_earn.task_associations." + name, **kwargs):
                with self.assertRaises((RuntimeError, StateStorageError)):
                    self.associations.create_managed(self.alice, self.item, self.workflow, operation_key="managed")
        with self.state.transaction() as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM managed_task_pairs").fetchone()[0], 0)
            self.assertEqual(conn.execute("SELECT count(*) FROM task_associations").fetchone()[0], 0)
            self.assertEqual(conn.execute("SELECT count(*) FROM state_operations WHERE operation_key='managed'").fetchone()[0], 0)

    def test_managed_transition_failures_roll_back_and_remain_recoverable(self):
        pair = self.managed()
        with patch("ai_earn.task_associations._workflow_audit", side_effect=RuntimeError("injected")):
            with self.assertRaises(RuntimeError):
                self.associations.start_pair(self.alice, pair, operation_key="start", lease_seconds=15)
        self.assertEqual(self.queue.get(self.alice, self.item)["status"], "queued")
        self.assertEqual(self.workflows.get(self.alice, self.workflow)["status"], "planned")
        lease = self.associations.start_pair(self.alice, pair, operation_key="start", lease_seconds=15)
        self.associations.submit_pair(self.alice, pair, lease, "output", operation_key="submit")
        with patch("ai_earn.task_associations._completion_audit", side_effect=RuntimeError("injected")):
            with self.assertRaises(RuntimeError):
                self.associations.complete_pair(self.alice, pair, lease, "output", confirmed=True, operation_key="complete")
        self.assertEqual(self.queue.get(self.alice, self.item)["status"], "leased")
        self.assertEqual(self.workflows.get(self.alice, self.workflow)["status"], "pending_review")
        self.associations.complete_pair(self.alice, pair, lease, "output", confirmed=True, operation_key="complete")
        self.assertEqual(self.queue.get(self.alice, self.item)["status"], "completed")
        self.state.backup_to(self.directory / "after-rollback.sqlite3", confirmed=True)

    def test_competing_calls_and_exact_historical_retries(self):
        self.upgrade()
        with ThreadPoolExecutor(max_workers=2) as pool:
            values = list(pool.map(lambda _: self.associations.create_managed(self.alice, self.item, self.workflow, operation_key="managed"), range(2)))
        self.assertEqual(values[0], values[1])
        # Exact prior creation acknowledgements do not mutate or authorize work.
        self.assertEqual(self.queue.enqueue(self.alice, "Synthetic", operation_key="queue", backoff_seconds=0), self.item)
        self.assertEqual(self.workflows.create(self.alice, "Synthetic", operation_key="workflow"), self.workflow)
        self.assertEqual(self.associations.cancel_pair(self.alice, values[0], operation_key="cancel"), values[0])
        self.assertEqual(self.associations.create_managed(self.alice, self.item, self.workflow, operation_key="managed"), values[0])
        self.state.backup_to(self.directory / "cancelled.sqlite3", confirmed=True)

    def test_session_expiry_rolls_back_and_bad_marker_is_refused(self):
        self.upgrade()
        import ai_earn.task_associations as module
        original = module._managed_audit
        def expire(*args):
            original(*args)
            self.clock.now += SESSION_SECONDS
        with patch.object(module, "_managed_audit", side_effect=expire):
            with self.assertRaises(AccessDenied):
                self.associations.create_managed(self.alice, self.item, self.workflow, operation_key="managed")
        with self.state.transaction() as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM managed_task_pairs").fetchone()[0], 0)
            self.assertEqual(conn.execute("SELECT count(*) FROM task_associations").fetchone()[0], 0)

    def test_managed_recovery_resume_rejection_and_completion(self):
        pair = self.managed()
        first = self.associations.start_pair(self.alice, pair, operation_key="start", lease_seconds=5)
        self.associations.submit_pair(self.alice, pair, first, "output", operation_key="submit")
        self.clock.now += 5
        self.associations.recover_pair(self.alice, pair, first, operation_key="recover")
        second = self.associations.resume_pair(self.alice, pair, operation_key="resume", lease_seconds=15)
        self.assertNotEqual(first, second)
        self.associations.review_pair(self.alice, pair, second, "output", approved=False, confirmed=True, operation_key="reject")
        self.associations.submit_pair(self.alice, pair, second, "new output", operation_key="submit2")
        self.associations.complete_pair(self.alice, pair, second, "new output", confirmed=True, operation_key="complete")
        self.assertEqual(self.queue.get(self.alice, self.item)["status"], "completed")
        self.state.backup_to(self.directory / "resumed.sqlite3", confirmed=True)

    def test_ready_filters_managed_before_limit_and_preserves_other_owner(self):
        self.upgrade()
        self.associations.create_managed(self.alice, self.item, self.workflow, operation_key="managed")
        normal = self.queue.enqueue(self.alice, "unmanaged", operation_key="normal", priority=1)
        bob = self.queue.enqueue(self.bob, "other owner", operation_key="bob")
        self.assertEqual([row["id"] for row in self.queue.ready(self.alice, limit=1)], [normal])
        self.assertEqual([row["id"] for row in self.queue.ready(self.bob, limit=1)], [bob])
        self.assertTrue(self.queue.claim(self.alice, normal, operation_key="normal-claim", lease_seconds=10))

    def test_no_conversion_of_legacy_or_previously_used_queue(self):
        self.upgrade()
        lease = self.queue.claim(self.alice, self.item, operation_key="old-claim", lease_seconds=5)
        self.clock.now += 5
        self.queue.recover(self.alice, self.item, lease, operation_key="old-recover")
        with self.assertRaises(AssociationError):
            self.associations.create_managed(self.alice, self.item, self.workflow, operation_key="managed")
        self.assertEqual(self.workflows.get(self.alice, self.workflow)["status"], "planned")
        legacy = self.associations.create(self.alice, self.item, self.workflow, operation_key="legacy")
        with self.assertRaises(AssociationError):
            self.associations.create_managed(self.alice, self.item, self.workflow, operation_key="managed")
        self.assertEqual(self.associations.get(self.alice, legacy)["queue_id"], self.item)

    def test_deleted_marker_with_retained_operation_fails_closed(self):
        pair = self.managed()
        with self.state.transaction() as conn:
            conn.execute("DELETE FROM managed_task_pairs WHERE id=?", (pair,))
        with self.assertRaises(StateStorageError):
            self.queue.ready(self.alice)
        with self.assertRaises(StateStorageError):
            self.queue.claim(self.alice, self.item, operation_key="direct", lease_seconds=5)
        with self.assertRaises(StateStorageError):
            self.workflows.start(self.alice, self.workflow, operation_key="direct-start")
        with self.assertRaises(StateStorageError):
            self.associations.create_managed(self.alice, self.item, self.workflow, operation_key="managed")
        with self.assertRaises(StateStorageError):
            self.state.backup_to(self.directory / "missing-marker.sqlite3", confirmed=True)

    def test_deleted_marker_and_operation_with_retained_audit_fails_closed(self):
        pair = self.managed()
        with self.state.transaction() as conn:
            conn.execute("DELETE FROM managed_task_pairs WHERE id=?", (pair,))
            conn.execute("DELETE FROM state_operations WHERE resource_type='managed_task_pair' AND resource_id=?", (pair,))
        with self.assertRaises(StateStorageError):
            self.queue.ready(self.alice)
        with self.assertRaises(StateStorageError):
            self.workflows.start(self.alice, self.workflow, operation_key="direct-start")
        with self.assertRaises(StateStorageError):
            self.state.backup_to(self.directory / "missing-marker-and-op.sqlite3", confirmed=True)

    def test_coordinated_marker_and_history_deletion_is_not_tamper_evident(self):
        pair = self.managed()
        with self.state.transaction() as conn:
            conn.execute("DELETE FROM managed_task_pairs WHERE id=?", (pair,))
            conn.execute("DELETE FROM state_operations WHERE resource_type='managed_task_pair' AND resource_id=?", (pair,))
            conn.execute("DELETE FROM state_audit WHERE resource_type='managed_task_pair' AND resource_id=?", (pair,))
        # The remaining association is indistinguishable from a legitimate
        # legacy association; this is not an OS or tamper-evidence boundary.
        self.assertTrue(self.queue.claim(self.alice, self.item, operation_key="direct", lease_seconds=5))
        self.state.backup_to(self.directory / "erased-marker.sqlite3", confirmed=True)

    def test_corrupt_managed_creation_history_refused_on_checked_copy(self):
        pair = self.managed()
        with self.state.transaction() as conn:
            conn.execute("DELETE FROM state_audit WHERE resource_type='managed_task_pair' AND resource_id=?", (pair,))
        with self.assertRaises(StateStorageError):
            self.state.backup_to(self.directory / "bad-history.sqlite3", confirmed=True)
        self.assertEqual(self.queue.get(self.alice, self.item)["status"], "queued")

    def test_reserved_v12_operation_type_collision_rolls_back_migration(self):
        with self.state.transaction() as conn:
            conn.execute("INSERT INTO state_operations VALUES (?,?,?,?,?)", ("a"*32, "collision", b"x"*32, "managed_task_pair", "f"*32))
        target = self.directory / "before-v12.sqlite3"
        with self.assertRaises(StateStorageError):
            self.state.migrate_to_v12(target, confirmed=True)
        self.assertTrue(target.exists())
        with self.state.transaction() as conn:
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 11)
            self.assertEqual(conn.execute("SELECT count(*) FROM state_operations WHERE operation_key='collision'").fetchone()[0], 1)

    def test_earlier_services_work_on_v12(self):
        from ai_earn.opportunity_drafts import OpportunityDrafts
        from ai_earn.task_plans import TaskPlans
        from ai_earn.order_drafts import OrderDrafts
        from ai_earn.invoice_drafts import InvoiceDrafts
        from ai_earn.payment_observations import PaymentObservations
        self.upgrade()
        OpportunityDrafts(self.auth, self.state).create(self.alice, "Opportunity", operation_key="opp")
        TaskPlans(self.auth, self.state).create(self.alice, "Plan", operation_key="plan")
        order = OrderDrafts(self.auth, self.state).create(self.alice, "Estimate", 100, operation_key="order")
        invoice = InvoiceDrafts(self.auth, self.state).create(self.alice, order, operation_key="invoice")
        PaymentObservations(self.auth, self.state).create(self.alice, invoice, operation_key="placeholder")
        self.state.backup_to(self.directory / "earlier-v12.sqlite3", confirmed=True)

    def test_managed_creation_and_direct_claim_compete_atomically(self):
        self.upgrade()
        def manage():
            try:
                self.associations.create_managed(self.alice, self.item, self.workflow, operation_key="managed")
                return "managed"
            except AssociationError:
                return "denied"
        def claim():
            try:
                self.queue.claim(self.alice, self.item, operation_key="direct", lease_seconds=20)
                return "claimed"
            except QueueError:
                return "denied"
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(manage), pool.submit(claim)]
            result = [future.result(timeout=15) for future in futures]
        self.assertIn(result, (["managed", "denied"], ["denied", "claimed"]))
        self.state.backup_to(self.directory / "race.sqlite3", confirmed=True)

    def test_v12_migration_crash_retains_checked_pre_migration_backup(self):
        target = self.directory / "before-v12.sqlite3"
        code = r"""
import os, sys
from pathlib import Path
from ai_earn import state_storage as s
original = s._apply_v12
def crash(conn):
    original(conn)
    os._exit(73)
s._apply_v12 = crash
s.StateStorage(Path(sys.argv[1])).migrate_to_v12(Path(sys.argv[2]), confirmed=True)
"""
        result = subprocess.run([sys.executable, "-c", code, str(self.state.path), str(target)], capture_output=True, timeout=30)
        self.assertEqual(result.returncode, 73)
        self.assertTrue(target.exists())
        for store in (self.state, StateStorage(target)):
            with store.transaction() as conn:
                self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 11)
                self.assertIsNone(conn.execute("SELECT 1 FROM sqlite_master WHERE name='managed_task_pairs'").fetchone())

    def test_ambiguous_auth_exit_can_retry_creation_exactly_once(self):
        self.upgrade()
        original = self.auth.storage.transaction
        @contextmanager
        def fail_after_commit():
            with original() as conn:
                yield conn
                raise RuntimeError("injected auth exit")
        with patch.object(self.auth.storage, "transaction", fail_after_commit):
            with self.assertRaises(RuntimeError):
                self.associations.create_managed(self.alice, self.item, self.workflow, operation_key="managed")
        pair = self.associations.create_managed(self.alice, self.item, self.workflow, operation_key="managed")
        self.assertEqual(self.associations.get(self.alice, pair)["id"], pair)
        with self.state.transaction() as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM managed_task_pairs").fetchone()[0], 1)
        self.state.backup_to(self.directory / "ambiguous.sqlite3", confirmed=True)

    def test_controlled_creation_crashes_before_and_after_state_commit(self):
        self.upgrade()
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
    original = module._managed_audit
    def crash(*args):
        original(*args)
        os._exit(73)
    module._managed_audit = crash
else:
    original = auth.storage.transaction
    @contextmanager
    def crash_after_state_commit():
        with original() as conn:
            yield conn
            os._exit(74)
    auth.storage.transaction = crash_after_state_commit
service.create_managed(p['token'], p['queue'], p['workflow'], operation_key='managed')
"""
        def run(point):
            data = dict(auth=str(self.auth.storage.path), state=str(self.state.path), token=self.alice,
                        queue=self.item, workflow=self.workflow, point=point)
            return subprocess.run([sys.executable, "-c", code], input=json.dumps(data), text=True, capture_output=True, timeout=30)
        self.assertEqual(run("before").returncode, 73)
        with self.state.transaction() as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM managed_task_pairs").fetchone()[0], 0)
            self.assertEqual(conn.execute("SELECT count(*) FROM task_associations").fetchone()[0], 0)
        self.assertEqual(run("after").returncode, 74)
        pair = self.associations.create_managed(self.alice, self.item, self.workflow, operation_key="managed")
        with self.assertRaises(QueueError):
            self.queue.claim(self.alice, self.item, operation_key="direct", lease_seconds=5)
        self.assertEqual(self.associations.inspect(self.alice, pair)["workflow_status"], "planned")
        self.state.backup_to(self.directory / "crash.sqlite3", confirmed=True)

    def test_inconsistent_managed_parent_states_fail_closed_and_backup_refuses(self):
        pair = self.managed()
        # Simulate an unsupported local database mutation that leaves a queued
        # managed pair with a cancelled workflow. No automatic repair occurs.
        with self.state.transaction() as conn:
            conn.execute("UPDATE task_workflows SET status='cancelled' WHERE id=?", (self.workflow,))
        with self.assertRaises(StateStorageError):
            self.queue.get(self.alice, self.item)
        with self.assertRaises(StateStorageError):
            self.workflows.get(self.alice, self.workflow)
        with self.assertRaises(StateStorageError):
            self.state.backup_to(self.directory / "bad-managed.sqlite3", confirmed=True)
        with self.state.transaction() as conn:
            self.assertEqual(conn.execute("SELECT status FROM task_queue WHERE id=?", (self.item,)).fetchone()[0], "queued")
            self.assertEqual(conn.execute("SELECT count(*) FROM managed_task_pairs WHERE id=?", (pair,)).fetchone()[0], 1)

    def test_migration_failure_preserves_source_and_backup(self):
        target = self.directory / "before-v12.sqlite3"
        from ai_earn.state_storage import _apply_v12
        def fail(conn):
            _apply_v12(conn)
            raise RuntimeError("injected")
        with patch("ai_earn.state_storage._apply_v12", side_effect=fail):
            with self.assertRaises(RuntimeError):
                self.state.migrate_to_v12(target, confirmed=True)
        for store in (self.state, StateStorage(target)):
            with store.transaction() as conn:
                self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 11)
                self.assertIsNone(conn.execute("SELECT 1 FROM sqlite_master WHERE name='managed_task_pairs'").fetchone())

if __name__ == "__main__":
    unittest.main()

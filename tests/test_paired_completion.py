"""Disposable v11 local task completion; no task execution or human proof."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from ai_earn.auth import AccessDenied, AuthService, SESSION_SECONDS
from ai_earn.auth_storage import AuthStorage
from ai_earn.state_storage import StateStorage, StateStorageError
from ai_earn.task_associations import AssociationError, TaskAssociations
from ai_earn.task_queue import QueueError, TaskQueue
from ai_earn.task_workflows import TaskWorkflows


class Clock:
    now = 1_800_000_000
    def __call__(self):
        return self.now


@unittest.skipUnless(os.name in {"posix", "nt"}, "requires a supported private filesystem")
class PairedCompletionTests(unittest.TestCase):
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
        for version in range(2, 11):
            getattr(self.state, f"migrate_to_v{version}")(self.directory / f"before-v{version}.sqlite3", confirmed=True)
        self.queue = TaskQueue(self.auth, self.state)
        self.workflows = TaskWorkflows(self.auth, self.state)
        self.item = self.queue.enqueue(self.alice, "Synthetic", operation_key="queue")
        self.workflow = self.workflows.create(self.alice, "Synthetic", operation_key="workflow")
        self.associations = TaskAssociations(self.auth, self.state)
        self.association = self.associations.create(self.alice, self.item, self.workflow, operation_key="association")

    def upgrade(self):
        self.state.migrate_to_v11(self.directory / "before-v11.sqlite3", confirmed=True)

    def pending(self, seconds=60):
        self.upgrade()
        lease = self.associations.start_pair(self.alice, self.association, operation_key="start", lease_seconds=seconds)
        self.associations.submit_pair(self.alice, self.association, lease, "synthetic output", operation_key="submit")
        return lease

    def test_explicit_migration_preserves_existing_data_and_backup(self):
        target = self.directory / "before-v11.sqlite3"
        with self.assertRaises(StateStorageError):
            self.state.migrate_to_v11(target)
        self.assertFalse(target.exists())
        self.upgrade()
        with StateStorage(target).transaction() as old:
            self.assertEqual(old.execute("PRAGMA user_version").fetchone()[0], 10)
            self.assertIsNone(old.execute("SELECT name FROM sqlite_master WHERE name='paired_completions'").fetchone())
        with self.state.transaction() as conn:
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 11)
        self.assertEqual(self.associations.create(self.alice, self.item, self.workflow, operation_key="association"), self.association)
        with self.assertRaises(StateStorageError):
            self.state.migrate_to_v11(target, confirmed=True)

    def test_local_completion_is_atomic_idempotent_and_blocks_recovery(self):
        lease = self.pending()
        self.assertEqual(self.associations.complete_pair(self.alice, self.association, lease, "synthetic output", confirmed=True, operation_key="complete"), self.association)
        self.assertEqual(self.associations.complete_pair(self.alice, self.association, lease, "synthetic output", confirmed=True, operation_key="complete"), self.association)
        self.assertEqual(self.workflows.get(self.alice, self.workflow)["status"], "completed")
        completed = self.queue.get(self.alice, self.item)
        self.assertEqual(completed["status"], "completed")
        self.assertIsNone(completed["current_lease_id"])
        self.assertIsNone(completed["lease_until"])
        self.assertEqual(completed["completed_at"], self.clock.now)
        self.assertFalse(self.queue.get_lease(self.alice, lease)["active"])
        self.assertEqual(self.associations.inspect(self.alice, self.association)["queue_status"], "completed")
        with self.assertRaises(QueueError):
            self.queue.retry(self.alice, self.item, lease, operation_key="retry")
        self.clock.now += 60
        with self.assertRaises(QueueError):
            self.queue.recover(self.alice, self.item, lease, operation_key="recover")
        with self.assertRaises(AssociationError):
            self.associations.recover_pair(self.alice, self.association, lease, operation_key="paired-recover")
        backup = self.directory / "completion-backup.sqlite3"
        restore = self.directory / "completion-restored.sqlite3"
        self.state.backup_to(backup, confirmed=True)
        StateStorage(backup).backup_to(restore, confirmed=True)
        restored = StateStorage(restore)
        self.assertEqual(TaskAssociations(self.auth, restored).complete_pair(self.alice, self.association, lease, "synthetic output", confirmed=True, operation_key="complete"), self.association)
        self.assertEqual(TaskQueue(self.auth, restored).get(self.alice, self.item)["status"], "completed")
        self.assertFalse(TaskQueue(self.auth, restored).get_lease(self.alice, lease)["active"])

    def test_denials_do_not_mutate_state(self):
        lease = self.pending()
        for token, ident in ((self.bob, self.association), (self.alice, "f" * 32), ("", self.association)):
            with self.assertRaises(AccessDenied):
                self.associations.complete_pair(token, ident, lease, "synthetic output", confirmed=True, operation_key="complete")
        for confirmation, output in ((False, "synthetic output"), (1, "synthetic output"), (True, "wrong output")):
            with self.assertRaises(AssociationError):
                self.associations.complete_pair(self.alice, self.association, lease, output, confirmed=confirmation, operation_key="complete")
        with self.assertRaises(AssociationError):
            self.associations.complete_pair(self.alice, self.association, "f"*32, "synthetic output", confirmed=True, operation_key="complete")
        self.assertEqual(self.workflows.get(self.alice, self.workflow)["status"], "pending_review")
        self.assertEqual(self.queue.get(self.alice, self.item)["status"], "leased")

    def test_expired_lease_and_reused_keys_fail_closed(self):
        lease = self.pending(seconds=5)
        with self.assertRaises(AssociationError):
            self.associations.complete_pair(self.alice, self.association, lease, "synthetic output", confirmed=True, operation_key="start")
        self.clock.now += 5
        with self.assertRaises(AssociationError):
            self.associations.complete_pair(self.alice, self.association, lease, "synthetic output", confirmed=True, operation_key="complete")
        self.assertEqual(self.workflows.get(self.alice, self.workflow)["status"], "pending_review")
        self.assertFalse(self.queue.get_lease(self.alice, lease)["active"])

    def test_failure_and_expiry_roll_back_approval_and_completion(self):
        lease = self.pending(seconds=60)
        with patch("ai_earn.task_associations._completion_audit", side_effect=RuntimeError("injected")):
            with self.assertRaises(RuntimeError):
                self.associations.complete_pair(self.alice, self.association, lease, "synthetic output", confirmed=True, operation_key="complete")
        import ai_earn.task_associations as module
        original = module._completion_audit
        def expire(*args):
            original(*args)
            self.clock.now += 60
        with patch.object(module, "_completion_audit", side_effect=expire):
            with self.assertRaises(AssociationError):
                self.associations.complete_pair(self.alice, self.association, lease, "synthetic output", confirmed=True, operation_key="complete")
        with self.state.transaction() as conn:
            self.assertEqual(conn.execute("SELECT status FROM task_workflows WHERE id=?", (self.workflow,)).fetchone()[0], "pending_review")
            self.assertEqual(conn.execute("SELECT count(*) FROM paired_completions").fetchone()[0], 0)
            self.assertEqual(conn.execute("SELECT count(*) FROM state_operations WHERE operation_key='complete'").fetchone()[0], 0)

    def test_competing_identical_requests_write_once(self):
        lease = self.pending()
        with ThreadPoolExecutor(max_workers=2) as pool:
            result = list(pool.map(lambda _: self.associations.complete_pair(self.alice, self.association, lease, "synthetic output", confirmed=True, operation_key="complete"), range(2)))
        self.assertEqual(result, [self.association, self.association])
        with self.state.transaction() as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM paired_completions").fetchone()[0], 1)
            self.assertEqual(conn.execute("SELECT count(*) FROM state_audit WHERE event='paired_completion_created'").fetchone()[0], 1)
            self.assertEqual(conn.execute("SELECT count(*) FROM state_audit WHERE event='workflow_approved' AND resource_id=?", (self.workflow,)).fetchone()[0], 1)

    def test_ambiguous_auth_exit_can_retry_exactly_once(self):
        lease = self.pending()
        original = self.auth.storage.transaction
        @contextmanager
        def fail_after_state_commit():
            with original() as conn:
                yield conn
                raise RuntimeError("injected auth exit")
        with patch.object(self.auth.storage, "transaction", fail_after_state_commit):
            with self.assertRaises(RuntimeError):
                self.associations.complete_pair(self.alice, self.association, lease, "synthetic output", confirmed=True, operation_key="complete")
        self.assertEqual(self.associations.complete_pair(self.alice, self.association, lease, "synthetic output", confirmed=True, operation_key="complete"), self.association)
        self.assertEqual(self.queue.get(self.alice, self.item)["status"], "completed")

    def test_migration_rolls_back_and_preserves_checked_backup(self):
        target = self.directory / "before-v11.sqlite3"
        from ai_earn.state_storage import _apply_v11
        def fail(conn):
            _apply_v11(conn)
            raise RuntimeError("injected")
        with patch("ai_earn.state_storage._apply_v11", side_effect=fail):
            with self.assertRaises(RuntimeError):
                self.state.migrate_to_v11(target, confirmed=True)
        self.assertTrue(target.exists())
        for copy in (self.state, StateStorage(target)):
            with copy.transaction() as conn:
                self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 10)
                self.assertIsNone(conn.execute("SELECT name FROM sqlite_master WHERE name='paired_completions'").fetchone())
        previous = target.read_bytes()
        with self.assertRaises(StateStorageError):
            self.state.migrate_to_v11(target, confirmed=True)
        self.assertEqual(previous, target.read_bytes())

    def test_reserved_operation_collision_rolls_back_upgrade(self):
        with self.state.transaction() as conn:
            conn.execute("INSERT INTO state_operations VALUES (?,?,?,?,?)", ("a"*32, "collision", b"x"*32, "paired_completion", "f"*32))
        target = self.directory / "before-v11.sqlite3"
        with self.assertRaises(StateStorageError):
            self.state.migrate_to_v11(target, confirmed=True)
        self.assertTrue(target.exists())
        with self.state.transaction() as conn:
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 10)
            self.assertEqual(conn.execute("SELECT count(*) FROM state_operations WHERE operation_key='collision'").fetchone()[0], 1)

    def test_previous_services_and_v11_record_comparison(self):
        from ai_earn.opportunity_drafts import OpportunityDrafts
        from ai_earn.task_plans import TaskPlans
        from ai_earn.order_drafts import OrderDrafts
        from ai_earn.invoice_drafts import InvoiceDrafts
        from ai_earn.payment_observations import PaymentObservations
        from ai_earn.state_recovery_comparison import StateRecoveryComparison
        lease = self.pending()
        OpportunityDrafts(self.auth, self.state).create(self.alice, "Synthetic", operation_key="opp")
        TaskPlans(self.auth, self.state).create(self.alice, "Synthetic", operation_key="plan")
        order = OrderDrafts(self.auth, self.state).create(self.alice, "Synthetic", 100, operation_key="order")
        invoice = InvoiceDrafts(self.auth, self.state).create(self.alice, order, operation_key="invoice")
        PaymentObservations(self.auth, self.state).create(self.alice, invoice, operation_key="placeholder")
        candidate = self.directory / "before-completion.sqlite3"
        self.state.backup_to(candidate, confirmed=True)
        self.associations.complete_pair(self.alice, self.association, lease, "synthetic output", confirmed=True, operation_key="complete")
        result = StateRecoveryComparison(self.auth, self.state, StateStorage(candidate)).compare_records(self.alice)
        self.assertEqual(result["schema_version"], 11)
        self.assertEqual(result["tables"]["paired_completions"]["current_only"], 1)
        self.assertEqual(result["tables"]["task_workflows"]["conflicts"], 1)

    def test_corrupt_completion_links_are_rejected_on_reads_and_backup(self):
        lease = self.pending()
        self.associations.complete_pair(self.alice, self.association, lease, "synthetic output", confirmed=True, operation_key="complete")
        other = self.workflows.create(self.alice, "Other", operation_key="other")
        self.workflows.start(self.alice, other, operation_key="other-start")
        self.workflows.submit(self.alice, other, "other output", operation_key="other-submit")
        self.workflows.review(self.alice, other, "other output", approved=True, confirmed=True, operation_key="other-review")
        with self.state.transaction() as conn:
            conn.execute("UPDATE paired_completions SET workflow_id=? WHERE id=?", (other, self.association))
        with self.assertRaises(StateStorageError):
            self.queue.get(self.alice, self.item)
        with self.assertRaises(StateStorageError):
            self.queue.get_lease(self.alice, lease)
        with self.assertRaises(StateStorageError):
            self.associations.inspect(self.alice, self.association)
        dest = self.directory / "bad-completion.sqlite3"
        with self.assertRaises(StateStorageError):
            self.state.backup_to(dest, confirmed=True)
        self.assertFalse(dest.exists())

    def test_completion_timestamp_outside_lease_is_rejected(self):
        lease = self.pending()
        deadline = self.queue.get_lease(self.alice, lease)["lease_until"]
        self.associations.complete_pair(self.alice, self.association, lease, "synthetic output", confirmed=True, operation_key="complete")
        with self.state.transaction() as conn:
            conn.execute("UPDATE paired_completions SET created_at=? WHERE id=?", (deadline, self.association))
        with self.assertRaises(StateStorageError):
            self.queue.get(self.alice, self.item)
        with self.assertRaises(StateStorageError):
            self.state.backup_to(self.directory / "bad-timestamp.sqlite3", confirmed=True)

    def test_corrupt_completion_cannot_be_listed_as_ready_or_newly_claimed(self):
        lease = self.pending()
        self.associations.complete_pair(self.alice, self.association, lease, "synthetic output", confirmed=True, operation_key="complete")
        # A malformed, but individually valid, queue row must not be offered or
        # re-claimed while its completion marker remains. No history is repaired.
        with self.state.transaction() as conn:
            conn.execute("UPDATE task_queue SET status='queued', current_lease_id=NULL, lease_until=NULL, available_at=? WHERE id=?", (self.clock.now, self.item))
        with self.assertRaises(StateStorageError):
            self.queue.ready(self.alice)
        with self.assertRaises(StateStorageError):
            self.queue.claim(self.alice, self.item, operation_key="new-claim", lease_seconds=5)
        bob_item = self.queue.enqueue(self.bob, "other owner", operation_key="bob-item")
        self.assertEqual([row["id"] for row in self.queue.ready(self.bob)], [bob_item])
        with self.state.transaction() as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM queue_leases WHERE queue_id=?", (self.item,)).fetchone()[0], 1)
            self.assertEqual(conn.execute("SELECT count(*) FROM state_operations WHERE operation_key='new-claim'").fetchone()[0], 0)

    def test_corrupt_completion_blocks_workflow_reads_and_mutations(self):
        lease = self.pending()
        self.associations.complete_pair(self.alice, self.association, lease, "synthetic output", confirmed=True, operation_key="complete")
        # This individually legal row conflicts with its completion marker.
        with self.state.transaction() as conn:
            conn.execute("UPDATE task_workflows SET status='in_progress', output_hash=NULL, output_size=NULL WHERE id=?", (self.workflow,))
        with self.assertRaises(StateStorageError):
            self.workflows.get(self.alice, self.workflow)
        with self.assertRaises(StateStorageError):
            self.workflows.submit(self.alice, self.workflow, "more output", operation_key="new-submit")
        with self.assertRaises(StateStorageError):
            self.workflows.cancel(self.alice, self.workflow, operation_key="new-cancel")
        other = self.workflows.create(self.bob, "other owner", operation_key="bob-workflow")
        self.assertEqual(self.workflows.get(self.bob, other)["status"], "planned")
        with self.state.transaction() as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM state_operations WHERE operation_key IN ('new-submit', 'new-cancel')").fetchone()[0], 0)

    def test_deleted_completion_creation_event_and_operation_rejected_by_backup(self):
        lease = self.pending()
        self.associations.complete_pair(self.alice, self.association, lease, "synthetic output", confirmed=True, operation_key="complete")
        with self.state.transaction() as conn:
            conn.execute("DELETE FROM state_operations WHERE operation_key='complete'")
            conn.execute("DELETE FROM state_audit WHERE event='paired_completion_created' AND resource_id=?", (self.association,))
        with self.assertRaises(StateStorageError):
            self.state.backup_to(self.directory / "missing-completion-audit.sqlite3", confirmed=True)

    def test_child_key_collision_and_conflicting_retry_fail_closed(self):
        lease = self.pending()
        with patch("ai_earn.task_associations._pair_child_key", return_value="queue"):
            with self.assertRaises(StateStorageError):
                self.associations.complete_pair(self.alice, self.association, lease, "synthetic output", confirmed=True, operation_key="complete")
        self.assertEqual(self.workflows.get(self.alice, self.workflow)["status"], "pending_review")
        self.associations.complete_pair(self.alice, self.association, lease, "synthetic output", confirmed=True, operation_key="complete")
        with self.assertRaises(AssociationError):
            self.associations.complete_pair(self.alice, self.association, lease, "different output", confirmed=True, operation_key="complete")
        with self.assertRaises(AssociationError):
            self.associations.complete_pair(self.alice, self.association, lease, "synthetic output", confirmed=True, operation_key="different-key")

    def test_previous_claim_retry_is_historical_not_active(self):
        lease = self.pending()
        self.associations.complete_pair(self.alice, self.association, lease, "synthetic output", confirmed=True, operation_key="complete")
        self.assertEqual(self.associations.start_pair(self.alice, self.association, operation_key="start", lease_seconds=60), lease)
        self.assertFalse(self.queue.get_lease(self.alice, lease)["active"])
        self.assertEqual(self.associations.submit_pair(self.alice, self.association, lease, "synthetic output", operation_key="submit"), self.workflow)
        self.assertEqual(self.queue.get(self.alice, self.item)["status"], "completed")

    def test_session_expiry_after_writes_rolls_back(self):
        lease = self.pending(seconds=3600)
        import ai_earn.task_associations as module
        original = module._completion_audit
        def expire(*args):
            original(*args)
            self.clock.now += SESSION_SECONDS
        with patch.object(module, "_completion_audit", side_effect=expire):
            with self.assertRaises(AccessDenied):
                self.associations.complete_pair(self.alice, self.association, lease, "synthetic output", confirmed=True, operation_key="complete")
        with self.state.transaction() as conn:
            self.assertEqual(conn.execute("SELECT status FROM task_workflows WHERE id=?", (self.workflow,)).fetchone()[0], "pending_review")
            self.assertEqual(conn.execute("SELECT count(*) FROM paired_completions").fetchone()[0], 0)

    def test_controlled_process_crashes_before_and_after_state_commit(self):
        lease = self.pending()
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
    original = module._completion_audit
    def crash(*args):
        original(*args)
        os._exit(73)
    module._completion_audit = crash
else:
    original = auth.storage.transaction
    @contextmanager
    def crash_after_state_commit():
        with original() as conn:
            yield conn
            os._exit(74)
    auth.storage.transaction = crash_after_state_commit
service.complete_pair(p['token'], p['association'], p['lease'], 'synthetic output', confirmed=True, operation_key='complete')
"""
        def run(point):
            return subprocess.run([sys.executable, "-c", code], input=json.dumps(dict(
                auth=str(self.auth.storage.path), state=str(self.state.path), token=self.alice,
                association=self.association, lease=lease, point=point)), text=True, capture_output=True, timeout=25)
        self.assertEqual(run("before").returncode, 73)
        self.assertEqual(self.workflows.get(self.alice, self.workflow)["status"], "pending_review")
        self.assertEqual(self.queue.get(self.alice, self.item)["status"], "leased")
        self.assertEqual(run("after").returncode, 74)
        self.assertEqual(self.associations.complete_pair(self.alice, self.association, lease, "synthetic output", confirmed=True, operation_key="complete"), self.association)
        self.assertEqual(self.queue.get(self.alice, self.item)["status"], "completed")
        self.state.backup_to(self.directory / "completion-crash-backup.sqlite3", confirmed=True)

    def test_completion_and_rejection_compete_without_partial_state(self):
        lease = self.pending()
        def complete():
            try:
                self.associations.complete_pair(self.alice, self.association, lease, "synthetic output", confirmed=True, operation_key="complete")
                return "complete"
            except AssociationError:
                return "denied"
        def reject():
            try:
                self.associations.review_pair(self.alice, self.association, lease, "synthetic output", approved=False, confirmed=True, operation_key="reject")
                return "rejected"
            except AssociationError:
                return "denied"
        with ThreadPoolExecutor(max_workers=2) as pool:
            first, second = pool.submit(complete), pool.submit(reject)
            outcome = (first.result(timeout=15), second.result(timeout=15))
        self.assertIn(outcome, (("complete", "denied"), ("denied", "rejected")))
        status = (self.queue.get(self.alice, self.item)["status"], self.workflows.get(self.alice, self.workflow)["status"])
        self.assertIn(status, (("completed", "completed"), ("leased", "in_progress")))
        self.state.backup_to(self.directory / "completion-race.sqlite3", confirmed=True)

    def test_foreign_owner_cannot_forge_completion_relations(self):
        lease = self.pending()
        bob_queue = self.queue.enqueue(self.bob, "other", operation_key="queue")
        with self.state.transaction() as conn:
            owner = conn.execute("SELECT owner_id FROM task_associations WHERE id=?", (self.association,)).fetchone()[0]
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute("INSERT INTO paired_completions VALUES (?,?,?,?,?,?)", (self.association, owner, bob_queue, self.workflow, lease, self.clock.now))
        self.assertEqual(self.workflows.get(self.alice, self.workflow)["status"], "pending_review")

    def test_corrupted_copied_completion_is_not_accepted(self):
        from ai_earn.state_storage import _copy_database
        lease = self.pending()
        self.associations.complete_pair(self.alice, self.association, lease, "synthetic output", confirmed=True, operation_key="complete")
        def corrupt(source, dest):
            _copy_database(source, dest)
            dest.execute("DELETE FROM state_audit WHERE event='paired_completion_created'")
            dest.commit()
        target = self.directory / "corrupt-copy.sqlite3"
        with patch("ai_earn.state_storage._copy_database", side_effect=corrupt):
            with self.assertRaises(StateStorageError):
                self.state.backup_to(target, confirmed=True)
        self.assertTrue(target.exists())
        self.assertEqual(self.queue.get(self.alice, self.item)["status"], "completed")
        with self.assertRaises(StateStorageError):
            StateStorage(target).backup_to(self.directory / "corrupt-restore.sqlite3", confirmed=True)

    def test_complete_marker_and_history_deletion_can_evade_checks(self):
        lease = self.pending()
        self.associations.complete_pair(self.alice, self.association, lease, "synthetic output", confirmed=True, operation_key="complete")
        with self.state.transaction() as conn:
            conn.execute("DELETE FROM paired_completions WHERE id=?", (self.association,))
            conn.execute("DELETE FROM state_operations WHERE operation_key='complete'")
            conn.execute("DELETE FROM state_audit WHERE event='paired_completion_created' AND resource_id=?", (self.association,))
        # A legitimately reviewed v10-style workflow can also be completed while
        # the queue stays leased; no tamper-evident history exists to distinguish it.
        self.state.backup_to(self.directory / "erased-completion.sqlite3", confirmed=True)
        self.assertEqual(self.queue.get(self.alice, self.item)["status"], "leased")
        self.assertEqual(self.workflows.get(self.alice, self.workflow)["status"], "completed")

    def test_v11_migration_process_crash_keeps_backup_and_source(self):
        dest = self.directory / "before-v11.sqlite3"
        code = r"""
import os, sys
from pathlib import Path
from ai_earn import state_storage as s
original = s._apply_v11
def crash(conn):
    original(conn)
    os._exit(73)
s._apply_v11 = crash
s.StateStorage(Path(sys.argv[1])).migrate_to_v11(Path(sys.argv[2]), confirmed=True)
"""
        result = subprocess.run([sys.executable, "-c", code, str(self.state.path), str(dest)], capture_output=True, timeout=25)
        self.assertEqual(result.returncode, 73)
        self.assertTrue(dest.exists())
        for copy in (self.state, StateStorage(dest)):
            with copy.transaction() as conn:
                self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 10)
                self.assertIsNone(conn.execute("SELECT name FROM sqlite_master WHERE name='paired_completions'").fetchone())

    def test_v10_still_has_no_completion_capability(self):
        lease = self.associations.start_pair(self.alice, self.association, operation_key="start", lease_seconds=60)
        self.associations.submit_pair(self.alice, self.association, lease, "synthetic output", operation_key="submit")
        with self.assertRaises(StateStorageError):
            self.associations.complete_pair(self.alice, self.association, lease, "synthetic output", confirmed=True, operation_key="complete")
        self.assertEqual(self.workflows.get(self.alice, self.workflow)["status"], "pending_review")


if __name__ == "__main__":
    unittest.main()

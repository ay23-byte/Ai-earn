"""End-to-end disposable cross-version and stale-backup rehearsal.

This does not verify real-data recovery, external work, or payment settlement.
"""
from contextlib import closing
import json
import os
import subprocess
import sys
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from ai_earn.auth import AccessDenied, AuthService
from ai_earn.auth_storage import AuthStorage
from ai_earn.invoice_drafts import InvoiceDrafts
from ai_earn.opportunity_drafts import OpportunityDrafts
from ai_earn.order_drafts import OrderDrafts
from ai_earn.payment_observations import PaymentObservations
from ai_earn.state_audit import StateAudit
from ai_earn.state_storage import StateStorage, StateStorageError
from ai_earn.state_recovery_comparison import StateRecoveryComparison
from ai_earn.task_associations import TaskAssociations
from ai_earn.task_plans import TaskPlans
from ai_earn.task_queue import TaskQueue
from ai_earn.task_workflows import TaskWorkflows


@unittest.skipUnless(os.name in {"posix", "nt"}, "requires a supported private filesystem")
class Phase3RecoveryRehearsalTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
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
        self.now = 1_800_000_000
        self.auth = AuthService(AuthStorage(self.directory / "auth.sqlite3"), lambda: self.now)
        for name in ("alice", "bobby"):
            self.auth.register(name, "disposable test password")
        self.alice = self.auth.login("alice", "disposable test password")
        self.bob = self.auth.login("bobby", "disposable test password")
        self.state = StateStorage(self.directory / "state.sqlite3", create=True, confirmed=True)

    def _upgrade(self, version):
        backup = self.directory / f"before-v{version}.sqlite3"
        getattr(self.state, f"migrate_to_v{version}")(backup, confirmed=True)
        with StateStorage(backup).transaction() as conn:
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], version - 1)
        with self.state.transaction() as conn:
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], version)

    def test_all_versions_preserve_owned_state_and_stale_backup_diverges(self):
        self._upgrade(2)
        self._upgrade(3)
        opportunities = OpportunityDrafts(self.auth, self.state)
        opportunity = opportunities.create(self.alice, "Synthetic opportunity", operation_key="opportunity")
        other_opportunity = opportunities.create(self.bob, "Other owner", operation_key="opportunity")
        opportunities.archive(self.alice, opportunity, operation_key="archive")
        self._upgrade(4)
        plans = TaskPlans(self.auth, self.state)
        plan = plans.create(self.alice, "Synthetic plan", operation_key="plan")
        plans.cancel(self.alice, plan, operation_key="cancel-plan")
        self._upgrade(5)
        orders = OrderDrafts(self.auth, self.state)
        order = orders.create(self.alice, "Synthetic estimate", 1234, operation_key="order")
        self._upgrade(6)
        invoices = InvoiceDrafts(self.auth, self.state)
        invoice = invoices.create(self.alice, order, operation_key="invoice")
        self._upgrade(7)
        queue = TaskQueue(self.auth, self.state)
        queued = queue.enqueue(self.alice, "Synthetic queue", operation_key="enqueue")
        lease = queue.claim(self.alice, queued, operation_key="claim", lease_seconds=60)
        with closing(sqlite3.connect(self.state.path)) as conn:
            self.assertEqual(conn.execute("PRAGMA journal_mode=WAL").fetchone()[0].lower(), "wal")
        self._upgrade(8)
        workflows = TaskWorkflows(self.auth, self.state)
        workflow = workflows.create(self.alice, "Synthetic review", operation_key="workflow")
        workflows.start(self.alice, workflow, operation_key="start")
        workflows.submit(self.alice, workflow, "synthetic result", operation_key="submit")
        workflows.review(self.alice, workflow, "synthetic result", approved=True, confirmed=True, operation_key="review")
        self._upgrade(9)
        observations = PaymentObservations(self.auth, self.state)
        observation = observations.create(self.alice, invoice, operation_key="observation")
        observations.flag(self.alice, observation, operation_key="flag")
        self.assertTrue(queue.get_lease(self.alice, lease)["active"])
        self.assertEqual(workflows.get(self.alice, workflow)["status"], "completed")
        # The separately reviewed workflow does not complete the queue item.
        self.assertEqual(queue.get(self.alice, queued)["status"], "leased")
        with self.assertRaises(AccessDenied):
            opportunities.get(self.bob, opportunity)
        self.assertEqual(opportunities.get(self.bob, other_opportunity)["title"], "Other owner")

        checkpoint = self.directory / "checkpoint.sqlite3"
        restored_path = self.directory / "restored.sqlite3"
        self.state.backup_to(checkpoint, confirmed=True)
        StateStorage(checkpoint).backup_to(restored_path, confirmed=True)
        restored = StateStorage(restored_path)
        restored_orders = OrderDrafts(self.auth, restored)
        restored_invoices = InvoiceDrafts(self.auth, restored)
        restored_observations = PaymentObservations(self.auth, restored)
        restored_queue = TaskQueue(self.auth, restored)
        restored_workflows = TaskWorkflows(self.auth, restored)
        with restored.transaction() as conn:
            counts_before = (conn.execute("SELECT count(*) FROM state_audit").fetchone()[0],
                             conn.execute("SELECT count(*) FROM state_operations").fetchone()[0])
        self.assertEqual(OpportunityDrafts(self.auth, restored).create(self.alice, "Synthetic opportunity", operation_key="opportunity"), opportunity)
        self.assertEqual(TaskPlans(self.auth, restored).create(self.alice, "Synthetic plan", operation_key="plan"), plan)
        self.assertEqual(restored_orders.create(self.alice, "Synthetic estimate", 1234, operation_key="order"), order)
        self.assertEqual(restored_invoices.create(self.alice, order, operation_key="invoice"), invoice)
        self.assertEqual(restored_observations.create(self.alice, invoice, operation_key="observation"), observation)
        self.assertEqual(restored_queue.claim(self.alice, queued, operation_key="claim", lease_seconds=60), lease)
        self.assertEqual(restored_workflows.review(self.alice, workflow, "synthetic result", approved=True, confirmed=True, operation_key="review"), workflow)
        self.assertEqual(StateAudit(self.auth, restored).list_owner_events(self.alice), StateAudit(self.auth, self.state).list_owner_events(self.alice))
        with restored.transaction() as conn:
            self.assertEqual(counts_before, (conn.execute("SELECT count(*) FROM state_audit").fetchone()[0],
                                             conn.execute("SELECT count(*) FROM state_operations").fetchone()[0]))

        # Both files are disposable: the restored snapshot does not know about
        # the following later operations, and cannot be safely resumed blindly.
        observations.discard(self.alice, observation, operation_key="later-discard")
        invoices.cancel(self.alice, invoice, operation_key="later-invoice-cancel")
        orders.cancel(self.alice, order, operation_key="later-order-cancel")
        self.now += 61
        queue.recover(self.alice, queued, lease, operation_key="later-recover")
        self.assertEqual(queue.get(self.alice, queued)["status"], "queued")
        self.assertEqual(restored_queue.get(self.alice, queued)["status"], "leased")
        self.assertFalse(restored_queue.get_lease(self.alice, lease)["active"])
        self.assertEqual(observations.get(self.alice, observation)["status"], "discarded")
        self.assertEqual(restored_observations.get(self.alice, observation)["status"], "flagged")
        self.assertEqual(restored_orders.get(self.alice, order)["status"], "draft")
        with self.state.transaction() as live, restored.transaction() as old:
            self.assertEqual(live.execute("SELECT count(*) FROM state_operations WHERE operation_key='later-discard'").fetchone()[0], 1)
            self.assertEqual(old.execute("SELECT count(*) FROM state_operations WHERE operation_key='later-discard'").fetchone()[0], 0)
        # Replaying an operation in the stale copy is a separate local mutation;
        # no reconciliation or exactly-once external effect is established.
        restored_observations.discard(self.alice, observation, operation_key="later-discard")
        self.assertEqual(restored_observations.get(self.alice, observation)["status"], "discarded")
        self.assertEqual(restored_invoices.get(self.alice, invoice)["status"], "draft")

    def test_v11_completion_stale_copy_and_process_restart(self):
        # All paths, users and output are synthetic. No existing file is restored.
        for version in range(2, 10):
            self._upgrade(version)
        queue = TaskQueue(self.auth, self.state)
        workflows = TaskWorkflows(self.auth, self.state)
        item = queue.enqueue(self.alice, "rehearsal queue", operation_key="item", max_attempts=1, backoff_seconds=0)
        workflow = workflows.create(self.alice, "rehearsal workflow", operation_key="workflow")
        self._upgrade(10)
        pairs = TaskAssociations(self.auth, self.state)
        association = pairs.create(self.alice, item, workflow, operation_key="pair")
        lease = pairs.start_pair(self.alice, association, operation_key="start", lease_seconds=30)
        pairs.submit_pair(self.alice, association, lease, "synthetic result", operation_key="submit")
        self._upgrade(11)
        self.assertEqual(queue.get(self.alice, item)["status"], "leased")
        self.assertEqual(workflows.get(self.alice, workflow)["status"], "pending_review")

        # Earlier schema copies cannot be silently compared or merged.
        before_v11 = StateStorage(self.directory / "before-v11.sqlite3")
        with self.assertRaises(StateStorageError):
            StateRecoveryComparison(self.auth, self.state, before_v11).compare_records(self.alice)
        checkpoint = self.directory / "v11-checkpoint.sqlite3"
        restored_path = self.directory / "v11-restored.sqlite3"
        self.state.backup_to(checkpoint, confirmed=True)
        StateStorage(checkpoint).backup_to(restored_path, confirmed=True)
        restored = StateStorage(restored_path)
        pairs.complete_pair(self.alice, association, lease, "synthetic result", confirmed=True, operation_key="complete")
        queue.enqueue(self.bob, "other owner", operation_key="other-item")
        comparison = StateRecoveryComparison(self.auth, self.state, restored)
        records = comparison.compare_records(self.alice)
        self.assertEqual(records["schema_version"], 11)
        self.assertEqual(records["tables"]["paired_completions"]["current_only"], 1)
        self.assertEqual(records["tables"]["task_workflows"]["conflicts"], 1)
        self.assertEqual(records["tables"]["task_queue"]["current_only"], 0)
        self.assertEqual(comparison.compare_records(self.bob)["tables"]["task_queue"]["current_only"], 1)
        self.assertGreaterEqual(comparison.compare(self.alice)["current_only"], 2)
        with self.assertRaises(AccessDenied):
            TaskAssociations(self.auth, restored).inspect(self.bob, association)

        # A checked completed copy can be reopened in a fresh interpreter;
        # exact retries acknowledge history, but the old lease is not active.
        completed = self.directory / "v11-completed.sqlite3"
        self.state.backup_to(completed, confirmed=True)
        script = r"""
import json, sys
from pathlib import Path
from ai_earn.auth import AuthService
from ai_earn.auth_storage import AuthStorage
from ai_earn.state_storage import StateStorage
from ai_earn.task_associations import TaskAssociations
from ai_earn.task_queue import TaskQueue
p = json.load(sys.stdin)
auth = AuthService(AuthStorage(Path(p['auth']), create=False), lambda: 1800000000)
state = StateStorage(Path(p['state']))
pair = TaskAssociations(auth, state)
queue = TaskQueue(auth, state)
assert pair.complete_pair(p['token'], p['pair'], p['lease'], 'synthetic result', confirmed=True, operation_key='complete') == p['pair']
assert queue.get(p['token'], p['item'])['status'] == 'completed'
assert not queue.get_lease(p['token'], p['lease'])['active']
"""
        result = subprocess.run([sys.executable, "-c", script], input=json.dumps({
            "auth": str(self.auth.storage.path), "state": str(completed), "token": self.alice,
            "pair": association, "lease": lease, "item": item,
        }), text=True, capture_output=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)

        # The stale snapshot cannot know completion happened. At the old lease
        # boundary it can independently exhaust and abandon the pending output.
        # This demonstrates why restoring it is not safe reconciliation.
        self.now += 30
        old_pairs = TaskAssociations(self.auth, restored)
        old_queue = TaskQueue(self.auth, restored)
        old_workflows = TaskWorkflows(self.auth, restored)
        old_pairs.recover_pair(self.alice, association, lease, operation_key="recovery")
        old_pairs.abandon_pair(self.alice, association, confirmed=True, operation_key="abandon")
        self.assertEqual(old_queue.get(self.alice, item)["status"], "exhausted")
        self.assertEqual(old_workflows.get(self.alice, workflow)["status"], "cancelled")
        self.assertEqual(queue.get(self.alice, item)["status"], "completed")
        self.assertEqual(workflows.get(self.alice, workflow)["status"], "completed")
        comparison = StateRecoveryComparison(self.auth, self.state, restored)
        self.assertGreaterEqual(comparison.compare(self.alice)["candidate_only"], 1)
        self.assertEqual(comparison.compare_records(self.alice)["tables"]["paired_completions"]["current_only"], 1)
        restored.backup_to(self.directory / "stale-divergent.sqlite3", confirmed=True)

    def test_v12_managed_pair_recovery_and_stale_retry_divergence(self):
        for version in range(2, 8):
            self._upgrade(version)
        queue = TaskQueue(self.auth, self.state)
        item = queue.enqueue(self.alice, "synthetic queue", operation_key="item", backoff_seconds=0)
        self._upgrade(8)
        workflows = TaskWorkflows(self.auth, self.state)
        workflow = workflows.create(self.alice, "synthetic workflow", operation_key="workflow")
        for version in range(9, 13):
            self._upgrade(version)
        old_version = StateStorage(self.directory / "before-v12.sqlite3")
        with self.assertRaises(StateStorageError):
            StateRecoveryComparison(self.auth, self.state, old_version).compare_records(self.alice)
        pairs = TaskAssociations(self.auth, self.state)
        pair = pairs.create_managed(self.alice, item, workflow, operation_key="pair")
        old = self.directory / "v12-stale.sqlite3"
        restore = self.directory / "v12-stale-restored.sqlite3"
        self.state.backup_to(old, confirmed=True)
        StateStorage(old).backup_to(restore, confirmed=True)
        restored = StateStorage(restore)
        original_lease = pairs.start_pair(self.alice, pair, operation_key="start", lease_seconds=30)
        pairs.submit_pair(self.alice, pair, original_lease, "synthetic output", operation_key="submit")
        pairs.complete_pair(self.alice, pair, original_lease, "synthetic output", confirmed=True, operation_key="complete")
        self.assertEqual(queue.get(self.alice, item)["status"], "completed")
        self.assertFalse(queue.get_lease(self.alice, original_lease)["active"])
        comparison = StateRecoveryComparison(self.auth, self.state, restored)
        self.assertEqual(comparison.compare_records(self.alice)["tables"]["managed_task_pairs"]["matching"], 1)
        self.assertEqual(comparison.compare_records(self.alice)["tables"]["paired_completions"]["current_only"], 1)
        self.assertGreaterEqual(comparison.compare(self.alice)["current_only"], 3)
        self.assertEqual(comparison.compare_records(self.bob)["tables"]["managed_task_pairs"]["matching"], 0)
        restored_pairs = TaskAssociations(self.auth, restored)
        self.assertEqual([row["association_id"] for row in restored_pairs.ready_managed(self.alice)], [pair])
        self.assertEqual(restored_pairs.ready_managed(self.bob), [])
        self.assertEqual(TaskQueue(self.auth, restored).ready(self.alice), [])
        # A checked stale copy has lost the later operation keys. The same
        # request can start another local lease: it must not be replayed as an
        # external effect without independent reconciliation/fencing.
        stale_lease = restored_pairs.start_pair(self.alice, pair, operation_key="start", lease_seconds=30)
        self.assertNotEqual(stale_lease, original_lease)
        self.assertTrue(TaskQueue(self.auth, restored).get_lease(self.alice, stale_lease)["active"])
        self.assertEqual(TaskQueue(self.auth, self.state).get(self.alice, item)["status"], "completed")
        self.assertGreaterEqual(comparison.compare(self.alice)["conflicts"], 1)
        restored.backup_to(self.directory / "v12-divergent.sqlite3", confirmed=True)

    def test_v12_atomic_claim_on_stale_copy_is_not_external_fencing(self):
        for version in range(2, 13):
            self._upgrade(version)
        queue = TaskQueue(self.auth, self.state)
        workflows = TaskWorkflows(self.auth, self.state)
        pairs = TaskAssociations(self.auth, self.state)
        item = queue.enqueue(self.alice, "synthetic queue", operation_key="item")
        workflow = workflows.create(self.alice, "synthetic workflow", operation_key="workflow")
        pair = pairs.create_managed(self.alice, item, workflow, operation_key="pair")
        stale_path = self.directory / "before-atomic-claim.sqlite3"
        self.state.backup_to(stale_path, confirmed=True)
        stale = StateStorage(stale_path)
        current = pairs.claim_next_managed(self.alice, lease_seconds=30, operation_key="next")
        restored = TaskAssociations(self.auth, stale).claim_next_managed(self.alice, lease_seconds=30, operation_key="next")
        self.assertEqual((current["association_id"], restored["association_id"]), (pair, pair))
        self.assertNotEqual(current["lease_id"], restored["lease_id"])
        self.assertTrue(queue.get_lease(self.alice, current["lease_id"])["active"])
        self.assertTrue(TaskQueue(self.auth, stale).get_lease(self.alice, restored["lease_id"])["active"])
        self.assertGreaterEqual(StateRecoveryComparison(self.auth, self.state, stale).compare(self.alice)["conflicts"], 1)
        stale.backup_to(self.directory / "atomic-claim-divergence.sqlite3", confirmed=True)

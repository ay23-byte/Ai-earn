"""End-to-end disposable cross-version and stale-backup rehearsal.

This does not verify real-data recovery, external work, or payment settlement.
"""
from contextlib import closing
import os
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
from ai_earn.state_storage import StateStorage
from ai_earn.task_plans import TaskPlans
from ai_earn.task_queue import TaskQueue
from ai_earn.task_workflows import TaskWorkflows


@unittest.skipUnless(os.name in {"posix", "nt"}, "requires a supported private filesystem")
class Phase3RecoveryRehearsalTests(unittest.TestCase):
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

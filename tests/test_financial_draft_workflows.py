"""Disposable draft-only coordination; no real payment or invoice issuance."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

from ai_earn.auth import AccessDenied, AuthService, SESSION_SECONDS
from ai_earn.auth_storage import AuthStorage
from ai_earn.invoice_drafts import InvoiceDrafts, InvoiceDraftError
from ai_earn.order_drafts import OrderDrafts
from ai_earn.payment_observations import PaymentObservations
from ai_earn.state_storage import StateStorage, StateStorageError


class Clock:
    now = 1_800_000_000
    def __call__(self):
        return self.now


@unittest.skipUnless(os.name in {"posix", "nt"}, "requires a supported private filesystem")
class FinancialDraftWorkflowTests(unittest.TestCase):
    def setUp(self):
        from ai_earn.financial_draft_workflows import FinancialDraftWorkflows
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
        self.orders = OrderDrafts(self.auth, self.state)
        self.invoices = InvoiceDrafts(self.auth, self.state)
        self.observations = PaymentObservations(self.auth, self.state)
        self.flows = FinancialDraftWorkflows(self.auth, self.state)
        self.order = self.orders.create(self.alice, "Synthetic scope", 12550, operation_key="order")
        self.invoice = self.invoices.create(self.alice, self.order, operation_key="invoice")
        self.observation = self.observations.create(self.alice, self.invoice, operation_key="observation")

    def test_withdraw_one_invoice_and_its_observations_without_touching_siblings(self):
        other = self.invoices.create(self.alice, self.order, operation_key="other-invoice")
        other_observation = self.observations.create(self.alice, other, operation_key="other-observation")
        self.observations.flag(self.alice, self.observation, operation_key="flag")
        self.assertEqual(self.flows.withdraw_invoice(self.alice, self.invoice, confirmed=True, operation_key="withdraw-invoice"), self.invoice)
        self.assertEqual(self.flows.withdraw_invoice(self.alice, self.invoice, confirmed=True, operation_key="withdraw-invoice"), self.invoice)
        self.assertEqual(self.invoices.get(self.alice, self.invoice)["status"], "cancelled")
        self.assertEqual(self.observations.get(self.alice, self.observation)["status"], "discarded")
        self.assertEqual(self.orders.get(self.alice, self.order)["status"], "draft")
        self.assertEqual(self.invoices.get(self.alice, other)["status"], "draft")
        self.assertEqual(self.observations.get(self.alice, other_observation)["status"], "unverified")
        backup = self.directory / "invoice-withdraw.sqlite3"
        self.state.backup_to(backup, confirmed=True)
        from ai_earn.financial_draft_workflows import FinancialDraftWorkflows
        restored = FinancialDraftWorkflows(self.auth, StateStorage(backup))
        self.assertEqual(restored.withdraw_invoice(self.alice, self.invoice, confirmed=True, operation_key="withdraw-invoice"), self.invoice)

    def test_invoice_withdraw_denial_cleanup_and_key_conflicts(self):
        from ai_earn.financial_draft_workflows import FinancialDraftError
        for value in (False, None, 1, "yes"):
            with self.assertRaises(FinancialDraftError):
                self.flows.withdraw_invoice(self.alice, self.invoice, confirmed=value, operation_key="withdraw")
        with self.assertRaises(AccessDenied):
            self.flows.withdraw_invoice(self.bob, self.invoice, confirmed=True, operation_key="withdraw")
        with self.assertRaises(FinancialDraftError):
            self.flows.withdraw_invoice(self.alice, self.invoice, confirmed=True, operation_key="invoice")
        self.invoices.cancel(self.alice, self.invoice, operation_key="old-cancel")
        self.flows.withdraw_invoice(self.alice, self.invoice, confirmed=True, operation_key="cleanup")
        self.assertEqual(self.observations.get(self.alice, self.observation)["status"], "discarded")
        with self.assertRaises(FinancialDraftError):
            self.flows.withdraw_invoice(self.alice, self.invoice, confirmed=True, operation_key="new")
        self.state.backup_to(self.directory / "invoice-cleanup.sqlite3", confirmed=True)

    def test_invoice_withdraw_rollback_expiry_and_scope_bound(self):
        import ai_earn.financial_draft_workflows as module
        with patch.object(module, "_invoice_audit", side_effect=RuntimeError("injected")):
            with self.assertRaises(RuntimeError):
                self.flows.withdraw_invoice(self.alice, self.invoice, confirmed=True, operation_key="withdraw")
        self.assertEqual(self.observations.get(self.alice, self.observation)["status"], "unverified")
        with patch.object(module, "MAX_LINKED_RECORDS", 0):
            with self.assertRaises(module.FinancialDraftError):
                self.flows.withdraw_invoice(self.alice, self.invoice, confirmed=True, operation_key="withdraw")
        original = module._invoice_audit
        def expire(*args):
            original(*args)
            self.clock.now += SESSION_SECONDS
        with patch.object(module, "_invoice_audit", side_effect=expire):
            with self.assertRaises(AccessDenied):
                self.flows.withdraw_invoice(self.alice, self.invoice, confirmed=True, operation_key="withdraw")
        self.alice = self.auth.login("alice", "disposable test password")
        self.assertEqual(self.invoices.get(self.alice, self.invoice)["status"], "draft")
        self.assertEqual(self.observations.get(self.alice, self.observation)["status"], "unverified")
        self.flows.withdraw_invoice(self.alice, self.invoice, confirmed=True, operation_key="withdraw")

    def test_invoice_withdraw_concurrent_exact_retries(self):
        with ThreadPoolExecutor(max_workers=2) as pool:
            calls = [pool.submit(self.flows.withdraw_invoice, self.alice, self.invoice, confirmed=True, operation_key="withdraw") for _ in range(2)]
            self.assertEqual([call.result(timeout=20) for call in calls], [self.invoice, self.invoice])
        self.state.backup_to(self.directory / "invoice-race.sqlite3", confirmed=True)

    def test_invoice_withdraw_detects_internal_key_collision_and_bad_history(self):
        import ai_earn.financial_draft_workflows as module
        for collision in ("withdraw", "invoice", "duplicate-child"):
            with patch.object(module, "_child_key", return_value=collision):
                with self.assertRaises(StateStorageError):
                    self.flows.withdraw_invoice(self.alice, self.invoice, confirmed=True, operation_key="withdraw")
        self.assertEqual(self.observations.get(self.alice, self.observation)["status"], "unverified")
        other = self.invoices.create(self.alice, self.order, operation_key="other")
        self.flows.withdraw_invoice(self.alice, self.invoice, confirmed=True, operation_key="withdraw")
        with self.state.transaction() as conn:
            conn.execute("UPDATE state_operations SET resource_id=? WHERE operation_key='withdraw'", (other,))
        with self.assertRaises(StateStorageError):
            self.flows.withdraw_invoice(self.alice, self.invoice, confirmed=True, operation_key="withdraw")

    def test_invoice_withdraw_rejects_corrupt_observation_and_isolates_other_owner(self):
        other = self.orders.create(self.bob, "Other", 100, operation_key="order")
        other_invoice = self.invoices.create(self.bob, other, operation_key="invoice")
        with self.state.transaction() as conn:
            conn.execute("UPDATE payment_observations SET proposed_amount_paise=1 WHERE id=?", (self.observation,))
        with self.assertRaises(StateStorageError):
            self.flows.withdraw_invoice(self.alice, self.invoice, confirmed=True, operation_key="withdraw")
        self.assertEqual(self.invoices.get(self.alice, self.invoice)["status"], "draft")
        self.flows.withdraw_invoice(self.bob, other_invoice, confirmed=True, operation_key="withdraw")
        self.assertEqual(self.orders.get(self.bob, other)["status"], "draft")

    def test_swapped_invoice_withdraw_results_are_rejected_by_snapshot_checks(self):
        other = self.invoices.create(self.alice, self.order, operation_key="other")
        self.flows.withdraw_invoice(self.alice, self.invoice, confirmed=True, operation_key="withdraw-one")
        self.flows.withdraw_invoice(self.alice, other, confirmed=True, operation_key="withdraw-two")
        with self.state.transaction() as conn:
            conn.execute("UPDATE state_operations SET resource_id=? WHERE operation_key='withdraw-one'", (other,))
            conn.execute("UPDATE state_operations SET resource_id=? WHERE operation_key='withdraw-two'", (self.invoice,))
        with self.assertRaises(StateStorageError):
            self.state.backup_to(self.directory / "swapped-withdraw.sqlite3", confirmed=True)
        with self.assertRaises(StateStorageError):
            self.flows.withdraw_invoice(self.alice, self.invoice, confirmed=True, operation_key="withdraw-one")

    def test_v10_through_v12_compatibility_and_owner_isolation(self):
        bob_order = self.orders.create(self.bob, "Private", 100, operation_key="bob-order")
        bob_invoice = self.invoices.create(self.bob, bob_order, operation_key="bob-invoice")
        bob_observation = self.observations.create(self.bob, bob_invoice, operation_key="bob-observation")
        for version in range(10, 13):
            getattr(self.state, f"migrate_to_v{version}")(self.directory / f"v{version-1}.sqlite3", confirmed=True)
            self.assertEqual(self.flows.inspect(self.alice, self.order)["order"]["status"], "draft")
        self.flows.withdraw(self.alice, self.order, confirmed=True, operation_key="withdraw")
        self.assertEqual(self.orders.get(self.bob, bob_order)["status"], "draft")
        self.assertEqual(self.invoices.get(self.bob, bob_invoice)["status"], "draft")
        self.assertEqual(self.observations.get(self.bob, bob_observation)["status"], "unverified")
        self.state.backup_to(self.directory / "v12-withdraw.sqlite3", confirmed=True)

    def test_partially_cancelled_drafts_are_cleaned_without_duplicate_events(self):
        self.invoices.cancel(self.alice, self.invoice, operation_key="cancel-invoice")
        self.flows.withdraw(self.alice, self.order, confirmed=True, operation_key="withdraw")
        self.assertEqual(self.observations.get(self.alice, self.observation)["status"], "discarded")
        with self.state.transaction() as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM state_audit WHERE resource_id=? AND event='invoice_draft_cancelled'", (self.invoice,)).fetchone()[0], 1)
        self.state.backup_to(self.directory / "partial.sqlite3", confirmed=True)

    def test_inspect_and_withdraw_all_linked_drafts_atomically(self):
        self.observations.flag(self.alice, self.observation, operation_key="flag")
        invoice2 = self.invoices.create(self.alice, self.order, operation_key="invoice2")
        observation2 = self.observations.create(self.alice, invoice2, operation_key="observation2")
        before = self.flows.inspect(self.alice, self.order)
        self.assertEqual(before["order"]["proposed_amount_paise"], 12550)
        self.assertEqual({v["id"] for v in before["invoices"]}, {self.invoice, invoice2})
        self.assertEqual({v["id"] for v in before["observations"]}, {self.observation, observation2})
        self.assertEqual(self.flows.withdraw(self.alice, self.order, confirmed=True, operation_key="withdraw"), self.order)
        self.assertEqual(self.orders.get(self.alice, self.order)["status"], "cancelled")
        self.assertEqual(self.invoices.get(self.alice, self.invoice)["status"], "cancelled")
        self.assertEqual(self.invoices.get(self.alice, invoice2)["status"], "cancelled")
        self.assertEqual(self.observations.get(self.alice, self.observation)["status"], "discarded")
        self.assertEqual(self.observations.get(self.alice, observation2)["status"], "discarded")
        self.assertEqual(self.flows.withdraw(self.alice, self.order, confirmed=True, operation_key="withdraw"), self.order)
        backup = self.directory / "financial-backup.sqlite3"
        self.state.backup_to(backup, confirmed=True)
        from ai_earn.financial_draft_workflows import FinancialDraftWorkflows
        restored = FinancialDraftWorkflows(self.auth, StateStorage(backup))
        self.assertEqual(restored.withdraw(self.alice, self.order, confirmed=True, operation_key="withdraw"), self.order)
        self.assertEqual(restored.inspect(self.alice, self.order)["order"]["status"], "cancelled")

    def test_previous_independent_parent_cancellation_can_be_cleaned_without_duplicate_cancellation(self):
        self.orders.cancel(self.alice, self.order, operation_key="old-cancel")
        self.flows.withdraw(self.alice, self.order, confirmed=True, operation_key="cleanup")
        self.assertEqual(self.observations.get(self.alice, self.observation)["status"], "discarded")
        with self.state.transaction() as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM state_audit WHERE resource_id=? AND event='order_draft_cancelled'", (self.order,)).fetchone()[0], 1)
        self.state.backup_to(self.directory / "cleaned.sqlite3", confirmed=True)

    def test_confirmation_owner_conflicting_keys_and_invalid_inputs(self):
        from ai_earn.financial_draft_workflows import FinancialDraftError
        for confirmed in (False, None, 1, "yes"):
            with self.assertRaises(FinancialDraftError):
                self.flows.withdraw(self.alice, self.order, confirmed=confirmed, operation_key="withdraw")
        for action in (lambda: self.flows.inspect(self.bob, self.order),
                       lambda: self.flows.withdraw(self.bob, self.order, confirmed=True, operation_key="withdraw"),
                       lambda: self.flows.inspect("", self.order)):
            with self.assertRaises(AccessDenied):
                action()
        for action in (lambda: self.flows.inspect(self.alice, "bad"),
                       lambda: self.flows.withdraw(self.alice, self.order, confirmed=True, operation_key=""),
                       lambda: self.flows.withdraw(self.alice, self.order, confirmed=True, operation_key="invoice")):
            with self.assertRaises(FinancialDraftError):
                action()
        self.assertEqual(self.orders.get(self.alice, self.order)["status"], "draft")

    def test_snapshot_corruption_fails_closed_and_other_owner_isolated(self):
        bob_order = self.orders.create(self.bob, "Bob", 0, operation_key="bob-order")
        with self.state.transaction() as conn:
            conn.execute("UPDATE payment_observations SET proposed_amount_paise=1 WHERE id=?", (self.observation,))
        for method in (lambda: self.flows.inspect(self.alice, self.order),
                       lambda: self.flows.withdraw(self.alice, self.order, confirmed=True, operation_key="withdraw")):
            with self.assertRaises(StateStorageError):
                method()
        self.assertEqual(self.flows.inspect(self.bob, bob_order)["order"]["status"], "draft")
        self.assertEqual(self.orders.get(self.alice, self.order)["status"], "draft")

    def test_rollback_and_session_expiry_leave_no_partial_changes(self):
        import ai_earn.financial_draft_workflows as module
        with patch.object(module, "_invoice_audit", side_effect=RuntimeError("injected")):
            with self.assertRaises(RuntimeError):
                self.flows.withdraw(self.alice, self.order, confirmed=True, operation_key="withdraw")
        self.assertEqual(self.observations.get(self.alice, self.observation)["status"], "unverified")
        original = module._invoice_audit
        def expire(*args):
            original(*args)
            self.clock.now += SESSION_SECONDS
        with patch.object(module, "_invoice_audit", side_effect=expire):
            with self.assertRaises(AccessDenied):
                self.flows.withdraw(self.alice, self.order, confirmed=True, operation_key="withdraw")
        self.alice = self.auth.login("alice", "disposable test password")
        self.assertEqual(self.orders.get(self.alice, self.order)["status"], "draft")
        self.assertEqual(self.invoices.get(self.alice, self.invoice)["status"], "draft")
        self.assertEqual(self.observations.get(self.alice, self.observation)["status"], "unverified")
        self.flows.withdraw(self.alice, self.order, confirmed=True, operation_key="withdraw")

    def test_version_bound_scope_and_internal_key_collisions_fail_closed(self):
        import ai_earn.financial_draft_workflows as module
        old = StateStorage(self.directory / "v8.sqlite3")
        old_order = OrderDrafts(self.auth, old).create(self.alice, "old", 1, operation_key="old")
        old_flows = module.FinancialDraftWorkflows(self.auth, old)
        with self.assertRaises(StateStorageError):
            old_flows.inspect(self.alice, old_order)
        with self.assertRaises(StateStorageError):
            old_flows.withdraw(self.alice, old_order, confirmed=True, operation_key="key")
        with patch.object(module, "MAX_LINKED_RECORDS", 1):
            with self.assertRaises(module.FinancialDraftError):
                self.flows.inspect(self.alice, self.order)
            with self.assertRaises(module.FinancialDraftError):
                self.flows.withdraw(self.alice, self.order, confirmed=True, operation_key="withdraw")
        for collision in ("withdraw", "invoice", "same-generated-key"):
            with patch.object(module, "_child_key", return_value=collision):
                with self.assertRaises(StateStorageError):
                    self.flows.withdraw(self.alice, self.order, confirmed=True, operation_key="withdraw")
        self.assertEqual(self.orders.get(self.alice, self.order)["status"], "draft")
        self.assertEqual(self.observations.get(self.alice, self.observation)["status"], "unverified")

    def test_historical_retry_rejects_swapped_result_or_inconsistent_snapshot(self):
        order2 = self.orders.create(self.alice, "Other", 500, operation_key="order2")
        self.flows.withdraw(self.alice, self.order, confirmed=True, operation_key="withdraw")
        self.flows.withdraw(self.alice, order2, confirmed=True, operation_key="withdraw2")
        with self.state.transaction() as conn:
            conn.execute("UPDATE state_operations SET resource_id=? WHERE operation_key='withdraw'", (order2,))
        with self.assertRaises(StateStorageError):
            self.flows.withdraw(self.alice, self.order, confirmed=True, operation_key="withdraw")
        with self.state.transaction() as conn:
            conn.execute("UPDATE state_operations SET resource_id=? WHERE operation_key='withdraw'", (self.order,))
            conn.execute("UPDATE payment_observations SET proposed_amount_paise=1 WHERE id=?", (self.observation,))
        with self.assertRaises(StateStorageError):
            self.flows.withdraw(self.alice, self.order, confirmed=True, operation_key="withdraw")

    def test_exact_concurrent_retries_and_noop_new_key(self):
        from ai_earn.financial_draft_workflows import FinancialDraftError
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(self.flows.withdraw, self.alice, self.order, confirmed=True, operation_key="withdraw") for _ in range(2)]
            self.assertEqual([f.result(timeout=20) for f in futures], [self.order, self.order])
        with self.assertRaises(FinancialDraftError):
            self.flows.withdraw(self.alice, self.order, confirmed=True, operation_key="new")
        self.state.backup_to(self.directory / "concurrent.sqlite3", confirmed=True)

    def test_inspection_rechecks_session_before_return(self):
        import ai_earn.financial_draft_workflows as module
        original = module._linked
        def expire(*args):
            result = original(*args)
            self.clock.now += SESSION_SECONDS
            return result
        with patch.object(module, "_linked", side_effect=expire):
            with self.assertRaises(AccessDenied):
                self.flows.inspect(self.alice, self.order)

    def test_stale_copy_can_repeat_local_withdrawal_without_proving_external_safety(self):
        from ai_earn.financial_draft_workflows import FinancialDraftWorkflows
        from ai_earn.state_recovery_comparison import StateRecoveryComparison
        stale_path = self.directory / "stale.sqlite3"
        self.state.backup_to(stale_path, confirmed=True)
        self.flows.withdraw(self.alice, self.order, confirmed=True, operation_key="withdraw")
        stale = StateStorage(stale_path)
        comparison = StateRecoveryComparison(self.auth, self.state, stale)
        self.assertGreater(comparison.compare(self.alice)["current_only"], 0)
        copied_flows = FinancialDraftWorkflows(self.auth, stale)
        self.assertEqual(copied_flows.withdraw(self.alice, self.order, confirmed=True, operation_key="withdraw"), self.order)
        compared = comparison.compare(self.alice)
        self.assertGreater(compared["current_only"], 0)
        self.assertGreater(compared["candidate_only"], 0)
        self.assertEqual(copied_flows.inspect(self.alice, self.order)["order"]["status"], "cancelled")

    def test_controlled_crashes_before_and_after_commit(self):
        code = r"""
from contextlib import contextmanager
from pathlib import Path
import json, os, sys
import ai_earn.financial_draft_workflows as module
from ai_earn.auth import AuthService
from ai_earn.auth_storage import AuthStorage
from ai_earn.state_storage import StateStorage
p = json.load(sys.stdin)
auth = AuthService(AuthStorage(Path(p['auth']), create=False), lambda: 1800000000)
service = module.FinancialDraftWorkflows(auth, StateStorage(Path(p['state'])))
if p['point'] == 'before':
    original = module._invoice_audit
    def crash(*args):
        original(*args)
        os._exit(73)
    module._invoice_audit = crash
else:
    original = auth.storage.transaction
    @contextmanager
    def crash_after_state_commit():
        with original() as conn:
            yield conn
            os._exit(74)
    auth.storage.transaction = crash_after_state_commit
service.withdraw(p['token'], p['order'], confirmed=True, operation_key='withdraw')
"""
        def run(point):
            data = dict(auth=str(self.auth.storage.path), state=str(self.state.path), token=self.alice, order=self.order, point=point)
            return subprocess.run([sys.executable, "-c", code], input=json.dumps(data), text=True, capture_output=True, timeout=30)
        self.assertEqual(run("before").returncode, 73)
        self.assertEqual(self.orders.get(self.alice, self.order)["status"], "draft")
        self.assertEqual(self.observations.get(self.alice, self.observation)["status"], "unverified")
        self.assertEqual(run("after").returncode, 74)
        self.assertEqual(self.flows.withdraw(self.alice, self.order, confirmed=True, operation_key="withdraw"), self.order)
        self.assertEqual(self.observations.get(self.alice, self.observation)["status"], "discarded")
        self.state.backup_to(self.directory / "crash.sqlite3", confirmed=True)

    def test_concurrent_creation_is_serialized_with_withdrawal(self):
        barrier = threading.Barrier(2)
        def create():
            barrier.wait()
            try:
                return self.invoices.create(self.alice, self.order, operation_key="late")
            except InvoiceDraftError:
                return None
        def withdraw():
            barrier.wait()
            return self.flows.withdraw(self.alice, self.order, confirmed=True, operation_key="withdraw")
        with ThreadPoolExecutor(max_workers=2) as pool:
            creating, withdrawing = pool.submit(create), pool.submit(withdraw)
            invoice = creating.result(timeout=20)
            self.assertEqual(withdrawing.result(timeout=20), self.order)
        if invoice is not None:
            self.assertEqual(self.invoices.get(self.alice, invoice)["status"], "cancelled")
        self.state.backup_to(self.directory / "race.sqlite3", confirmed=True)

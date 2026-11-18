"""Disposable owner-scoped state-copy comparison; no automatic restore."""
from concurrent.futures import ThreadPoolExecutor
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from ai_earn.auth import AccessDenied, AuthService, SESSION_SECONDS
from ai_earn.auth_storage import AuthStorage
from ai_earn.order_drafts import OrderDrafts
from ai_earn.state_recovery_comparison import StateRecoveryComparison
from ai_earn.state_storage import StateStorage, StateStorageError


class Clock:
    now = 1_800_000_000
    def __call__(self):
        return self.now


@unittest.skipUnless(os.name in {"posix", "nt"}, "requires a supported private filesystem")
class StateRecoveryComparisonTests(unittest.TestCase):
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
        self.clock = Clock()
        self.auth = AuthService(AuthStorage(self.directory / "auth.sqlite3"), self.clock)
        for name in ("alice", "bobby"):
            self.auth.register(name, "disposable test password")
        self.alice = self.auth.login("alice", "disposable test password")
        self.bob = self.auth.login("bobby", "disposable test password")
        self.current = StateStorage(self.directory / "current.sqlite3", create=True, confirmed=True)
        for version in range(2, 11):
            getattr(self.current, f"migrate_to_v{version}")(self.directory / f"v{version-1}.sqlite3", confirmed=True)
        self.orders = OrderDrafts(self.auth, self.current)
        self.order = self.orders.create(self.alice, "Synthetic", 10, operation_key="create")
        self.orders.create(self.bob, "Other owner", 20, operation_key="create")
        self.candidate_path = self.directory / "candidate.sqlite3"
        self.current.backup_to(self.candidate_path, confirmed=True)
        self.candidate = StateStorage(self.candidate_path)
        self.service = StateRecoveryComparison(self.auth, self.current, self.candidate)

    def test_identical_copies_match_without_exposing_keys_or_writing(self):
        before = self._counts()
        result = self.service.compare(self.alice)
        self.assertEqual(result, {"schema_version": 10, "matching": 1, "current_only": 0,
                                  "candidate_only": 0, "conflicts": 0})
        self.assertNotIn(self.alice, str(result))
        self.assertNotIn("create", str(result))
        self.assertNotIn(self.order, str(result))
        self.assertEqual(before, self._counts())

    def _counts(self):
        values = []
        for store in (self.current, self.candidate):
            with store.transaction() as conn:
                values.append((conn.execute("SELECT count(*) FROM state_audit").fetchone()[0],
                               conn.execute("SELECT count(*) FROM state_operations").fetchone()[0]))
        return values

    def test_divergence_and_same_key_conflict_are_counted_per_owner(self):
        self.orders.cancel(self.alice, self.order, operation_key="current-only")
        self.orders.create(self.alice, "Current", 1, operation_key="conflict")
        candidate_orders = OrderDrafts(self.auth, self.candidate)
        candidate_orders.create(self.alice, "Candidate", 2, operation_key="conflict")
        candidate_orders.create(self.alice, "Only candidate", 3, operation_key="candidate-only")
        candidate_orders.create(self.alice, "Another candidate", 4, operation_key="candidate-second")
        self.orders.create(self.bob, "Private other", 7, operation_key="other-user-only")
        self.assertEqual(self.service.compare(self.alice), {"schema_version": 10, "matching": 1,
                         "current_only": 1, "candidate_only": 2, "conflicts": 1})
        self.assertEqual(self.service.compare(self.bob), {"schema_version": 10, "matching": 1,
                         "current_only": 1, "candidate_only": 0, "conflicts": 0})
        reverse = StateRecoveryComparison(self.auth, self.candidate, self.current).compare(self.alice)
        self.assertEqual((reverse["current_only"], reverse["candidate_only"], reverse["conflicts"]), (2, 1, 1))

    def test_stale_copy_is_not_equal_after_later_mutation(self):
        self.orders.cancel(self.alice, self.order, operation_key="later-cancel")
        result = self.service.compare(self.alice)
        self.assertEqual((result["matching"], result["current_only"], result["candidate_only"], result["conflicts"]), (1, 1, 0, 0))
        self.assertEqual(OrderDrafts(self.auth, self.candidate).get(self.alice, self.order)["status"], "draft")

    def test_matching_operations_do_not_establish_record_equality(self):
        # Directly modifying the disposable copy demonstrates a known limitation.
        with self.candidate.transaction() as conn:
            conn.execute("UPDATE order_drafts SET title='Altered' WHERE id=?", (self.order,))
        result = self.service.compare(self.alice)
        self.assertEqual((result["matching"], result["current_only"], result["candidate_only"], result["conflicts"]), (1, 0, 0, 0))
        self.assertNotEqual(self.orders.get(self.alice, self.order)["title"],
                            OrderDrafts(self.auth, self.candidate).get(self.alice, self.order)["title"])

    def test_record_comparison_detects_changed_content_without_exposure_or_writes(self):
        with self.candidate.transaction() as conn:
            conn.execute("UPDATE order_drafts SET title='Altered' WHERE id=?", (self.order,))
        before = self._counts()
        result = self.service.compare_records(self.alice)
        self.assertEqual(result["schema_version"], 10)
        self.assertEqual(result["tables"]["order_drafts"], {"matching": 0, "current_only": 0, "candidate_only": 0, "conflicts": 1})
        self.assertEqual(self.service.compare(self.alice)["matching"], 1)
        for secret in (self.order, "Synthetic", "Altered", self.alice):
            self.assertNotIn(secret, str(result))
        self.assertEqual(before, self._counts())
        self.assertEqual(self.service.compare_records(self.bob)["tables"]["order_drafts"]["matching"], 1)
        self.assertEqual(self.service.compare_records(self.bob)["tables"]["order_drafts"]["conflicts"], 0)

    def test_record_comparison_counts_owner_specific_records_and_reverse(self):
        self.orders.create(self.alice, "Only current", 10, operation_key="only-current")
        OrderDrafts(self.auth, self.candidate).create(self.alice, "Only candidate", 10, operation_key="only-candidate")
        self.orders.create(self.bob, "Not Alice", 10, operation_key="bob-current")
        result = self.service.compare_records(self.alice)["tables"]["order_drafts"]
        self.assertEqual(result, {"matching": 1, "current_only": 1, "candidate_only": 1, "conflicts": 0})
        self.assertEqual(StateRecoveryComparison(self.auth, self.candidate, self.current).compare_records(self.alice)["tables"]["order_drafts"], result)
        self.assertEqual(self.service.compare_records(self.bob)["tables"]["order_drafts"]["current_only"], 1)

    def test_record_comparison_includes_all_supported_versioned_tables(self):
        expected = {"opportunity_drafts", "task_plans", "order_drafts", "invoice_drafts", "task_queue", "queue_leases", "task_workflows", "payment_observations", "task_associations"}
        tables = self.service.compare_records(self.alice)["tables"]
        self.assertEqual(set(tables), expected)
        for table in expected - {"order_drafts"}:
            self.assertEqual(tables[table], {"matching": 0, "current_only": 0, "candidate_only": 0, "conflicts": 0})
        for version in range(2, 10):
            with self.subTest(version=version):
                old = StateStorage(self.directory / f"v{version}.sqlite3")
                dest = self.directory / f"comparison-v{version}.sqlite3"
                old.backup_to(dest, confirmed=True)
                result = StateRecoveryComparison(self.auth, old, StateStorage(dest)).compare_records(self.alice)
                self.assertEqual(result["schema_version"], version)
                minima = {"opportunity_drafts": 3, "task_plans": 4, "order_drafts": 5, "invoice_drafts": 6, "task_queue": 7, "queue_leases": 7, "task_workflows": 8, "payment_observations": 9, "task_associations": 10}
                self.assertEqual(set(result["tables"]), {name for name, minimum in minima.items() if version >= minimum})

    def test_record_comparison_populated_queue_workflow_and_association(self):
        from ai_earn.task_queue import TaskQueue
        from ai_earn.task_workflows import TaskWorkflows
        from ai_earn.task_associations import TaskAssociations
        queue = TaskQueue(self.auth, self.current)
        flow = TaskWorkflows(self.auth, self.current)
        item = queue.enqueue(self.alice, "Queued", operation_key="enqueue")
        workflow = flow.create(self.alice, "Flow", operation_key="flow")
        assoc = TaskAssociations(self.auth, self.current)
        ident = assoc.create(self.alice, item, workflow, operation_key="link")
        assoc.start_pair(self.alice, ident, operation_key="start", lease_seconds=60)
        tables = self.service.compare_records(self.alice)["tables"]
        for table in ("task_queue", "queue_leases", "task_workflows", "task_associations"):
            self.assertEqual(tables[table]["current_only"], 1)
            self.assertEqual(tables[table]["matching"], 0)

    def test_record_comparison_reports_changes_across_all_populated_types(self):
        from ai_earn.invoice_drafts import InvoiceDrafts
        from ai_earn.opportunity_drafts import OpportunityDrafts
        from ai_earn.payment_observations import PaymentObservations
        from ai_earn.task_associations import TaskAssociations
        from ai_earn.task_plans import TaskPlans
        from ai_earn.task_queue import TaskQueue
        from ai_earn.task_workflows import TaskWorkflows
        opps = OpportunityDrafts(self.auth, self.current)
        plans = TaskPlans(self.auth, self.current)
        invoices = InvoiceDrafts(self.auth, self.current)
        observations = PaymentObservations(self.auth, self.current)
        queue = TaskQueue(self.auth, self.current)
        flows = TaskWorkflows(self.auth, self.current)
        associations = TaskAssociations(self.auth, self.current)
        opportunity = opps.create(self.alice, "opp", operation_key="opp")
        plan = plans.create(self.alice, "plan", operation_key="plan")
        invoice = invoices.create(self.alice, self.order, operation_key="invoice")
        observation = observations.create(self.alice, invoice, operation_key="observation")
        item = queue.enqueue(self.alice, "queue", operation_key="queue", backoff_seconds=0)
        workflow = flows.create(self.alice, "workflow", operation_key="workflow")
        association = associations.create(self.alice, item, workflow, operation_key="association")
        first_lease = associations.start_pair(self.alice, association, operation_key="start", lease_seconds=60)
        snapshot = self.directory / "all-records.sqlite3"
        self.current.backup_to(snapshot, confirmed=True)
        comparison = StateRecoveryComparison(self.auth, self.current, StateStorage(snapshot))
        self.assertTrue(all(row["conflicts"] == row["current_only"] == row["candidate_only"] == 0 for row in comparison.compare_records(self.alice)["tables"].values()))
        opps.archive(self.alice, opportunity, operation_key="archive")
        plans.cancel(self.alice, plan, operation_key="cancel-plan")
        observations.flag(self.alice, observation, operation_key="flag")
        invoices.cancel(self.alice, invoice, operation_key="cancel-invoice")
        self.orders.cancel(self.alice, self.order, operation_key="cancel-order")
        associations.submit_pair(self.alice, association, first_lease, "result", operation_key="submit")
        self.clock.now += 60
        associations.recover_pair(self.alice, association, first_lease, operation_key="recover")
        associations.resume_pair(self.alice, association, operation_key="resume", lease_seconds=60)
        second_q = queue.enqueue(self.alice, "another", operation_key="queue2")
        second_w = flows.create(self.alice, "another", operation_key="flow2")
        associations.create(self.alice, second_q, second_w, operation_key="link2")
        rows = comparison.compare_records(self.alice)["tables"]
        for table in ("opportunity_drafts", "task_plans", "order_drafts", "invoice_drafts", "payment_observations"):
            self.assertEqual(rows[table]["conflicts"], 1, table)
        self.assertEqual(rows["task_queue"]["conflicts"], 1)
        self.assertEqual(rows["task_queue"]["current_only"], 1)
        self.assertEqual(rows["task_workflows"]["conflicts"], 1)
        self.assertEqual(rows["task_workflows"]["current_only"], 1)
        self.assertEqual(rows["queue_leases"]["matching"], 1)
        self.assertEqual(rows["queue_leases"]["current_only"], 1)
        self.assertEqual(rows["task_associations"]["matching"], 1)
        self.assertEqual(rows["task_associations"]["current_only"], 1)
        self.assertTrue(all(row["candidate_only"] == row["conflicts"] == 0 for row in comparison.compare_records(self.bob)["tables"].values()))

    def test_record_comparison_refuses_missing_copy_without_recreating_it(self):
        self.candidate_path.unlink()
        with self.assertRaises(StateStorageError):
            self.service.compare_records(self.alice)
        self.assertFalse(self.candidate_path.exists())

    @unittest.skipUnless(os.name == "posix", "POSIX hard-link test")
    def test_record_comparison_refuses_hardlinked_copies(self):
        alias = self.directory / "record-alias.sqlite3"
        os.link(self.current.path, alias)
        with self.assertRaises(StateStorageError):
            StateRecoveryComparison(self.auth, self.current, StateStorage(alias)).compare_records(self.alice)

    def test_record_comparison_does_not_prove_operation_or_audit_equality(self):
        from ai_earn.invoice_drafts import InvoiceDrafts
        from ai_earn.payment_observations import PaymentObservations
        invoice = InvoiceDrafts(self.auth, self.current).create(self.alice, self.order, operation_key="invoice")
        obs = PaymentObservations(self.auth, self.current)
        ident = obs.create(self.alice, invoice, operation_key="placeholder")
        copy_path = self.directory / "matching-records.sqlite3"
        self.current.backup_to(copy_path, confirmed=True)
        obs.flag(self.alice, ident, operation_key="flag")
        obs.unflag(self.alice, ident, operation_key="unflag")
        comparison = StateRecoveryComparison(self.auth, self.current, StateStorage(copy_path))
        # Constant test clock makes updated_at equal despite the two operations.
        result = comparison.compare_records(self.alice)
        self.assertTrue(all(t["current_only"] == t["candidate_only"] == t["conflicts"] == 0 for t in result["tables"].values()))
        self.assertEqual(comparison.compare(self.alice)["current_only"], 2)

    def test_record_comparison_enforces_auth_schema_and_corruption(self):
        for token in ("", "x"*43, self.bob):
            if token == self.bob:
                self.auth.logout(self.bob)
            with self.assertRaises(AccessDenied):
                self.service.compare_records(token)
        with self.assertRaises(StateStorageError):
            StateRecoveryComparison(self.auth, self.current, self.current).compare_records(self.alice)
        with self.assertRaises(StateStorageError):
            StateRecoveryComparison(self.auth, self.current, StateStorage(self.directory / "v9.sqlite3")).compare_records(self.alice)
        with self.candidate.transaction() as conn:
            conn.execute("DELETE FROM state_operations WHERE operation_key='create' AND owner_id=(SELECT owner_id FROM order_drafts WHERE id=?)", (self.order,))
        with self.assertRaises(StateStorageError):
            self.service.compare_records(self.alice)

    def test_record_comparison_blocks_competing_writer_and_rechecks_session(self):
        from ai_earn.state_recovery_comparison import _check_integrity
        entered, release, started, done = (threading.Event() for _ in range(4))
        def hold(conn):
            _check_integrity(conn)
            if not entered.is_set():
                entered.set()
                if not release.wait(10):
                    raise RuntimeError("barrier timeout")
        def write():
            started.set()
            self.orders.cancel(self.alice, self.order, operation_key="later")
            done.set()
        with patch("ai_earn.state_recovery_comparison._check_integrity", side_effect=hold):
            with ThreadPoolExecutor(max_workers=2) as pool:
                comparison = pool.submit(self.service.compare_records, self.alice)
                try:
                    self.assertTrue(entered.wait(10))
                    writer = pool.submit(write)
                    self.assertTrue(started.wait(10))
                    self.assertFalse(done.wait(0.1))
                finally:
                    release.set()
                self.assertEqual(comparison.result(timeout=15)["tables"]["order_drafts"]["matching"], 1)
                writer.result(timeout=15)
        self.assertEqual(self.service.compare_records(self.alice)["tables"]["order_drafts"]["conflicts"], 1)
        def expire(conn):
            _check_integrity(conn)
            self.clock.now += SESSION_SECONDS
        with patch("ai_earn.state_recovery_comparison._check_integrity", side_effect=expire):
            with self.assertRaises(AccessDenied):
                self.service.compare_records(self.alice)

    def test_expired_revoked_and_invalid_sessions_are_denied(self):
        self.auth.logout(self.bob)
        for token in ("", "x"*43, self.bob):
            with self.assertRaises(AccessDenied):
                self.service.compare(token)
        self.clock.now += SESSION_SECONDS
        with self.assertRaises(AccessDenied):
            self.service.compare(self.alice)

    def test_session_expiry_during_comparison_denies_result(self):
        from ai_earn.state_recovery_comparison import _check_integrity
        calls = 0
        def expire(conn):
            nonlocal calls
            _check_integrity(conn)
            calls += 1
            if calls == 2:
                self.clock.now += SESSION_SECONDS
        with patch("ai_earn.state_recovery_comparison._check_integrity", side_effect=expire):
            with self.assertRaises(AccessDenied):
                self.service.compare(self.alice)

    def test_rejects_same_file_and_incompatible_versions(self):
        with self.assertRaises(StateStorageError):
            StateRecoveryComparison(self.auth, self.current, self.current).compare(self.alice)
        older = StateStorage(self.directory / "v9.sqlite3")
        with self.assertRaises(StateStorageError):
            StateRecoveryComparison(self.auth, self.current, older).compare(self.alice)

    @unittest.skipUnless(os.name == "posix", "POSIX hard-link test")
    def test_hardlinked_copies_are_not_treated_as_distinct(self):
        alias = self.directory / "alias.sqlite3"
        os.link(self.current.path, alias)
        with self.assertRaises(StateStorageError):
            StateRecoveryComparison(self.auth, self.current, StateStorage(alias)).compare(self.alice)

    def test_missing_copy_is_not_recreated(self):
        self.candidate_path.unlink()
        with self.assertRaises(StateStorageError):
            self.service.compare(self.alice)
        self.assertFalse(self.candidate_path.exists())
        self.assertEqual(self.orders.get(self.alice, self.order)["proposed_amount_paise"], 10)

    def test_refuses_corrupt_copy_without_repair(self):
        with self.candidate.transaction() as conn:
            conn.execute("DELETE FROM state_operations WHERE operation_key='create' AND owner_id=(SELECT owner_id FROM order_drafts WHERE id=?)", (self.order,))
            conn.execute("DELETE FROM state_audit WHERE event='order_draft_created' AND resource_id=?", (self.order,))
        with self.assertRaises(StateStorageError):
            self.service.compare(self.alice)
        with self.candidate.transaction() as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM order_drafts WHERE id=?", (self.order,)).fetchone()[0], 1)
            self.assertEqual(conn.execute("SELECT count(*) FROM state_audit WHERE resource_id=?", (self.order,)).fetchone()[0], 0)

    def test_reverse_comparisons_serialize_without_deadlock(self):
        reverse = StateRecoveryComparison(self.auth, self.candidate, self.current)
        with ThreadPoolExecutor(max_workers=2) as pool:
            a = pool.submit(self.service.compare, self.alice)
            b = pool.submit(reverse.compare, self.alice)
            self.assertEqual(a.result(timeout=15), b.result(timeout=15))

    def test_comparison_blocks_normal_writer_until_snapshot_completes(self):
        from ai_earn.state_recovery_comparison import _check_integrity
        entered, release, started, done = (threading.Event() for _ in range(4))
        def hold(conn):
            _check_integrity(conn)
            if not entered.is_set():
                entered.set()
                if not release.wait(10):
                    raise RuntimeError("barrier timeout")
        def write():
            started.set()
            self.orders.cancel(self.alice, self.order, operation_key="later")
            done.set()
        with patch("ai_earn.state_recovery_comparison._check_integrity", side_effect=hold):
            with ThreadPoolExecutor(max_workers=2) as pool:
                comparison = pool.submit(self.service.compare, self.alice)
                try:
                    self.assertTrue(entered.wait(10))
                    writer = pool.submit(write)
                    self.assertTrue(started.wait(10))
                    self.assertFalse(done.wait(0.1))
                finally:
                    release.set()
                self.assertEqual(comparison.result(timeout=15)["current_only"], 0)
                writer.result(timeout=15)
        self.assertEqual(self.service.compare(self.alice)["current_only"], 1)

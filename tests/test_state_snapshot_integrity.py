"""Disposable consistency checks for proposed amounts, never real payments."""
from contextlib import closing
import os
import sqlite3
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from ai_earn.auth import AuthService
from ai_earn.auth_storage import AuthStorage
from ai_earn.invoice_drafts import InvoiceDrafts
from ai_earn.order_drafts import OrderDrafts
from ai_earn.payment_observations import PaymentObservations
from ai_earn.state_storage import StateStorage, StateStorageError, _copy_database


@unittest.skipUnless(os.name in {"posix", "nt"}, "requires a supported private filesystem")
class SnapshotIntegrityTests(unittest.TestCase):
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
        self.auth = AuthService(AuthStorage(self.directory / "auth.sqlite3"), lambda: 1_800_000_000)
        self.auth.register("alice", "disposable test password")
        self.token = self.auth.login("alice", "disposable test password")
        self.state = StateStorage(self.directory / "state.sqlite3", create=True, confirmed=True)
        for version in range(2, 6):
            getattr(self.state, f"migrate_to_v{version}")(self.directory / f"v{version-1}.sqlite3", confirmed=True)
        self.orders = OrderDrafts(self.auth, self.state)
        self.order = self.orders.create(self.token, "Synthetic estimate", 12550, operation_key="order")
        self.state.migrate_to_v6(self.directory / "v5.sqlite3", confirmed=True)
        self.invoices = InvoiceDrafts(self.auth, self.state)
        self.invoice = self.invoices.create(self.token, self.order, operation_key="invoice")
        for version in range(7, 10):
            getattr(self.state, f"migrate_to_v{version}")(self.directory / f"v{version-1}.sqlite3", confirmed=True)
        self.observations = PaymentObservations(self.auth, self.state)
        self.observation = self.observations.create(self.token, self.invoice, operation_key="observation")

    def _change_amount(self, storage, table, ident, amount):
        with storage.transaction() as conn:
            conn.execute(f"UPDATE {table} SET proposed_amount_paise=? WHERE id=?", (amount, ident))
            self.assertEqual(conn.execute("PRAGMA quick_check").fetchone()[0], "ok")
            self.assertIsNone(conn.execute("PRAGMA foreign_key_check").fetchone())

    def test_invoice_mismatch_refused_in_v6_through_v9(self):
        for version in range(6, 10):
            with self.subTest(version=version):
                source = self.state if version == 9 else StateStorage(self.directory / f"v{version}.sqlite3")
                self._change_amount(source, "invoice_drafts", self.invoice, 12551)
                backup = self.directory / f"bad-invoice-v{version}.sqlite3"
                with self.assertRaises(StateStorageError):
                    source.backup_to(backup, confirmed=True)
                self.assertFalse(backup.exists())
                with source.transaction() as conn:
                    self.assertEqual(conn.execute("SELECT proposed_amount_paise FROM invoice_drafts WHERE id=?", (self.invoice,)).fetchone()[0], 12551)

    def test_placeholder_mismatch_refused_without_modifying_source(self):
        self._change_amount(self.state, "payment_observations", self.observation, 12551)
        backup = self.directory / "bad-placeholder.sqlite3"
        with self.assertRaises(StateStorageError):
            self.state.backup_to(backup, confirmed=True)
        self.assertFalse(backup.exists())
        with self.state.transaction() as conn:
            self.assertEqual(conn.execute("SELECT proposed_amount_paise FROM payment_observations WHERE id=?", (self.observation,)).fetchone()[0], 12551)

    def test_invoice_mismatch_is_refused_by_scoped_services_and_retries(self):
        self._change_amount(self.state, "invoice_drafts", self.invoice, 12551)
        operations = (
            lambda: self.invoices.get(self.token, self.invoice),
            lambda: self.invoices.create(self.token, self.order, operation_key="invoice"),
            lambda: self.invoices.cancel(self.token, self.invoice, operation_key="new-cancel"),
            lambda: self.observations.create(self.token, self.invoice, operation_key="new-observation"),
            lambda: self.observations.create(self.token, self.invoice, operation_key="observation"),
            lambda: self.observations.get(self.token, self.observation),
            lambda: self.observations.flag(self.token, self.observation, operation_key="new-flag"),
        )
        with self.state.transaction() as conn:
            before = (conn.execute("SELECT count(*) FROM state_audit").fetchone()[0],
                      conn.execute("SELECT count(*) FROM state_operations").fetchone()[0])
        for operation in operations:
            with self.subTest(operation=operation):
                with self.assertRaises(StateStorageError):
                    operation()
        with self.state.transaction() as conn:
            self.assertEqual(before, (conn.execute("SELECT count(*) FROM state_audit").fetchone()[0],
                                      conn.execute("SELECT count(*) FROM state_operations").fetchone()[0]))
            self.assertEqual(conn.execute("SELECT status FROM invoice_drafts WHERE id=?", (self.invoice,)).fetchone()[0], "draft")
            self.assertEqual(conn.execute("SELECT status FROM payment_observations WHERE id=?", (self.observation,)).fetchone()[0], "unverified")

    def test_placeholder_mismatch_is_refused_by_scoped_services_and_retries(self):
        self._change_amount(self.state, "payment_observations", self.observation, 12551)
        for operation in (lambda: self.observations.get(self.token, self.observation),
                          lambda: self.observations.create(self.token, self.invoice, operation_key="observation"),
                          lambda: self.observations.flag(self.token, self.observation, operation_key="flag"),
                          lambda: self.observations.discard(self.token, self.observation, operation_key="discard")):
            with self.assertRaises(StateStorageError):
                operation()
        # An unrelated new placeholder from a consistent invoice is not blocked.
        other = self.observations.create(self.token, self.invoice, operation_key="other")
        self.assertEqual(self.observations.get(self.token, other)["proposed_amount_paise"], 12550)
        self.assertEqual(self.invoices.get(self.token, self.invoice)["proposed_amount_paise"], 12550)

    def test_other_owner_cannot_distinguish_corrupt_invoice_or_placeholder(self):
        from ai_earn.auth import AccessDenied
        self.auth.register("bobby", "disposable test password")
        bob = self.auth.login("bobby", "disposable test password")
        self._change_amount(self.state, "invoice_drafts", self.invoice, 12551)
        for operation in (lambda: self.invoices.get(bob, self.invoice),
                          lambda: self.invoices.cancel(bob, self.invoice, operation_key="x"),
                          lambda: self.observations.create(bob, self.invoice, operation_key="x"),
                          lambda: self.observations.get(bob, self.observation),
                          lambda: self.observations.flag(bob, self.observation, operation_key="x")):
            with self.assertRaises(AccessDenied):
                operation()
        order = self.orders.create(bob, "Other", 42, operation_key="order")
        invoice = self.invoices.create(bob, order, operation_key="invoice")
        obs = self.observations.create(bob, invoice, operation_key="observation")
        self.assertEqual(self.observations.get(bob, obs)["proposed_amount_paise"], 42)

    def test_missing_owned_parents_fail_closed_on_scoped_operations(self):
        # Direct corruption only: application connections enforce foreign keys.
        with closing(sqlite3.connect(self.state.path)) as conn, conn:
            conn.execute("PRAGMA foreign_keys=OFF")
            conn.execute("DELETE FROM order_drafts WHERE id=?", (self.order,))
        for operation in (lambda: self.invoices.get(self.token, self.invoice),
                          lambda: self.invoices.create(self.token, self.order, operation_key="invoice"),
                          lambda: self.observations.create(self.token, self.invoice, operation_key="new"),
                          lambda: self.observations.get(self.token, self.observation),
                          lambda: self.observations.create(self.token, self.invoice, operation_key="observation")):
            with self.assertRaises(StateStorageError):
                operation()

    def test_missing_invoice_parent_and_result_fail_closed(self):
        with closing(sqlite3.connect(self.state.path)) as conn, conn:
            conn.execute("PRAGMA foreign_keys=OFF")
            conn.execute("DELETE FROM invoice_drafts WHERE id=?", (self.invoice,))
        for operation in (lambda: self.invoices.create(self.token, self.order, operation_key="invoice"),
                          lambda: self.observations.get(self.token, self.observation),
                          lambda: self.observations.flag(self.token, self.observation, operation_key="flag"),
                          lambda: self.observations.create(self.token, self.invoice, operation_key="observation")):
            with self.assertRaises(StateStorageError):
                operation()

    def test_matching_snapshots_preserve_exact_retries_after_cancellation(self):
        self.observations.flag(self.token, self.observation, operation_key="flag")
        self.invoices.cancel(self.token, self.invoice, operation_key="invoice-cancel")
        self.orders.cancel(self.token, self.order, operation_key="order-cancel")
        self.assertEqual(self.invoices.create(self.token, self.order, operation_key="invoice"), self.invoice)
        self.assertEqual(self.invoices.cancel(self.token, self.invoice, operation_key="invoice-cancel"), self.invoice)
        self.assertEqual(self.observations.create(self.token, self.invoice, operation_key="observation"), self.observation)
        self.assertEqual(self.observations.flag(self.token, self.observation, operation_key="flag"), self.observation)
        self.observations.discard(self.token, self.observation, operation_key="discard")
        self.assertEqual(self.observations.get(self.token, self.observation)["status"], "discarded")

    def test_invoice_checks_apply_to_each_supported_version(self):
        for version in (6, 7, 8, 9):
            with self.subTest(version=version):
                source = self.state if version == 9 else StateStorage(self.directory / f"v{version}.sqlite3")
                self._change_amount(source, "invoice_drafts", self.invoice, 12551)
                with self.assertRaises(StateStorageError):
                    InvoiceDrafts(self.auth, source).get(self.token, self.invoice)

    def test_copied_snapshot_mismatch_is_refused_and_left_for_inspection(self):
        for table, ident in (("invoice_drafts", self.invoice), ("payment_observations", self.observation)):
            with self.subTest(table=table):
                def corrupt(source, destination):
                    _copy_database(source, destination)
                    destination.execute(f"UPDATE {table} SET proposed_amount_paise=12551 WHERE id=?", (ident,))
                    destination.commit()
                backup = self.directory / f"copy-{table}.sqlite3"
                with patch("ai_earn.state_storage._copy_database", side_effect=corrupt):
                    with self.assertRaises(StateStorageError):
                        self.state.backup_to(backup, confirmed=True)
                self.assertTrue(backup.exists())
                with self.assertRaises(StateStorageError):
                    StateStorage(backup).backup_to(self.directory / f"restore-{table}.sqlite3", confirmed=True)
        self.assertEqual(self.invoices.get(self.token, self.invoice)["proposed_amount_paise"], 12550)
        self.assertEqual(self.observations.get(self.token, self.observation)["proposed_amount_paise"], 12550)

    def test_mismatched_v8_invoice_blocks_v9_upgrade(self):
        source = StateStorage(self.directory / "v8.sqlite3")
        self._change_amount(source, "invoice_drafts", self.invoice, 1)
        backup = self.directory / "blocked-upgrade.sqlite3"
        with self.assertRaises(StateStorageError):
            source.migrate_to_v9(backup, confirmed=True)
        self.assertFalse(backup.exists())
        with source.transaction() as conn:
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 8)
            self.assertEqual(conn.execute("SELECT proposed_amount_paise FROM invoice_drafts WHERE id=?", (self.invoice,)).fetchone()[0], 1)

    def test_post_migration_snapshot_check_rolls_back_injected_corruption(self):
        from ai_earn.state_storage import _apply_v9
        source = StateStorage(self.directory / "v8.sqlite3")
        def corrupt_during_upgrade(conn):
            _apply_v9(conn)
            invoice = conn.execute("SELECT owner_id FROM invoice_drafts WHERE id=?", (self.invoice,)).fetchone()
            conn.execute("INSERT INTO payment_observations VALUES (?,?,?,?,?,'unverified',?,?)",
                         ("f" * 32, invoice[0], self.invoice, 1, "INR", 1_800_000_000, 1_800_000_000))
        backup = self.directory / "pre-upgrade.sqlite3"
        with patch("ai_earn.state_storage._apply_v9", side_effect=corrupt_during_upgrade):
            with self.assertRaises(StateStorageError):
                source.migrate_to_v9(backup, confirmed=True)
        self.assertTrue(backup.exists())
        for checked in (source, StateStorage(backup)):
            with checked.transaction() as conn:
                self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 8)
                self.assertIsNone(conn.execute("SELECT name FROM sqlite_master WHERE name='payment_observations'").fetchone())

    def test_boundary_proposed_amounts_are_valid(self):
        for amount in (0, 1_000_000_000_000):
            with self.subTest(amount=amount):
                order = self.orders.create(self.token, "Boundary", amount, operation_key=f"order-{amount}")
                invoice = self.invoices.create(self.token, order, operation_key=f"invoice-{amount}")
                observation = self.observations.create(self.token, invoice, operation_key=f"observation-{amount}")
                self.assertEqual(self.observations.get(self.token, observation)["proposed_amount_paise"], amount)
        self.state.backup_to(self.directory / "boundaries.sqlite3", confirmed=True)

    def test_parent_cancellation_and_placeholder_flags_remain_valid(self):
        self.observations.flag(self.token, self.observation, operation_key="flag")
        self.invoices.cancel(self.token, self.invoice, operation_key="invoice-cancel")
        self.orders.cancel(self.token, self.order, operation_key="order-cancel")
        backup = self.directory / "cancelled.sqlite3"
        restored = self.directory / "cancelled-restored.sqlite3"
        self.state.backup_to(backup, confirmed=True)
        StateStorage(backup).backup_to(restored, confirmed=True)
        result = PaymentObservations(self.auth, StateStorage(restored)).get(self.token, self.observation)
        self.assertEqual((result["proposed_amount_paise"], result["status"]), (12550, "flagged"))

    def test_consistently_altered_amounts_are_not_proven_authentic(self):
        for table, ident in (("order_drafts", self.order), ("invoice_drafts", self.invoice),
                             ("payment_observations", self.observation)):
            self._change_amount(self.state, table, ident, 1)
        self.state.backup_to(self.directory / "equal-but-altered.sqlite3", confirmed=True)

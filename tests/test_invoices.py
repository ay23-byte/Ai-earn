"""Tests for v20 invoices — audit, PDF stub, 11-concept separation."""

import hashlib
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from ai_earn.auth import AuthService, AccessDenied
from ai_earn.auth_storage import AuthStorage
from ai_earn.state_storage import StateStorage, StateStorageError
from ai_earn.invoices import Invoices, InvoiceError
from ai_earn.order_drafts import OrderDrafts
from ai_earn.ledger import Ledger
from ai_earn.dashboard_metrics import DashboardMetrics
from ai_earn.state_recovery_comparison import StateRecoveryComparison


@unittest.skipUnless(os.name in {"posix", "nt"}, "requires private filesystem")
class InvoiceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
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
        self.auth.register("bob", "disposable test password")
        self.alice_token = self.auth.login("alice", "disposable test password")
        self.bob_token = self.auth.login("bob", "disposable test password")
        state = StateStorage(self.state_path, create=True, confirmed=True)
        for v in range(2, 21):
            getattr(state, f"migrate_to_v{v}")(self.directory / f"before-v{v}.sqlite3", confirmed=True)
        self.state = StateStorage(self.state_path)
        self.orders = OrderDrafts(self.auth, self.state)
        self.invoices = Invoices(self.auth, self.state)
        self.ledger = Ledger(self.auth, self.state)

    def _order(self, title="client", amount=50000, op="ok"):
        return self.orders.create(self.alice_token, title, amount, operation_key=op)

    def test_create_and_get(self):
        oid = self._order(op="k1a")
        iid = self.invoices.create(self.alice_token, oid, operation_key="k1")
        item = self.invoices.get(self.alice_token, iid)
        self.assertEqual(item["order_id"], oid)
        self.assertEqual(item["amount_paise"], 50000)
        self.assertEqual(item["currency"], "INR")
        self.assertEqual(item["status"], "draft")
        self.assertIsNone(item["pdf_hash"])
        self.assertTrue(item["invoice_number"].startswith("INV-"))
        with self.assertRaises(AccessDenied):
            self.invoices.get(self.bob_token, iid)

    def test_list_and_filter(self):
        oid1 = self._order("c1", 10000, "ok1")
        oid2 = self._order("c2", 20000, "ok2")
        i1 = self.invoices.create(self.alice_token, oid1, operation_key="k1")
        i2 = self.invoices.create(self.alice_token, oid2, operation_key="k2")
        self.invoices.issued(self.alice_token, i1, operation_key="k3")
        lst_draft = self.invoices.list(self.alice_token, status="draft")
        self.assertEqual(len(lst_draft), 1)
        self.assertEqual(lst_draft[0]["id"], i2)
        lst_issued = self.invoices.list(self.alice_token, status="issued")
        self.assertEqual(len(lst_issued), 1)
        lst_all = self.invoices.list(self.alice_token)
        self.assertEqual(len(lst_all), 2)
        lst_by_order = self.invoices.list(self.alice_token, order_id=oid1)
        self.assertEqual(len(lst_by_order), 1)
        lst_page = self.invoices.list(self.alice_token, limit=1)
        self.assertEqual(len(lst_page), 1)
        lst_next = self.invoices.list(self.alice_token, after_id=lst_page[0]["id"], limit=1)
        self.assertEqual(len(lst_next), 1)
        self.assertNotEqual(lst_page[0]["id"], lst_next[0]["id"])

    def test_lifecycle_draft_issued_paid(self):
        oid = self._order(op="ok1")
        iid = self.invoices.create(self.alice_token, oid, operation_key="k1")
        self.invoices.issued(self.alice_token, iid, operation_key="k2")
        item = self.invoices.get(self.alice_token, iid)
        self.assertEqual(item["status"], "issued")
        self.assertEqual(len(item["pdf_hash"]), 32)
        stub = self.invoices.pdf_stub(self.alice_token, iid)
        self.assertIn(b"INVOICE", stub)
        self.assertEqual(hashlib.sha256(stub).digest(), item["pdf_hash"])
        self.invoices.paid(self.alice_token, iid, operation_key="k3")
        self.assertEqual(self.invoices.get(self.alice_token, iid)["status"], "paid")
        # pdf still matches issued hash
        stub2 = self.invoices.pdf_stub(self.alice_token, iid)
        self.assertEqual(stub, stub2)
        with self.assertRaises(InvoiceError):
            self.invoices.issued(self.alice_token, iid, operation_key="k4")

    def test_lifecycle_overdue_paths(self):
        oid = self._order(op="ok1")
        iid = self.invoices.create(self.alice_token, oid, operation_key="k1")
        self.invoices.issued(self.alice_token, iid, operation_key="k2")
        self.invoices.overdue(self.alice_token, iid, operation_key="k3")
        self.assertEqual(self.invoices.get(self.alice_token, iid)["status"], "overdue")
        self.assertIsNotNone(self.invoices.pdf_stub(self.alice_token, iid))
        self.invoices.paid(self.alice_token, iid, operation_key="k4")
        self.assertEqual(self.invoices.get(self.alice_token, iid)["status"], "paid")

    def test_void_paths(self):
        # draft -> void
        oid = self._order(op="ok1")
        iid = self.invoices.create(self.alice_token, oid, operation_key="k1")
        self.invoices.voided(self.alice_token, iid, operation_key="k2")
        self.assertEqual(self.invoices.get(self.alice_token, iid)["status"], "void")
        with self.assertRaises(InvoiceError):
            self.invoices.pdf_stub(self.alice_token, iid)
        # issued -> void
        oid2 = self._order("c2", 20000, "ok2")
        iid2 = self.invoices.create(self.alice_token, oid2, operation_key="k3")
        self.invoices.issued(self.alice_token, iid2, operation_key="k4")
        self.invoices.voided(self.alice_token, iid2, operation_key="k5")
        self.assertEqual(self.invoices.get(self.alice_token, iid2)["status"], "void")
        # overdue -> void
        oid3 = self._order("c3", 30000, "ok3")
        iid3 = self.invoices.create(self.alice_token, oid3, operation_key="k6")
        self.invoices.issued(self.alice_token, iid3, operation_key="k7")
        self.invoices.overdue(self.alice_token, iid3, operation_key="k8")
        self.invoices.voided(self.alice_token, iid3, operation_key="k9")
        self.assertEqual(self.invoices.get(self.alice_token, iid3)["status"], "void")

    def test_invalid_transitions(self):
        oid = self._order(op="ok1")
        iid = self.invoices.create(self.alice_token, oid, operation_key="k1")
        with self.assertRaises(InvoiceError):
            self.invoices.paid(self.alice_token, iid, operation_key="k2")
        with self.assertRaises(InvoiceError):
            self.invoices.overdue(self.alice_token, iid, operation_key="k3")
        self.invoices.issued(self.alice_token, iid, operation_key="k4")
        # multiple invoices per order are allowed (disposable); verify second can be created
        iid_second = self.invoices.create(self.alice_token, oid, operation_key="k5")
        self.assertNotEqual(iid, iid_second)
        self.invoices.voided(self.alice_token, iid, operation_key="k6")
        with self.assertRaises(InvoiceError):
            self.invoices.paid(self.alice_token, iid, operation_key="k7")

    def test_pdf_only_issued(self):
        oid = self._order(op="ok1")
        iid = self.invoices.create(self.alice_token, oid, operation_key="k1")
        with self.assertRaises(InvoiceError):
            self.invoices.pdf_stub(self.alice_token, iid)
        self.invoices.issued(self.alice_token, iid, operation_key="k2")
        stub = self.invoices.pdf_stub(self.alice_token, iid)
        self.assertTrue(len(stub) > 0)
        # hash consistency checked inside pdf_stub

    def test_invalid_inputs(self):
        with self.assertRaises(InvoiceError):
            self.invoices.create(self.alice_token, "nothex", operation_key="k1")
        with self.assertRaises(InvoiceError):
            self.invoices.get(self.alice_token, "nothex")
        with self.assertRaises(InvoiceError):
            self.invoices.list(self.alice_token, status="invalid")
        with self.assertRaises(InvoiceError):
            self.invoices.list(self.alice_token, limit=0)
        with self.assertRaises(InvoiceError):
            self.invoices.issued(self.alice_token, "a"*32, operation_key="bad key!")
        # order not owned
        oid_bob = OrderDrafts(self.auth, self.state).create(self.bob_token, "bobclient", 10000, operation_key="kb1")
        with self.assertRaises(AccessDenied):
            self.invoices.create(self.alice_token, oid_bob, operation_key="k99")

    def test_idempotency_and_conflict(self):
        oid = self._order(op="ok1")
        iid = self.invoices.create(self.alice_token, oid, operation_key="same")
        iid2 = self.invoices.create(self.alice_token, oid, operation_key="same")
        self.assertEqual(iid, iid2)
        oid2 = self._order("c2", 12345, "ok2")
        with self.assertRaises(InvoiceError):
            self.invoices.create(self.alice_token, oid2, operation_key="same")
        # issued idempotent
        self.invoices.issued(self.alice_token, iid, operation_key="ik")
        iid_retry = self.invoices.issued(self.alice_token, iid, operation_key="ik")
        self.assertEqual(iid, iid_retry)
        with self.assertRaises(InvoiceError):
            self.invoices.paid(self.alice_token, iid, operation_key="ik")  # same key different request

    def test_concurrent_same_key(self):
        oid = self._order(op="ok1")
        results = []
        errors = []
        def attempt():
            try:
                iid = self.invoices.create(self.alice_token, oid, operation_key="concurrent")
                results.append(iid)
            except Exception as e:
                errors.append(e)
        threads = [threading.Thread(target=attempt) for _ in range(5)]
        for t in threads: t.start()
        for t in threads: t.join()
        self.assertEqual(len(results), 5)
        self.assertEqual(len(set(results)), 1)
        self.assertEqual(len(errors), 0)

    def test_isolation(self):
        oid_alice = self._order(op="ok1")
        oid_bob = OrderDrafts(self.auth, self.state).create(self.bob_token, "bobc", 50000, operation_key="okb1")
        i_alice = self.invoices.create(self.alice_token, oid_alice, operation_key="k1")
        i_bob = Invoices(self.auth, self.state).create(self.bob_token, oid_bob, operation_key="k1")
        self.assertNotEqual(i_alice, i_bob)
        self.assertEqual(len(self.invoices.list(self.alice_token)), 1)
        self.assertEqual(len(Invoices(self.auth, self.state).list(self.bob_token)), 1)
        with self.assertRaises(AccessDenied):
            self.invoices.issued(self.bob_token, i_alice, operation_key="k2")

    def test_history_tamper(self):
        oid = self._order(op="ok1")
        iid = self.invoices.create(self.alice_token, oid, operation_key="k1")
        self.invoices.issued(self.alice_token, iid, operation_key="k2")
        # Tamper: directly update status without audit
        with self.state.transaction() as conn:
            conn.execute("UPDATE invoices SET status='paid' WHERE id=?", (iid,))
        with self.assertRaises(StateStorageError):
            self.invoices.get(self.alice_token, iid)

    def test_dashboard_and_recovery(self):
        oid = self._order(op="ok1")
        iid = self.invoices.create(self.alice_token, oid, operation_key="k1")
        self.invoices.issued(self.alice_token, iid, operation_key="k2")
        metrics = DashboardMetrics(self.auth, self.state).get(self.alice_token)
        self.assertIn("invoices", metrics)
        self.assertEqual(metrics["invoices"]["count"], 1)
        self.assertIn("ledger_11", metrics)
        self.assertEqual(metrics["ledger_11"]["invoices_issued"]["count"], 1)
        self.assertEqual(metrics["ledger_11"]["payment_received_verified"]["verification"], "not_implemented")
        self.assertEqual(metrics["ledger_11"]["payment_received_verified"]["amount_paise"], 0)
        # ledger 11 includes net_realized_synthetic
        self.assertIn("net_realized_synthetic", metrics["ledger_11"])
        # recovery
        cand_path = self.directory / "cand.sqlite3"
        self.state.backup_to(cand_path, confirmed=True)
        cand = StateStorage(cand_path)
        comp = StateRecoveryComparison(self.auth, self.state, cand)
        rec = comp.compare_records(self.alice_token)
        self.assertEqual(rec["tables"]["invoices"]["matching"], 1)

    def test_ledger_eleven_concepts_separation(self):
        # Create varied data to ensure 11 buckets distinct
        oid = self._order(op="ok1")
        iid = self.invoices.create(self.alice_token, oid, operation_key="k1")
        self.invoices.issued(self.alice_token, iid, operation_key="k2")
        self.ledger.add(self.alice_token, "income", 100000, "", operation_key="lk1")
        self.ledger.add(self.alice_token, "expense", 20000, "", operation_key="lk2")
        self.ledger.add(self.alice_token, "refund", 5000, "", operation_key="lk3")
        self.ledger.add(self.alice_token, "fee", 1000, "", operation_key="lk4")
        eleven = self.ledger.eleven_concepts(self.alice_token)
        self.assertEqual(eleven["invoices_issued"]["amount_paise"], 50000)
        self.assertEqual(eleven["proposed"]["amount_paise"], 50000)
        self.assertEqual(eleven["payment_received_verified"]["amount_paise"], 0)
        self.assertEqual(eleven["payment_received_verified"]["verification"], "not_implemented")
        self.assertEqual(eleven["ledger_net_unverified"]["amount_paise"], 100000-20000-5000-1000)
        self.assertEqual(eleven["net_realized_synthetic"]["verification"], "synthetic")
        self.assertEqual(eleven["net_realized_synthetic"]["amount_paise"], 0 - 5000 - 1000 - 20000)  # settled 0

    def test_migration_v19_to_v20(self):
        tmp2 = tempfile.TemporaryDirectory()
        self.addCleanup(tmp2.cleanup)
        dir2 = Path(tmp2.name) / "priv"
        dir2.mkdir(mode=0o700)
        auth_path2 = dir2 / "auth.sqlite3"
        state_path2 = dir2 / "state.sqlite3"
        auth2 = AuthService(AuthStorage(auth_path2))
        auth2.register("alice", "disposable test password")
        token2 = auth2.login("alice", "disposable test password")
        state2 = StateStorage(state_path2, create=True, confirmed=True)
        for v in range(2, 20):
            getattr(state2, f"migrate_to_v{v}")(dir2 / f"before-v{v}.sqlite3", confirmed=True)
        with state2.transaction() as conn:
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 19)
        backup = dir2 / "before-v20.sqlite3"
        state2.migrate_to_v20(backup, confirmed=True)
        with state2.transaction() as conn:
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 20)
        # can create invoice after migration
        od2 = OrderDrafts(auth2, state2)
        inv2 = Invoices(auth2, state2)
        oid = od2.create(token2, "c", 12345, operation_key="ok1")
        iid = inv2.create(token2, oid, operation_key="k1")
        self.assertEqual(inv2.get(token2, iid)["status"], "draft")

    def test_version_guard(self):
        tmp2 = tempfile.TemporaryDirectory()
        self.addCleanup(tmp2.cleanup)
        dir2 = Path(tmp2.name) / "priv"
        dir2.mkdir(mode=0o700)
        auth_path2 = dir2 / "auth.sqlite3"
        state_path2 = dir2 / "state.sqlite3"
        auth2 = AuthService(AuthStorage(auth_path2))
        auth2.register("alice", "disposable test password")
        token2 = auth2.login("alice", "disposable test password")
        state2 = StateStorage(state_path2, create=True, confirmed=True)
        for v in range(2, 20):
            getattr(state2, f"migrate_to_v{v}")(dir2 / f"before-v{v}.sqlite3", confirmed=True)
        inv2 = Invoices(auth2, state2)
        with self.assertRaises(StateStorageError):
            inv2.create(token2, "a"*32, operation_key="k1")

"""Tests for v17 CRM customers — owner-scoped, session-authorized."""

import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from ai_earn.auth import AuthService, AccessDenied
from ai_earn.auth_storage import AuthStorage
from ai_earn.state_storage import StateStorage, StateStorageError
from ai_earn.customers import Customers, CustomerError
from ai_earn.dashboard_metrics import DashboardMetrics
from ai_earn.state_recovery_comparison import StateRecoveryComparison


@unittest.skipUnless(os.name in {"posix", "nt"}, "requires private filesystem")
class CustomerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
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
        for v in range(2, 18):
            getattr(state, f"migrate_to_v{v}")(self.directory / f"before-v{v}.sqlite3", confirmed=True)
        self.state = StateStorage(self.state_path)
        self.customers = Customers(self.auth, self.state)

    def test_create_and_get(self):
        ident = self.customers.create(self.alice_token, "Alice Co", "alice@example.com", "referral", operation_key="k1")
        item = self.customers.get(self.alice_token, ident)
        self.assertEqual(item["name"], "Alice Co")
        self.assertEqual(item["contact"], "alice@example.com")
        self.assertEqual(item["source"], "referral")
        self.assertEqual(item["status"], "lead")
        with self.assertRaises(AccessDenied):
            self.customers.get(self.bob_token, ident)

    def test_list_and_filter(self):
        c1 = self.customers.create(self.alice_token, "A", "a@a.com", "", operation_key="k1")
        c2 = self.customers.create(self.alice_token, "B", "b@b.com", "", operation_key="k2")
        self.customers.contacted(self.alice_token, c1, operation_key="p1")
        lst = self.customers.list(self.alice_token, status="contacted")
        self.assertEqual(len(lst), 1)
        self.assertEqual(lst[0]["id"], c1)
        lst_all = self.customers.list(self.alice_token)
        self.assertEqual(len(lst_all), 2)
        lst_page = self.customers.list(self.alice_token, limit=1)
        self.assertEqual(len(lst_page), 1)
        lst_next = self.customers.list(self.alice_token, after_id=lst_page[0]["id"], limit=1)
        self.assertEqual(len(lst_next), 1)
        self.assertNotEqual(lst_page[0]["id"], lst_next[0]["id"])

    def test_transitions(self):
        ident = self.customers.create(self.alice_token, "Cust", "c@c.com", "", operation_key="k1")
        self.customers.contacted(self.alice_token, ident, operation_key="k2")
        self.assertEqual(self.customers.get(self.alice_token, ident)["status"], "contacted")
        self.customers.converted(self.alice_token, ident, operation_key="k3")
        self.assertEqual(self.customers.get(self.alice_token, ident)["status"], "converted")
        with self.assertRaises(CustomerError):
            self.customers.contacted(self.alice_token, ident, operation_key="k4")
        # lead -> rejected
        ident2 = self.customers.create(self.alice_token, "Cust2", "c2@c.com", "", operation_key="k5")
        self.customers.rejected(self.alice_token, ident2, operation_key="k6")
        self.assertEqual(self.customers.get(self.alice_token, ident2)["status"], "rejected")
        # contacted -> rejected
        ident3 = self.customers.create(self.alice_token, "Cust3", "c3@c.com", "", operation_key="k7")
        self.customers.contacted(self.alice_token, ident3, operation_key="k8")
        self.customers.rejected(self.alice_token, ident3, operation_key="k9")
        self.assertEqual(self.customers.get(self.alice_token, ident3)["status"], "rejected")

    def test_invalid_inputs(self):
        with self.assertRaises(CustomerError):
            self.customers.create(self.alice_token, "", "c@c.com", "", operation_key="k1")
        with self.assertRaises(CustomerError):
            self.customers.create(self.alice_token, "N", "", "", operation_key="k1")
        with self.assertRaises(CustomerError):
            self.customers.create(self.alice_token, "N", "c@c.com", "x"*501, operation_key="k1")
        with self.assertRaises(CustomerError):
            self.customers.create(self.alice_token, "N\x00", "c@c.com", "", operation_key="k1")
        with self.assertRaises(CustomerError):
            self.customers.create(self.alice_token, "N", "c\x00@c.com", "", operation_key="k1")
        with self.assertRaises(CustomerError):
            self.customers.create(self.alice_token, " ", "c@c.com", "", operation_key="k1")

    def test_idempotency_and_conflict(self):
        ident = self.customers.create(self.alice_token, "Name", "contact", "src", operation_key="same")
        ident2 = self.customers.create(self.alice_token, "Name", "contact", "src", operation_key="same")
        self.assertEqual(ident, ident2)
        with self.assertRaises(CustomerError):
            self.customers.create(self.alice_token, "Different", "contact", "src", operation_key="same")
        self.customers.contacted(self.alice_token, ident, operation_key="pub")
        self.customers.contacted(self.alice_token, ident, operation_key="pub")
        self.assertEqual(self.customers.get(self.alice_token, ident)["status"], "contacted")

    def test_concurrent_same_key(self):
        results = []
        errors = []
        def attempt():
            try:
                ident = self.customers.create(self.alice_token, "Conc", "conc@c.com", "", operation_key="concurrent")
                results.append(ident)
            except Exception as e:
                errors.append(e)
        threads = [threading.Thread(target=attempt) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(len(results), 5)
        self.assertEqual(len(set(results)), 1)
        self.assertEqual(len(errors), 0)

    def test_dashboard_and_recovery(self):
        cid = self.customers.create(self.alice_token, "Cust", "c@c.com", "", operation_key="k1")
        metrics = DashboardMetrics(self.auth, self.state).get(self.alice_token)
        self.assertIn("customers", metrics)
        self.assertEqual(metrics["customers"]["count"], 1)
        cand_path = self.directory / "cand.sqlite3"
        self.state.backup_to(cand_path, confirmed=True)
        cand = StateStorage(cand_path)
        comp = StateRecoveryComparison(self.auth, self.state, cand)
        rec = comp.compare_records(self.alice_token)
        self.assertEqual(rec["tables"]["customers"]["matching"], 1)

    def test_migration_v16_to_v17(self):
        tmp2 = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(tmp2.cleanup)
        dir2 = Path(tmp2.name) / "priv"
        if os.name == "nt":
            from ai_earn.windows_security import create_private_directory
            create_private_directory(dir2)
        else:
            dir2.mkdir(mode=0o700)
        auth_path2 = dir2 / "auth.sqlite3"
        state_path2 = dir2 / "state.sqlite3"
        auth2 = AuthService(AuthStorage(auth_path2))
        auth2.register("alice", "disposable test password")
        token2 = auth2.login("alice", "disposable test password")
        state2 = StateStorage(state_path2, create=True, confirmed=True)
        for v in range(2, 17):
            getattr(state2, f"migrate_to_v{v}")(dir2 / f"before-v{v}.sqlite3", confirmed=True)
        with state2.transaction() as conn:
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 16)
        backup = dir2 / "before-v17.sqlite3"
        state2.migrate_to_v17(backup, confirmed=True)
        with state2.transaction() as conn:
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 17)
        cust2 = Customers(auth2, state2)
        cid = cust2.create(token2, "Migrated", "m@m.com", "", operation_key="k1")
        self.assertEqual(cust2.get(token2, cid)["status"], "lead")

    def test_version_guard(self):
        tmp2 = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(tmp2.cleanup)
        dir2 = Path(tmp2.name) / "priv"
        if os.name == "nt":
            from ai_earn.windows_security import create_private_directory
            create_private_directory(dir2)
        else:
            dir2.mkdir(mode=0o700)
        auth_path2 = dir2 / "auth.sqlite3"
        state_path2 = dir2 / "state.sqlite3"
        auth2 = AuthService(AuthStorage(auth_path2))
        auth2.register("alice", "disposable test password")
        token2 = auth2.login("alice", "disposable test password")
        state2 = StateStorage(state_path2, create=True, confirmed=True)
        for v in range(2, 17):
            getattr(state2, f"migrate_to_v{v}")(dir2 / f"before-v{v}.sqlite3", confirmed=True)
        cust2 = Customers(auth2, state2)
        with self.assertRaises(StateStorageError):
            cust2.create(token2, "Fail", "c@c.com", "", operation_key="k1")

    def test_isolation(self):
        c_alice = self.customers.create(self.alice_token, "Alice", "a@a.com", "", operation_key="k1")
        c_bob = self.customers.create(self.bob_token, "Bob", "b@b.com", "", operation_key="k1")
        self.assertNotEqual(c_alice, c_bob)
        self.assertEqual(len(self.customers.list(self.alice_token)), 1)
        self.assertEqual(len(self.customers.list(self.bob_token)), 1)

    def test_invalid_transitions(self):
        ident = self.customers.create(self.alice_token, "Cust", "c@c.com", "", operation_key="k1")
        with self.assertRaises(CustomerError):
            self.customers.converted(self.alice_token, ident, operation_key="k2")
        self.customers.contacted(self.alice_token, ident, operation_key="k3")
        # contacted -> contacted invalid
        with self.assertRaises(CustomerError):
            self.customers.contacted(self.alice_token, ident, operation_key="k4")

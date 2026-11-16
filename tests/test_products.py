"""Tests for v17 product workspace — owner-scoped, session-authorized."""

import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from ai_earn.auth import AuthService, AccessDenied
from ai_earn.auth_storage import AuthStorage
from ai_earn.state_storage import StateStorage, StateStorageError
from ai_earn.products import Products, ProductError
from ai_earn.dashboard_metrics import DashboardMetrics
from ai_earn.state_recovery_comparison import StateRecoveryComparison


@unittest.skipUnless(os.name in {"posix", "nt"}, "requires private filesystem")
class ProductTests(unittest.TestCase):
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
        for v in range(2, 18):
            getattr(state, f"migrate_to_v{v}")(self.directory / f"before-v{v}.sqlite3", confirmed=True)
        self.state = StateStorage(self.state_path)
        self.products = Products(self.auth, self.state)

    def test_create_and_get(self):
        ident = self.products.create(self.alice_token, "My Product", "Description text", "1.0.0", 500000, operation_key="k1")
        item = self.products.get(self.alice_token, ident)
        self.assertEqual(item["title"], "My Product")
        self.assertEqual(item["description"], "Description text")
        self.assertEqual(item["version"], "1.0.0")
        self.assertEqual(item["price_paise"], 500000)
        self.assertEqual(item["status"], "draft")
        with self.assertRaises(AccessDenied):
            self.products.get(self.bob_token, ident)

    def test_list_and_filter(self):
        p1 = self.products.create(self.alice_token, "Alpha", "", "1.0", 100000, operation_key="k1")
        p2 = self.products.create(self.alice_token, "Beta", "", "2.0", 200000, operation_key="k2")
        self.products.publish(self.alice_token, p1, operation_key="p1")
        lst = self.products.list(self.alice_token, status="published")
        self.assertEqual(len(lst), 1)
        self.assertEqual(lst[0]["id"], p1)
        lst_all = self.products.list(self.alice_token)
        self.assertEqual(len(lst_all), 2)
        lst_page = self.products.list(self.alice_token, limit=1)
        self.assertEqual(len(lst_page), 1)
        lst_next = self.products.list(self.alice_token, after_id=lst_page[0]["id"], limit=1)
        self.assertEqual(len(lst_next), 1)
        self.assertNotEqual(lst_page[0]["id"], lst_next[0]["id"])

    def test_transitions(self):
        ident = self.products.create(self.alice_token, "Prod", "", "1.0", 0, operation_key="k1")
        self.products.publish(self.alice_token, ident, operation_key="k2")
        self.assertEqual(self.products.get(self.alice_token, ident)["status"], "published")
        self.products.archive(self.alice_token, ident, operation_key="k3")
        self.assertEqual(self.products.get(self.alice_token, ident)["status"], "archived")
        with self.assertRaises(ProductError):
            self.products.publish(self.alice_token, ident, operation_key="k4")
        ident2 = self.products.create(self.alice_token, "Prod2", "", "1.0", 0, operation_key="k5")
        self.products.archive(self.alice_token, ident2, operation_key="k6")
        self.assertEqual(self.products.get(self.alice_token, ident2)["status"], "archived")

    def test_invalid_inputs(self):
        with self.assertRaises(ProductError):
            self.products.create(self.alice_token, "", "", "1.0", 0, operation_key="k1")
        with self.assertRaises(ProductError):
            self.products.create(self.alice_token, "T", "x"*2001, "1.0", 0, operation_key="k1")
        with self.assertRaises(ProductError):
            self.products.create(self.alice_token, "T", "", "", 0, operation_key="k1")
        with self.assertRaises(ProductError):
            self.products.create(self.alice_token, "T", "", "!", 0, operation_key="k1")
        with self.assertRaises(ProductError):
            self.products.create(self.alice_token, "T", "", "1.0", -1, operation_key="k1")
        with self.assertRaises(ProductError):
            self.products.create(self.alice_token, "T\x00", "", "1.0", 0, operation_key="k1")
        with self.assertRaises(ProductError):
            self.products.create(self.alice_token, "T", "", "a"*21, 0, operation_key="k1")

    def test_idempotency_and_conflict(self):
        ident = self.products.create(self.alice_token, "Svc", "Desc", "1.0", 100, operation_key="same")
        ident2 = self.products.create(self.alice_token, "Svc", "Desc", "1.0", 100, operation_key="same")
        self.assertEqual(ident, ident2)
        with self.assertRaises(ProductError):
            self.products.create(self.alice_token, "Different", "Desc", "1.0", 100, operation_key="same")
        self.products.publish(self.alice_token, ident, operation_key="pub")
        self.products.publish(self.alice_token, ident, operation_key="pub")
        self.assertEqual(self.products.get(self.alice_token, ident)["status"], "published")

    def test_concurrent_same_key(self):
        results = []
        errors = []
        def attempt():
            try:
                ident = self.products.create(self.alice_token, "Concurrent", "", "1.0", 0, operation_key="concurrent")
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
        pid = self.products.create(self.alice_token, "Prod", "Desc", "1.0", 12345, operation_key="k1")
        metrics = DashboardMetrics(self.auth, self.state).get(self.alice_token)
        self.assertIn("products", metrics)
        self.assertEqual(metrics["products"]["count"], 1)
        cand_path = self.directory / "cand.sqlite3"
        self.state.backup_to(cand_path, confirmed=True)
        cand = StateStorage(cand_path)
        comp = StateRecoveryComparison(self.auth, self.state, cand)
        rec = comp.compare_records(self.alice_token)
        self.assertEqual(rec["tables"]["products"]["matching"], 1)

    def test_migration_v16_to_v17(self):
        tmp2 = tempfile.TemporaryDirectory()
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
        prod2 = Products(auth2, state2)
        pid = prod2.create(token2, "Migrated", "", "1.0", 1000, operation_key="k1")
        self.assertEqual(prod2.get(token2, pid)["status"], "draft")

    def test_version_guard(self):
        tmp2 = tempfile.TemporaryDirectory()
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
        prod2 = Products(auth2, state2)
        with self.assertRaises(StateStorageError):
            prod2.create(token2, "Fail", "", "1.0", 0, operation_key="k1")

    def test_isolation(self):
        p_alice = self.products.create(self.alice_token, "Alice Prod", "", "1.0", 0, operation_key="k1")
        p_bob = self.products.create(self.bob_token, "Bob Prod", "", "1.0", 0, operation_key="k1")
        self.assertNotEqual(p_alice, p_bob)
        self.assertEqual(len(self.products.list(self.alice_token)), 1)
        self.assertEqual(len(self.products.list(self.bob_token)), 1)

    def test_price_and_version_boundaries(self):
        # price 0 and max
        pid = self.products.create(self.alice_token, "T", "", "a", 0, operation_key="k1")
        self.assertEqual(self.products.get(self.alice_token, pid)["price_paise"], 0)
        pid2 = self.products.create(self.alice_token, "T2", "", "1_0-2.3", 1_000_000_000_000, operation_key="k2")
        self.assertEqual(self.products.get(self.alice_token, pid2)["price_paise"], 1_000_000_000_000)
        with self.assertRaises(ProductError):
            self.products.create(self.alice_token, "T", "", "1.0", 1_000_000_000_001, operation_key="k3")

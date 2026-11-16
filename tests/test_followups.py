"""Tests for v19 followups — owner-scoped, session-authorized, CSV."""

import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from ai_earn.auth import AuthService, AccessDenied
from ai_earn.auth_storage import AuthStorage
from ai_earn.state_storage import StateStorage, StateStorageError
from ai_earn.followups import Followups, FollowupError
from ai_earn.customers import Customers
from ai_earn.dashboard_metrics import DashboardMetrics
from ai_earn.state_recovery_comparison import StateRecoveryComparison


@unittest.skipUnless(os.name in {"posix", "nt"}, "requires private filesystem")
class FollowupTests(unittest.TestCase):
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
        for v in range(2, 20):
            getattr(state, f"migrate_to_v{v}")(self.directory / f"before-v{v}.sqlite3", confirmed=True)
        self.state = StateStorage(self.state_path)
        self.followups = Followups(self.auth, self.state)
        self.customers = Customers(self.auth, self.state)
        self.cust_alice = self.customers.create(self.alice_token, "Alice Co", "alice@example.com", "src", operation_key="cust1")
        self.cust_bob = self.customers.create(self.bob_token, "Bob Co", "bob@example.com", "src", operation_key="cust1")

    def test_create_and_get(self):
        ident = self.followups.create(self.alice_token, self.cust_alice, 0, "Note1", operation_key="k1")
        item = self.followups.get(self.alice_token, ident)
        self.assertEqual(item["customer_id"], self.cust_alice)
        self.assertEqual(item["due_at"], 0)
        self.assertEqual(item["note"], "Note1")
        self.assertEqual(item["status"], "pending")
        with self.assertRaises(AccessDenied):
            self.followups.get(self.bob_token, ident)
        with self.assertRaises(AccessDenied):
            self.followups.create(self.bob_token, self.cust_alice, 0, "hi", operation_key="k2")

    def test_list_and_filter(self):
        f1 = self.followups.create(self.alice_token, self.cust_alice, 100, "N1", operation_key="k1")
        f2 = self.followups.create(self.alice_token, self.cust_alice, 200, "N2", operation_key="k2")
        self.followups.completed(self.alice_token, f1, operation_key="p1")
        lst = self.followups.list(self.alice_token, status="completed")
        self.assertEqual(len(lst), 1)
        self.assertEqual(lst[0]["id"], f1)
        lst_all = self.followups.list(self.alice_token)
        self.assertEqual(len(lst_all), 2)
        lst_cust = self.followups.list(self.alice_token, customer_id=self.cust_alice)
        self.assertEqual(len(lst_cust), 2)
        lst_page = self.followups.list(self.alice_token, limit=1)
        self.assertEqual(len(lst_page), 1)
        lst_next = self.followups.list(self.alice_token, after_id=lst_page[0]["id"], limit=1)
        self.assertEqual(len(lst_next), 1)
        self.assertNotEqual(lst_page[0]["id"], lst_next[0]["id"])

    def test_transitions(self):
        ident = self.followups.create(self.alice_token, self.cust_alice, 0, "", operation_key="k1")
        self.followups.completed(self.alice_token, ident, operation_key="k2")
        self.assertEqual(self.followups.get(self.alice_token, ident)["status"], "completed")
        with self.assertRaises(FollowupError):
            self.followups.completed(self.alice_token, ident, operation_key="k3")
        ident2 = self.followups.create(self.alice_token, self.cust_alice, 0, "", operation_key="k4")
        self.followups.cancelled(self.alice_token, ident2, operation_key="k5")
        self.assertEqual(self.followups.get(self.alice_token, ident2)["status"], "cancelled")
        with self.assertRaises(FollowupError):
            self.followups.cancelled(self.alice_token, ident2, operation_key="k6")

    def test_invalid_inputs(self):
        with self.assertRaises(FollowupError):
            self.followups.create(self.alice_token, self.cust_alice, -1, "", operation_key="k1")
        with self.assertRaises(FollowupError):
            self.followups.create(self.alice_token, self.cust_alice, 4102444801, "", operation_key="k1")
        with self.assertRaises(FollowupError):
            self.followups.create(self.alice_token, self.cust_alice, 0, "x"*501, operation_key="k1")
        with self.assertRaises(FollowupError):
            self.followups.create(self.alice_token, self.cust_alice, 0, "note\x00", operation_key="k1")
        with self.assertRaises(FollowupError):
            self.followups.create(self.alice_token, "badid", 0, "", operation_key="k1")

    def test_idempotency_and_conflict(self):
        ident = self.followups.create(self.alice_token, self.cust_alice, 0, "note", operation_key="same")
        ident2 = self.followups.create(self.alice_token, self.cust_alice, 0, "note", operation_key="same")
        self.assertEqual(ident, ident2)
        with self.assertRaises(FollowupError):
            self.followups.create(self.alice_token, self.cust_alice, 1, "note", operation_key="same")
        self.followups.completed(self.alice_token, ident, operation_key="pub")
        self.followups.completed(self.alice_token, ident, operation_key="pub")
        self.assertEqual(self.followups.get(self.alice_token, ident)["status"], "completed")

    def test_concurrent_same_key(self):
        results = []
        errors = []
        def attempt():
            try:
                ident = self.followups.create(self.alice_token, self.cust_alice, 0, "conc", operation_key="concurrent")
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

    def test_empty_and_boundaries(self):
        self.assertEqual(self.followups.list(self.alice_token), [])
        note_max = "y" * 500
        ident = self.followups.create(self.alice_token, self.cust_alice, 4102444800, note_max, operation_key="k1")
        self.assertEqual(self.followups.get(self.alice_token, ident)["note"], note_max)
        self.assertEqual(len(self.followups.list(self.alice_token, status="pending")), 1)

    def test_dashboard_and_recovery(self):
        self.followups.create(self.alice_token, self.cust_alice, 0, "Body", operation_key="k1")
        metrics = DashboardMetrics(self.auth, self.state).get(self.alice_token)
        self.assertIn("followups", metrics)
        self.assertEqual(metrics["followups"]["count"], 1)
        cand_path = self.directory / "cand.sqlite3"
        self.state.backup_to(cand_path, confirmed=True)
        cand = StateStorage(cand_path)
        comp = StateRecoveryComparison(self.auth, self.state, cand)
        rec = comp.compare_records(self.alice_token)
        self.assertEqual(rec["tables"]["followups"]["matching"], 1)

    def test_migration_v18_to_v19(self):
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
        for v in range(2, 19):
            getattr(state2, f"migrate_to_v{v}")(dir2 / f"before-v{v}.sqlite3", confirmed=True)
        with state2.transaction() as conn:
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 18)
        backup = dir2 / "before-v19.sqlite3"
        state2.migrate_to_v19(backup, confirmed=True)
        with state2.transaction() as conn:
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 19)
        cust2 = Customers(auth2, state2)
        cid = cust2.create(token2, "Migrated", "m@m.com", "", operation_key="k1")
        foll2 = Followups(auth2, state2)
        pid = foll2.create(token2, cid, 0, "Body", operation_key="k2")
        self.assertEqual(foll2.get(token2, pid)["status"], "pending")

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
        for v in range(2, 19):
            getattr(state2, f"migrate_to_v{v}")(dir2 / f"before-v{v}.sqlite3", confirmed=True)
        foll2 = Followups(auth2, state2)
        cust2 = Customers(auth2, state2)
        cid = cust2.create(token2, "N", "c@c.com", "", operation_key="k1")
        with self.assertRaises(StateStorageError):
            foll2.create(token2, cid, 0, "Body", operation_key="k1")

    def test_isolation(self):
        f_alice = self.followups.create(self.alice_token, self.cust_alice, 0, "A", operation_key="k1")
        f_bob = self.followups.create(self.bob_token, self.cust_bob, 0, "B", operation_key="k1")
        self.assertNotEqual(f_alice, f_bob)
        self.assertEqual(len(self.followups.list(self.alice_token)), 1)
        self.assertEqual(len(self.followups.list(self.bob_token)), 1)

    def test_csv_import_export(self):
        # Export empty
        csv_empty = self.customers.export_csv(self.alice_token)
        self.assertIn("name,contact,source", csv_empty)
        # Import valid CSV
        csv_text = "name,contact,source\nBob, bob@example.com, src1\nAlice, alice@example.com, \n"
        result = self.customers.import_csv(self.alice_token, csv_text, operation_key_prefix="imp1")
        self.assertEqual(len(result["created"]), 2)
        # Idempotent retry with same prefix should not create duplicates (same derived keys)
        result2 = self.customers.import_csv(self.alice_token, csv_text, operation_key_prefix="imp1")
        self.assertEqual(len(result2["created"]), 2)
        self.assertEqual(set(result["created"]), set(result2["created"]))
        # Export should contain at least 3 (cust_alice + 2 imported)
        csv_out = self.customers.export_csv(self.alice_token)
        self.assertIn("Bob", csv_out)
        self.assertIn("Alice", csv_out)
        # Invalid header
        with self.assertRaises(Exception):
            self.customers.import_csv(self.alice_token, "bad,header\nx,y,z\n", operation_key_prefix="imp2")
        # Invalid row (name too long)
        long_name = "x" * 201
        bad_csv = f"name,contact,source\n{long_name},contact,source\n"
        with self.assertRaises(Exception):
            self.customers.import_csv(self.alice_token, bad_csv, operation_key_prefix="imp3")
        # CSV isolation: bob's import doesn't affect alice
        csv_bob = "name,contact,source\nBob2,bob2@example.com,src\n"
        res_bob = self.customers.import_csv(self.bob_token, csv_bob, operation_key_prefix="imp1")
        self.assertEqual(len(res_bob["created"]), 1)
        self.assertEqual(len(self.customers.list(self.alice_token)), 3)  # alice: cust_alice +2

    def test_csv_boundaries(self):
        # Too many rows
        rows = "name,contact,source\n" + "\n".join([f"Name{i},contact{i}@example.com,src" for i in range(1001)])
        with self.assertRaises(Exception):
            self.customers.import_csv(self.alice_token, rows, operation_key_prefix="big")
        # Too large
        huge = "name,contact,source\n" + "a," + "b@"*500000 + ",c\n"
        # length check should trigger before row limit; create huge text >1M
        huge_text = "name,contact,source\n" + ("a,b,c\n" * 400000)  # ~2M
        with self.assertRaises(Exception):
            self.customers.import_csv(self.alice_token, huge_text, operation_key_prefix="huge")
        # Special characters quoting
        csv_text = 'name,contact,source\n"Name, With, Comma",contact@example.com,src\n'
        result = self.customers.import_csv(self.alice_token, csv_text, operation_key_prefix="comma")
        self.assertEqual(len(result["created"]), 1)
        exported = self.customers.export_csv(self.alice_token)
        self.assertIn("Name, With, Comma", exported)

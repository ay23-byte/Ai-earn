"""Tests for v18 communications — owner-scoped, session-authorized."""

import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from ai_earn.auth import AuthService, AccessDenied
from ai_earn.auth_storage import AuthStorage
from ai_earn.state_storage import StateStorage, StateStorageError
from ai_earn.communications import Communications, CommunicationError
from ai_earn.customers import Customers
from ai_earn.dashboard_metrics import DashboardMetrics
from ai_earn.state_recovery_comparison import StateRecoveryComparison


@unittest.skipUnless(os.name in {"posix", "nt"}, "requires private filesystem")
class CommunicationTests(unittest.TestCase):
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
        for v in range(2, 19):
            getattr(state, f"migrate_to_v{v}")(self.directory / f"before-v{v}.sqlite3", confirmed=True)
        self.state = StateStorage(self.state_path)
        self.comms = Communications(self.auth, self.state)
        self.customers = Customers(self.auth, self.state)
        self.cust_alice = self.customers.create(self.alice_token, "Alice Co", "alice@example.com", "src", operation_key="cust1")
        self.cust_bob = self.customers.create(self.bob_token, "Bob Co", "bob@example.com", "src", operation_key="cust1")

    def test_create_and_get(self):
        ident = self.comms.create(self.alice_token, self.cust_alice, "email", "Hello body", operation_key="k1")
        item = self.comms.get(self.alice_token, ident)
        self.assertEqual(item["customer_id"], self.cust_alice)
        self.assertEqual(item["channel"], "email")
        self.assertEqual(item["body"], "Hello body")
        self.assertEqual(item["status"], "draft")
        with self.assertRaises(AccessDenied):
            self.comms.get(self.bob_token, ident)
        # cannot create for other owner's customer
        with self.assertRaises(AccessDenied):
            self.comms.create(self.bob_token, self.cust_alice, "email", "hi", operation_key="k2")

    def test_list_and_filter(self):
        c1 = self.comms.create(self.alice_token, self.cust_alice, "email", "Body1", operation_key="k1")
        c2 = self.comms.create(self.alice_token, self.cust_alice, "phone", "Body2", operation_key="k2")
        self.comms.sent(self.alice_token, c1, operation_key="p1")
        lst = self.comms.list(self.alice_token, status="sent")
        self.assertEqual(len(lst), 1)
        self.assertEqual(lst[0]["id"], c1)
        lst_all = self.comms.list(self.alice_token)
        self.assertEqual(len(lst_all), 2)
        # filter by customer
        lst_cust = self.comms.list(self.alice_token, customer_id=self.cust_alice)
        self.assertEqual(len(lst_cust), 2)
        # pagination
        lst_page = self.comms.list(self.alice_token, limit=1)
        self.assertEqual(len(lst_page), 1)
        lst_next = self.comms.list(self.alice_token, after_id=lst_page[0]["id"], limit=1)
        self.assertEqual(len(lst_next), 1)
        self.assertNotEqual(lst_page[0]["id"], lst_next[0]["id"])

    def test_transitions(self):
        ident = self.comms.create(self.alice_token, self.cust_alice, "email", "Body", operation_key="k1")
        self.comms.sent(self.alice_token, ident, operation_key="k2")
        self.assertEqual(self.comms.get(self.alice_token, ident)["status"], "sent")
        self.comms.archive(self.alice_token, ident, operation_key="k3")
        self.assertEqual(self.comms.get(self.alice_token, ident)["status"], "archived")
        with self.assertRaises(CommunicationError):
            self.comms.sent(self.alice_token, ident, operation_key="k4")
        ident2 = self.comms.create(self.alice_token, self.cust_alice, "email", "Body2", operation_key="k5")
        self.comms.archive(self.alice_token, ident2, operation_key="k6")
        self.assertEqual(self.comms.get(self.alice_token, ident2)["status"], "archived")

    def test_invalid_inputs(self):
        with self.assertRaises(CommunicationError):
            self.comms.create(self.alice_token, self.cust_alice, "", "Body", operation_key="k1")
        with self.assertRaises(CommunicationError):
            self.comms.create(self.alice_token, self.cust_alice, "x"*31, "Body", operation_key="k1")
        with self.assertRaises(CommunicationError):
            self.comms.create(self.alice_token, self.cust_alice, "email", "", operation_key="k1")
        with self.assertRaises(CommunicationError):
            self.comms.create(self.alice_token, self.cust_alice, "email", "x"*2001, operation_key="k1")
        with self.assertRaises(CommunicationError):
            self.comms.create(self.alice_token, self.cust_alice, "email\x00", "Body", operation_key="k1")

    def test_idempotency_and_conflict(self):
        ident = self.comms.create(self.alice_token, self.cust_alice, "email", "Body", operation_key="same")
        ident2 = self.comms.create(self.alice_token, self.cust_alice, "email", "Body", operation_key="same")
        self.assertEqual(ident, ident2)
        with self.assertRaises(CommunicationError):
            self.comms.create(self.alice_token, self.cust_alice, "email", "Different", operation_key="same")
        self.comms.sent(self.alice_token, ident, operation_key="pub")
        self.comms.sent(self.alice_token, ident, operation_key="pub")
        self.assertEqual(self.comms.get(self.alice_token, ident)["status"], "sent")

    def test_concurrent_same_key(self):
        results = []
        errors = []
        def attempt():
            try:
                ident = self.comms.create(self.alice_token, self.cust_alice, "email", "Conc", operation_key="concurrent")
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
        self.comms.create(self.alice_token, self.cust_alice, "email", "Body", operation_key="k1")
        metrics = DashboardMetrics(self.auth, self.state).get(self.alice_token)
        self.assertIn("communications", metrics)
        self.assertEqual(metrics["communications"]["count"], 1)
        cand_path = self.directory / "cand.sqlite3"
        self.state.backup_to(cand_path, confirmed=True)
        cand = StateStorage(cand_path)
        comp = StateRecoveryComparison(self.auth, self.state, cand)
        rec = comp.compare_records(self.alice_token)
        self.assertEqual(rec["tables"]["communications"]["matching"], 1)

    def test_migration_v17_to_v18(self):
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
        for v in range(2, 18):
            getattr(state2, f"migrate_to_v{v}")(dir2 / f"before-v{v}.sqlite3", confirmed=True)
        with state2.transaction() as conn:
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 17)
        backup = dir2 / "before-v18.sqlite3"
        state2.migrate_to_v18(backup, confirmed=True)
        with state2.transaction() as conn:
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 18)
        cust2 = Customers(auth2, state2)
        cid = cust2.create(token2, "Migrated", "m@m.com", "", operation_key="k1")
        comm2 = Communications(auth2, state2)
        pid = comm2.create(token2, cid, "email", "Body", operation_key="k2")
        self.assertEqual(comm2.get(token2, pid)["status"], "draft")

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
        for v in range(2, 18):
            getattr(state2, f"migrate_to_v{v}")(dir2 / f"before-v{v}.sqlite3", confirmed=True)
        comm2 = Communications(auth2, state2)
        cust2 = Customers(auth2, state2)
        # need customer on v17
        cid = cust2.create(token2, "N", "c@c.com", "", operation_key="k1")
        with self.assertRaises(StateStorageError):
            comm2.create(token2, cid, "email", "Body", operation_key="k1")

    def test_empty_and_boundaries(self):
        # empty list, zero-filter, body boundary 1 and 2000
        self.assertEqual(self.comms.list(self.alice_token), [])
        ident = self.comms.create(self.alice_token, self.cust_alice, "a", "x", operation_key="k1")
        body_max = "y" * 2000
        ident2 = self.comms.create(self.alice_token, self.cust_alice, "b", body_max, operation_key="k2")
        self.assertEqual(self.comms.get(self.alice_token, ident2)["body"], body_max)
        self.assertEqual(len(self.comms.list(self.alice_token, status="draft")), 2)
        self.assertEqual(len(self.comms.list(self.alice_token, status="sent")), 0)

    def test_isolation(self):
        c_alice = self.comms.create(self.alice_token, self.cust_alice, "email", "A", operation_key="k1")
        c_bob = self.comms.create(self.bob_token, self.cust_bob, "email", "B", operation_key="k1")
        self.assertNotEqual(c_alice, c_bob)
        self.assertEqual(len(self.comms.list(self.alice_token)), 1)
        self.assertEqual(len(self.comms.list(self.bob_token)), 1)

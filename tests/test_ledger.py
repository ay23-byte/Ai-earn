"""Tests for v18 ledger — owner-scoped, session-authorized, net calculation."""

import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from ai_earn.auth import AuthService, AccessDenied
from ai_earn.auth_storage import AuthStorage
from ai_earn.state_storage import StateStorage, StateStorageError
from ai_earn.ledger import Ledger, LedgerError
from ai_earn.dashboard_metrics import DashboardMetrics
from ai_earn.state_recovery_comparison import StateRecoveryComparison


@unittest.skipUnless(os.name in {"posix", "nt"}, "requires private filesystem")
class LedgerTests(unittest.TestCase):
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
        for v in range(2, 19):
            getattr(state, f"migrate_to_v{v}")(self.directory / f"before-v{v}.sqlite3", confirmed=True)
        self.state = StateStorage(self.state_path)
        self.ledger = Ledger(self.auth, self.state)

    def test_create_and_get(self):
        ident = self.ledger.add(self.alice_token, "income", 500000, "note", operation_key="k1")
        item = self.ledger.get(self.alice_token, ident)
        self.assertEqual(item["kind"], "income")
        self.assertEqual(item["amount_paise"], 500000)
        self.assertEqual(item["note"], "note")
        with self.assertRaises(AccessDenied):
            self.ledger.get(self.bob_token, ident)

    def test_list_and_filter(self):
        l1 = self.ledger.add(self.alice_token, "income", 100000, "", operation_key="k1")
        self.ledger.add(self.alice_token, "expense", 20000, "", operation_key="k2")
        lst = self.ledger.list(self.alice_token, kind="income")
        self.assertEqual(len(lst), 1)
        self.assertEqual(lst[0]["id"], l1)
        lst_all = self.ledger.list(self.alice_token)
        self.assertEqual(len(lst_all), 2)
        lst_page = self.ledger.list(self.alice_token, limit=1)
        self.assertEqual(len(lst_page), 1)
        lst_next = self.ledger.list(self.alice_token, after_id=lst_page[0]["id"], limit=1)
        self.assertEqual(len(lst_next), 1)
        self.assertNotEqual(lst_page[0]["id"], lst_next[0]["id"])

    def test_net(self):
        self.ledger.add(self.alice_token, "income", 100000, "", operation_key="k1")
        self.ledger.add(self.alice_token, "expense", 30000, "", operation_key="k2")
        self.ledger.add(self.alice_token, "fee", 10000, "", operation_key="k3")
        self.ledger.add(self.alice_token, "refund", 5000, "", operation_key="k4")
        net = self.ledger.net(self.alice_token)
        self.assertEqual(net["net_paise"], 100000 - 30000 - 10000 - 5000)
        self.assertEqual(net["totals"]["income"], 100000)
        self.assertEqual(net["totals"]["expense"], 30000)

    def test_invalid_inputs(self):
        with self.assertRaises(LedgerError):
            self.ledger.add(self.alice_token, "invalid", 100, "", operation_key="k1")
        with self.assertRaises(LedgerError):
            self.ledger.add(self.alice_token, "income", -1, "", operation_key="k1")
        with self.assertRaises(LedgerError):
            self.ledger.add(self.alice_token, "income", 1_000_000_000_001, "", operation_key="k1")
        with self.assertRaises(LedgerError):
            self.ledger.add(self.alice_token, "income", 100, "x"*501, operation_key="k1")
        with self.assertRaises(LedgerError):
            self.ledger.add(self.alice_token, "income", 100, "note\x00", operation_key="k1")

    def test_idempotency_and_conflict(self):
        ident = self.ledger.add(self.alice_token, "income", 100, "note", operation_key="same")
        ident2 = self.ledger.add(self.alice_token, "income", 100, "note", operation_key="same")
        self.assertEqual(ident, ident2)
        with self.assertRaises(LedgerError):
            self.ledger.add(self.alice_token, "expense", 100, "note", operation_key="same")

    def test_concurrent_same_key(self):
        results = []
        errors = []
        def attempt():
            try:
                ident = self.ledger.add(self.alice_token, "income", 0, "", operation_key="concurrent")
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
        self.ledger.add(self.alice_token, "income", 12345, "", operation_key="k1")
        metrics = DashboardMetrics(self.auth, self.state).get(self.alice_token)
        self.assertIn("ledger", metrics)
        self.assertEqual(metrics["ledger"]["count"], 1)
        self.assertIn("net_paise", metrics["ledger"])
        cand_path = self.directory / "cand.sqlite3"
        self.state.backup_to(cand_path, confirmed=True)
        cand = StateStorage(cand_path)
        comp = StateRecoveryComparison(self.auth, self.state, cand)
        rec = comp.compare_records(self.alice_token)
        self.assertEqual(rec["tables"]["ledger_entries"]["matching"], 1)

    def test_migration_v17_to_v18(self):
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
        for v in range(2, 18):
            getattr(state2, f"migrate_to_v{v}")(dir2 / f"before-v{v}.sqlite3", confirmed=True)
        with state2.transaction() as conn:
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 17)
        backup = dir2 / "before-v18.sqlite3"
        state2.migrate_to_v18(backup, confirmed=True)
        with state2.transaction() as conn:
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 18)
        led2 = Ledger(auth2, state2)
        lid = led2.add(token2, "income", 1000, "", operation_key="k1")
        self.assertEqual(led2.get(token2, lid)["amount_paise"], 1000)

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
        for v in range(2, 18):
            getattr(state2, f"migrate_to_v{v}")(dir2 / f"before-v{v}.sqlite3", confirmed=True)
        led2 = Ledger(auth2, state2)
        with self.assertRaises(StateStorageError):
            led2.add(token2, "income", 0, "", operation_key="k1")

    def test_isolation(self):
        l_alice = self.ledger.add(self.alice_token, "income", 0, "", operation_key="k1")
        l_bob = self.ledger.add(self.bob_token, "income", 0, "", operation_key="k1")
        self.assertNotEqual(l_alice, l_bob)
        self.assertEqual(len(self.ledger.list(self.alice_token)), 1)
        self.assertEqual(len(self.ledger.list(self.bob_token)), 1)

    def test_net_empty(self):
        net = self.ledger.net(self.alice_token)
        self.assertEqual(net["net_paise"], 0)
        self.assertEqual(net["count"], 0)

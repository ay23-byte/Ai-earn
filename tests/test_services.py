"""Tests for v16 service catalog & portfolio — owner-scoped, session-authorized."""

import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from ai_earn.auth import AuthService, AccessDenied
from ai_earn.auth_storage import AuthStorage
from ai_earn.state_storage import StateStorage, StateStorageError
from ai_earn.services import Services, Portfolio, ServiceError
from ai_earn.dashboard_metrics import DashboardMetrics
from ai_earn.state_recovery_comparison import StateRecoveryComparison


@unittest.skipUnless(os.name in {"posix", "nt"}, "requires private filesystem")
class ServiceTests(unittest.TestCase):
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
        for v in range(2, 17):
            getattr(state, f"migrate_to_v{v}")(self.directory / f"before-v{v}.sqlite3", confirmed=True)
        self.state = StateStorage(self.state_path)
        self.services = Services(self.auth, self.state)
        self.portfolio = Portfolio(self.auth, self.state)

    def test_create_and_get(self):
        ident = self.services.create(self.alice_token, "My Service", "Scope text", "Deliverable text", 500000, 2, operation_key="k1")
        item = self.services.get(self.alice_token, ident)
        self.assertEqual(item["title"], "My Service")
        self.assertEqual(item["scope"], "Scope text")
        self.assertEqual(item["deliverable"], "Deliverable text")
        self.assertEqual(item["price_paise"], 500000)
        self.assertEqual(item["revisions"], 2)
        self.assertEqual(item["status"], "draft")
        # bob cannot see alice's service
        with self.assertRaises(AccessDenied):
            self.services.get(self.bob_token, ident)

    def test_list_and_filter(self):
        s1 = self.services.create(self.alice_token, "Alpha", "", "Deliv", 100000, 1, operation_key="k1")
        s2 = self.services.create(self.alice_token, "Beta", "", "Deliv", 200000, 0, operation_key="k2")
        self.services.publish(self.alice_token, s1, operation_key="p1")
        lst = self.services.list(self.alice_token, status="published")
        self.assertEqual(len(lst), 1)
        self.assertEqual(lst[0]["id"], s1)
        lst_all = self.services.list(self.alice_token)
        self.assertEqual(len(lst_all), 2)
        # pagination
        lst_page = self.services.list(self.alice_token, limit=1)
        self.assertEqual(len(lst_page), 1)
        lst_next = self.services.list(self.alice_token, after_id=lst_page[0]["id"], limit=1)
        self.assertEqual(len(lst_next), 1)
        self.assertNotEqual(lst_page[0]["id"], lst_next[0]["id"])

    def test_transitions(self):
        ident = self.services.create(self.alice_token, "Svc", "", "Deliv", 0, 0, operation_key="k1")
        # draft -> published
        self.services.publish(self.alice_token, ident, operation_key="k2")
        self.assertEqual(self.services.get(self.alice_token, ident)["status"], "published")
        # published -> archived
        self.services.archive(self.alice_token, ident, operation_key="k3")
        self.assertEqual(self.services.get(self.alice_token, ident)["status"], "archived")
        # cannot publish after archived
        with self.assertRaises(ServiceError):
            self.services.publish(self.alice_token, ident, operation_key="k4")
        # draft -> archived directly
        ident2 = self.services.create(self.alice_token, "Svc2", "", "Deliv", 0, 0, operation_key="k5")
        self.services.archive(self.alice_token, ident2, operation_key="k6")
        self.assertEqual(self.services.get(self.alice_token, ident2)["status"], "archived")

    def test_invalid_inputs(self):
        with self.assertRaises(ServiceError):
            self.services.create(self.alice_token, "", "", "Deliv", 0, 0, operation_key="k1")
        with self.assertRaises(ServiceError):
            self.services.create(self.alice_token, "T", "", "", 0, 0, operation_key="k1")
        with self.assertRaises(ServiceError):
            self.services.create(self.alice_token, "T", "", "Deliv", -1, 0, operation_key="k1")
        with self.assertRaises(ServiceError):
            self.services.create(self.alice_token, "T", "", "Deliv", 0, 101, operation_key="k1")
        with self.assertRaises(ServiceError):
            self.services.create(self.alice_token, "T", "x"*2001, "Deliv", 0, 0, operation_key="k1")
        with self.assertRaises(ServiceError):
            self.services.create(self.alice_token, "T\x00", "", "Deliv", 0, 0, operation_key="k1")

    def test_idempotency_and_conflict(self):
        ident = self.services.create(self.alice_token, "Svc", "Scope", "Deliv", 100, 1, operation_key="same")
        ident2 = self.services.create(self.alice_token, "Svc", "Scope", "Deliv", 100, 1, operation_key="same")
        self.assertEqual(ident, ident2)
        # same key different payload -> conflict
        with self.assertRaises(ServiceError):
            self.services.create(self.alice_token, "Different", "Scope", "Deliv", 100, 1, operation_key="same")
        # transition idempotency
        self.services.publish(self.alice_token, ident, operation_key="pub")
        self.services.publish(self.alice_token, ident, operation_key="pub")
        self.assertEqual(self.services.get(self.alice_token, ident)["status"], "published")

    def test_concurrent_same_key(self):
        results = []
        errors = []

        def attempt():
            try:
                ident = self.services.create(self.alice_token, "Concurrent", "", "Deliv", 0, 0, operation_key="concurrent")
                results.append(ident)
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=attempt) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        # all should get same id, no conflict beyond first
        self.assertEqual(len(results), 5)
        self.assertEqual(len(set(results)), 1)
        self.assertEqual(len(errors), 0)

    def test_portfolio_create_and_list(self):
        sid = self.services.create(self.alice_token, "Svc", "", "Deliv", 0, 0, operation_key="k1")
        pid = self.portfolio.create(self.alice_token, sid, "Portfolio Item", "Description", operation_key="p1")
        items = self.portfolio.list(self.alice_token, service_id=sid)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["id"], pid)
        # bob cannot see
        with self.assertRaises(AccessDenied):
            self.services.get(self.bob_token, sid)
        self.assertEqual(self.portfolio.list(self.bob_token, service_id=sid), [])
        # invalid service
        with self.assertRaises(AccessDenied):
            self.portfolio.create(self.bob_token, sid, "X", "Y", operation_key="p2")

    def test_dashboard_and_recovery(self):
        sid = self.services.create(self.alice_token, "Svc", "", "Deliv", 12345, 1, operation_key="k1")
        pid = self.portfolio.create(self.alice_token, sid, "Item", "Desc", operation_key="p1")
        metrics = DashboardMetrics(self.auth, self.state).get(self.alice_token)
        self.assertIn("services", metrics)
        self.assertEqual(metrics["services"]["count"], 1)
        self.assertIn("portfolio", metrics)
        self.assertEqual(metrics["portfolio"]["count"], 1)
        # recovery comparison
        cand_path = self.directory / "cand.sqlite3"
        self.state.backup_to(cand_path, confirmed=True)
        cand = StateStorage(cand_path)
        comp = StateRecoveryComparison(self.auth, self.state, cand)
        rec = comp.compare_records(self.alice_token)
        self.assertEqual(rec["tables"]["services"]["matching"], 1)
        self.assertEqual(rec["tables"]["portfolio_items"]["matching"], 1)

    def test_migration_v15_to_v16(self):
        # create v15 DB, then migrate to v16
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
        for v in range(2, 16):
            getattr(state2, f"migrate_to_v{v}")(dir2 / f"before-v{v}.sqlite3", confirmed=True)
        # verify v15 has opportunities but no services
        with state2.transaction() as conn:
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 15)
        # backup and migrate to v16
        backup = dir2 / "before-v16.sqlite3"
        state2.migrate_to_v16(backup, confirmed=True)
        with state2.transaction() as conn:
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 16)
        # now services works
        svc2 = Services(auth2, state2)
        sid = svc2.create(token2, "Migrated Svc", "", "Deliv", 1000, 1, operation_key="k1")
        self.assertEqual(svc2.get(token2, sid)["status"], "draft")
        # existing test for version guard
        from ai_earn.opportunities import Opportunities
        # opportunities should still work on v16
        opp = Opportunities(auth2, state2)
        oid = opp.create(token2, "Opp", "https://example.com", 1000, operation_key="op1")
        self.assertIsNotNone(opp.get(token2, oid))

    def test_version_guard(self):
        # v15 DB should reject services
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
        for v in range(2, 16):
            getattr(state2, f"migrate_to_v{v}")(dir2 / f"before-v{v}.sqlite3", confirmed=True)
        svc2 = Services(auth2, state2)
        with self.assertRaises(StateStorageError):
            svc2.create(token2, "Fail", "", "Deliv", 0, 0, operation_key="k1")

    def test_isolation_and_owner_scoped_list(self):
        s_alice = self.services.create(self.alice_token, "Alice Svc", "", "Deliv", 0, 0, operation_key="k1")
        s_bob = self.services.create(self.bob_token, "Bob Svc", "", "Deliv", 0, 0, operation_key="k1")
        self.assertNotEqual(s_alice, s_bob)
        self.assertEqual(len(self.services.list(self.alice_token)), 1)
        self.assertEqual(len(self.services.list(self.bob_token)), 1)
        self.assertEqual(self.services.list(self.alice_token)[0]["title"], "Alice Svc")

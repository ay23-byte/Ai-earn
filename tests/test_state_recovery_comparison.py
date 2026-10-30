"""Disposable owner-scoped state-copy comparison; no automatic restore."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import os
from pathlib import Path
import sqlite3
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

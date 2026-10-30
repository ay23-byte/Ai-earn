"""Disposable scheduling state only; no task execution or external actions."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, contextmanager
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from ai_earn.auth import AccessDenied, AuthService, SESSION_SECONDS
from ai_earn.auth_storage import AuthStorage
from ai_earn.invoice_drafts import InvoiceDrafts
from ai_earn.order_drafts import OrderDrafts
from ai_earn.state_storage import StateStorage, StateStorageError
from ai_earn.task_queue import QueueError, TaskQueue


class Clock:
    now = 1_800_000_000
    def __call__(self):
        return self.now


@unittest.skipUnless(os.name in {"posix", "nt"}, "requires a supported private filesystem")
class TaskQueueTests(unittest.TestCase):
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
        self.state = StateStorage(self.directory / "state.sqlite3", create=True, confirmed=True)
        for version in range(2, 7):
            getattr(self.state, f"migrate_to_v{version}")(self.directory / f"v{version-1}.sqlite3", confirmed=True)
        self.orders = OrderDrafts(self.auth, self.state)
        self.order = self.orders.create(self.alice, "Existing estimate", 100, operation_key="order")
        self.invoices = InvoiceDrafts(self.auth, self.state)
        self.invoice = self.invoices.create(self.alice, self.order, operation_key="invoice")
        self.state.migrate_to_v7(self.directory / "v6.sqlite3", confirmed=True)
        self.queue = TaskQueue(self.auth, self.state)

    def add(self, key="create", **kw):
        return self.queue.enqueue(self.alice, "Example", operation_key=key, **kw)

    def test_enqueue_cancel_and_identical_retry(self):
        ident = self.add(priority=2)
        self.assertEqual(self.add(priority=2), ident)
        self.assertEqual(self.queue.get(self.alice, ident)["status"], "queued")
        self.assertEqual(self.queue.cancel(self.alice, ident, operation_key="cancel"), ident)
        self.assertEqual(self.queue.cancel(self.alice, ident, operation_key="cancel"), ident)
        with self.assertRaises(QueueError):
            self.queue.cancel(self.alice, ident, operation_key="again")
        with self.state.transaction() as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM state_audit WHERE resource_type='task_queue'").fetchone()[0], 2)

    def test_priority_readiness_limits_and_deadline(self):
        low = self.add("low", priority=1)
        high = self.add("high", priority=9)
        future = self.add("future", priority=9, available_at=self.clock.now + 10)
        expired = self.add("expired", priority=8, deadline_at=self.clock.now + 5)
        self.assertEqual(self.queue.ready(self.alice, limit=1)[0]["id"], high)
        self.clock.now += 10
        ids = [item["id"] for item in self.queue.ready(self.alice)]
        self.assertIn(future, ids)
        self.assertIn(low, ids)
        self.assertNotIn(self.add("later", available_at=self.clock.now + 1), ids)
        self.assertNotIn(expired, ids)
        self.assertEqual(ids[-1], low)

    def test_claim_retry_backoff_fencing_and_exhaustion(self):
        ident = self.add(max_attempts=2, backoff_seconds=10)
        lease1 = self.queue.claim(self.alice, ident, operation_key="claim1", lease_seconds=30)
        self.assertTrue(self.queue.get_lease(self.alice, lease1)["active"])
        self.assertEqual(self.queue.claim(self.alice, ident, operation_key="claim1", lease_seconds=30), lease1)
        self.assertEqual(self.queue.retry(self.alice, ident, lease1, operation_key="retry1"), ident)
        self.assertFalse(self.queue.get_lease(self.alice, lease1)["active"])
        with self.assertRaises(QueueError):
            self.queue.claim(self.alice, ident, operation_key="claim2", lease_seconds=30)
        self.clock.now += 10
        lease2 = self.queue.claim(self.alice, ident, operation_key="claim2", lease_seconds=30)
        self.assertNotEqual(lease1, lease2)
        with self.assertRaises(QueueError):
            self.queue.retry(self.alice, ident, lease1, operation_key="stale")
        self.assertEqual(self.queue.retry(self.alice, ident, lease1, operation_key="retry1"), ident)
        self.assertTrue(self.queue.get_lease(self.alice, lease2)["active"])
        self.queue.retry(self.alice, ident, lease2, operation_key="retry2")
        self.assertEqual(self.queue.get(self.alice, ident)["status"], "exhausted")
        with self.assertRaises(QueueError):
            self.queue.claim(self.alice, ident, operation_key="claim3", lease_seconds=30)

    def test_expired_lease_recovery_after_reopen_and_backup(self):
        ident = self.add(backoff_seconds=3)
        lease = self.queue.claim(self.alice, ident, operation_key="claim", lease_seconds=5)
        with self.assertRaises(QueueError):
            self.queue.recover(self.alice, ident, lease, operation_key="recover")
        self.clock.now += 5
        self.assertFalse(self.queue.get_lease(self.alice, lease)["active"])
        self.assertEqual(self.queue.claim(self.alice, ident, operation_key="claim", lease_seconds=5), lease)
        self.assertFalse(self.queue.get_lease(self.alice, lease)["active"])
        with self.assertRaises(QueueError):
            self.queue.retry(self.alice, ident, lease, operation_key="wrong")
        copy = self.directory / "snapshot.sqlite3"
        self.state.backup_to(copy, confirmed=True)
        reopened = TaskQueue(self.auth, StateStorage(copy))
        self.assertEqual(reopened.recover(self.alice, ident, lease, operation_key="recover"), ident)
        self.assertEqual(reopened.recover(self.alice, ident, lease, operation_key="recover"), ident)
        self.assertEqual(reopened.get(self.alice, ident)["status"], "queued")
        self.assertEqual(self.queue.get(self.alice, ident)["status"], "leased")
        self.clock.now += 3
        self.assertNotEqual(reopened.claim(self.alice, ident, operation_key="next", lease_seconds=5), lease)

    def test_deadline_caps_lease_and_recovery_exhausts(self):
        ident = self.add(deadline_at=self.clock.now + 5)
        lease = self.queue.claim(self.alice, ident, operation_key="claim", lease_seconds=60)
        self.assertEqual(self.queue.get_lease(self.alice, lease)["lease_until"], self.clock.now + 5)
        self.clock.now += 5
        self.queue.recover(self.alice, ident, lease, operation_key="recover")
        self.assertEqual(self.queue.get(self.alice, ident)["status"], "exhausted")

    def test_backoff_past_deadline_exhausts_without_stranding_queue(self):
        ident = self.add(deadline_at=self.clock.now + 30, backoff_seconds=30)
        lease = self.queue.claim(self.alice, ident, operation_key="claim", lease_seconds=5)
        self.queue.retry(self.alice, ident, lease, operation_key="retry")
        self.assertEqual(self.queue.get(self.alice, ident)["status"], "exhausted")
        self.assertEqual(self.queue.ready(self.alice), [])

    def test_cross_owner_denial_and_scoped_keys(self):
        ident = self.add()
        lease = self.queue.claim(self.alice, ident, operation_key="claim", lease_seconds=30)
        for fn in (lambda: self.queue.get(self.bob, ident),
                   lambda: self.queue.claim(self.bob, ident, operation_key="claim", lease_seconds=30),
                   lambda: self.queue.get_lease(self.bob, lease),
                   lambda: self.queue.recover(self.bob, ident, lease, operation_key="recover")):
            with self.assertRaises(AccessDenied):
                fn()
        bob = self.queue.enqueue(self.bob, "Other", operation_key="create")
        self.assertNotEqual(ident, bob)
        self.assertEqual([x["id"] for x in self.queue.ready(self.bob)], [bob])

    def test_invalid_values_and_key_collisions(self):
        for kw in ({"priority": True}, {"priority": 10}, {"max_attempts": 0}, {"max_attempts": 11},
                   {"backoff_seconds": -1}, {"available_at": 1.0}, {"deadline_at": self.clock.now},
                   {"deadline_at": self.clock.now + 1, "available_at": self.clock.now + 2}):
            with self.subTest(kw=kw):
                with self.assertRaises(QueueError):
                    self.add(**kw)
        ident = self.add()
        for fn in (lambda: self.add(priority=1),
                   lambda: self.queue.claim(self.alice, ident, operation_key="create", lease_seconds=1),
                   lambda: self.queue.enqueue(self.alice, "Other", operation_key="order"),
                   lambda: self.queue.claim(self.alice, ident, operation_key="claim", lease_seconds=True)):
            with self.assertRaises(QueueError):
                fn()

    def test_invalid_revoked_and_expired_session(self):
        self.auth.logout(self.bob)
        for token in (self.bob, "", "x" * 43):
            with self.assertRaises(AccessDenied):
                self.queue.enqueue(token, "Scope", operation_key="key")
        self.clock.now += SESSION_SECONDS
        with self.assertRaises(AccessDenied):
            self.queue.ready(self.alice)

    def test_competing_claims_and_same_key_retries(self):
        ident = self.add()
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(self.queue.claim, self.alice, ident, operation_key="same", lease_seconds=30) for _ in range(2)]
            ids = [f.result(timeout=10) for f in futures]
        self.assertEqual(ids[0], ids[1])
        with self.assertRaises(QueueError):
            self.queue.claim(self.alice, ident, operation_key="other", lease_seconds=30)
        with self.state.transaction() as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM queue_leases").fetchone()[0], 1)
            self.assertEqual(conn.execute("SELECT count(*) FROM state_audit WHERE event='queue_leased'").fetchone()[0], 1)

    def test_concurrent_different_keys_only_one_claims(self):
        ident = self.add()
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(self.queue.claim, self.alice, ident, operation_key=k, lease_seconds=30) for k in ("one", "two")]
            results = []
            for future in futures:
                try:
                    results.append(future.result(timeout=10))
                except QueueError:
                    pass
        self.assertEqual(len(results), 1)

    def test_failure_rolls_back_record_audit_key_and_lease(self):
        for helper in ("_audit", "_remember"):
            with patch("ai_earn.task_queue." + helper, side_effect=RuntimeError("injected")):
                with self.assertRaises(RuntimeError):
                    self.add()
        ident = self.add()
        for helper in ("_audit", "_remember"):
            with patch("ai_earn.task_queue." + helper, side_effect=RuntimeError("injected")):
                with self.assertRaises(RuntimeError):
                    self.queue.claim(self.alice, ident, operation_key="claim", lease_seconds=30)
        self.assertEqual(self.queue.get(self.alice, ident)["status"], "queued")
        with self.state.transaction() as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM queue_leases").fetchone()[0], 0)
            self.assertEqual(conn.execute("SELECT count(*) FROM state_operations WHERE operation_key='claim'").fetchone()[0], 0)

    def test_expiry_before_commit_rolls_back_claim(self):
        ident = self.add()
        from ai_earn.task_queue import _audit
        def expire(*args):
            _audit(*args)
            self.clock.now += 2
        with patch("ai_earn.task_queue._audit", side_effect=expire):
            with self.assertRaises(QueueError):
                self.queue.claim(self.alice, ident, operation_key="claim", lease_seconds=1)
        self.assertEqual(self.queue.get(self.alice, ident)["status"], "queued")
        with self.state.transaction() as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM queue_leases").fetchone()[0], 0)

    def test_session_expiry_during_mutation_rolls_back(self):
        from ai_earn.task_queue import _audit
        def expire(*args):
            _audit(*args)
            self.clock.now += SESSION_SECONDS
        with patch("ai_earn.task_queue._audit", side_effect=expire):
            with self.assertRaises(AccessDenied):
                self.add()
        with self.state.transaction() as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM task_queue").fetchone()[0], 0)
            self.assertEqual(conn.execute("SELECT count(*) FROM state_operations WHERE operation_key='create'").fetchone()[0], 0)

    def test_reschedule_failure_rolls_back_and_lease_stays_active(self):
        ident = self.add()
        lease = self.queue.claim(self.alice, ident, operation_key="claim", lease_seconds=30)
        with patch("ai_earn.task_queue._remember", side_effect=RuntimeError("injected")):
            with self.assertRaises(RuntimeError):
                self.queue.retry(self.alice, ident, lease, operation_key="retry")
        self.assertTrue(self.queue.get_lease(self.alice, lease)["active"])
        self.queue.retry(self.alice, ident, lease, operation_key="retry")

    def test_ambiguous_auth_exit_returns_same_lease(self):
        ident = self.add()
        original = self.auth.storage.transaction
        @contextmanager
        def fail_exit():
            with original() as conn:
                yield conn
                raise RuntimeError("auth exit failure")
        with patch.object(self.auth.storage, "transaction", fail_exit):
            with self.assertRaises(RuntimeError):
                self.queue.claim(self.alice, ident, operation_key="claim", lease_seconds=30)
        lease = self.queue.claim(self.alice, ident, operation_key="claim", lease_seconds=30)
        self.assertTrue(self.queue.get_lease(self.alice, lease)["active"])
        with self.state.transaction() as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM queue_leases").fetchone()[0], 1)

    def test_prior_services_and_backup_compatibility(self):
        self.assertEqual(self.orders.create(self.alice, "Existing estimate", 100, operation_key="order"), self.order)
        self.assertEqual(self.invoices.create(self.alice, self.order, operation_key="invoice"), self.invoice)
        prior = StateStorage(self.directory / "v6.sqlite3")
        self.assertEqual(InvoiceDrafts(self.auth, prior).get(self.alice, self.invoice)["status"], "draft")
        with self.assertRaises(StateStorageError):
            TaskQueue(self.auth, prior).enqueue(self.alice, "Scope", operation_key="key")

    def test_schema_constraints_and_owner_foreign_key(self):
        ident = self.add()
        bob = self.auth.profile(self.bob)["id"]
        with self.assertRaises(StateStorageError):
            with self.state.transaction() as conn:
                conn.execute("INSERT INTO queue_leases VALUES(?,?,?,?,?)", ("a"*32, bob, ident, 1, self.clock.now + 5))
        with self.assertRaises(StateStorageError):
            with self.state.transaction() as conn:
                conn.execute("UPDATE task_queue SET status='completed' WHERE id=?", (ident,))
        with self.assertRaises(StateStorageError):
            with self.state.transaction() as conn:
                conn.execute("UPDATE task_queue SET status='leased' WHERE id=?", (ident,))
        with self.assertRaises(StateStorageError):
            with self.state.transaction() as conn:
                conn.execute("UPDATE task_queue SET status='leased',current_lease_id=?,lease_until=? WHERE id=?",
                             ("b"*32, self.clock.now + 5, ident))

    def test_mismatched_current_lease_is_not_used_or_backed_up(self):
        ident = self.add()
        lease = self.queue.claim(self.alice, ident, operation_key="claim", lease_seconds=30)
        with closing(sqlite3.connect(self.state.path)) as conn, conn:
            conn.execute("UPDATE queue_leases SET lease_until=lease_until+1 WHERE id=?", (lease,))
            self.assertEqual(conn.execute("PRAGMA quick_check").fetchone()[0], "ok")
            self.assertEqual(conn.execute("PRAGMA foreign_key_check").fetchall(), [])
        for fn in (lambda: self.queue.get(self.alice, ident),
                   lambda: self.queue.get_lease(self.alice, lease),
                   lambda: self.queue.retry(self.alice, ident, lease, operation_key="retry"),
                   lambda: self.queue.claim(self.alice, ident, operation_key="claim", lease_seconds=30)):
            with self.assertRaises(StateStorageError):
                fn()
        destination = self.directory / "inconsistent.sqlite3"
        with self.assertRaises(StateStorageError):
            self.state.backup_to(destination, confirmed=True)
        self.assertFalse(destination.exists())

    def test_missing_lease_history_is_not_returned_as_ready(self):
        ident = self.add(backoff_seconds=0)
        lease = self.queue.claim(self.alice, ident, operation_key="claim", lease_seconds=30)
        self.queue.retry(self.alice, ident, lease, operation_key="retry")
        with closing(sqlite3.connect(self.state.path)) as conn, conn:
            conn.execute("DELETE FROM queue_leases WHERE id=?", (lease,))
            self.assertEqual(conn.execute("PRAGMA quick_check").fetchone()[0], "ok")
            self.assertEqual(conn.execute("PRAGMA foreign_key_check").fetchall(), [])
        for fn in (lambda: self.queue.ready(self.alice),
                   lambda: self.add(backoff_seconds=0),
                   lambda: self.queue.claim(self.alice, ident, operation_key="new", lease_seconds=30)):
            with self.assertRaises(StateStorageError):
                fn()
        self.assertEqual(self.queue.ready(self.bob), [])
        destination = self.directory / "missing-history.sqlite3"
        with self.assertRaises(StateStorageError):
            self.state.backup_to(destination, confirmed=True)
        self.assertFalse(destination.exists())

    def test_attempt_mismatch_blocks_upgrade_before_backup_creation(self):
        ident = self.add()
        self.queue.claim(self.alice, ident, operation_key="claim", lease_seconds=30)
        with closing(sqlite3.connect(self.state.path)) as conn, conn:
            conn.execute("UPDATE task_queue SET attempts=0 WHERE id=?", (ident,))
            self.assertEqual(conn.execute("PRAGMA quick_check").fetchone()[0], "ok")
            self.assertEqual(conn.execute("PRAGMA foreign_key_check").fetchall(), [])
        backup = self.directory / "v7-invalid.sqlite3"
        with self.assertRaises(StateStorageError):
            self.state.migrate_to_v8(backup, confirmed=True)
        self.assertFalse(backup.exists())
        with closing(sqlite3.connect(self.state.path)) as conn:
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 7)

    def test_lease_beyond_deadline_fails_even_when_both_expiries_match(self):
        ident = self.add(deadline_at=self.clock.now + 10)
        lease = self.queue.claim(self.alice, ident, operation_key="claim", lease_seconds=5)
        with closing(sqlite3.connect(self.state.path)) as conn, conn:
            conn.execute("UPDATE task_queue SET lease_until=? WHERE id=?", (self.clock.now + 11, ident))
            conn.execute("UPDATE queue_leases SET lease_until=? WHERE id=?", (self.clock.now + 11, lease))
            self.assertEqual(conn.execute("PRAGMA quick_check").fetchone()[0], "ok")
            self.assertEqual(conn.execute("PRAGMA foreign_key_check").fetchall(), [])
        with self.assertRaises(StateStorageError):
            self.queue.get_lease(self.alice, lease)
        with self.assertRaises(StateStorageError):
            self.state.backup_to(self.directory / "deadline-invalid.sqlite3", confirmed=True)

    def test_corrupted_backup_copy_is_rejected_without_changing_source(self):
        ident = self.add()
        lease = self.queue.claim(self.alice, ident, operation_key="claim", lease_seconds=30)
        destination = self.directory / "bad-copy.sqlite3"
        from ai_earn.state_storage import _copy_database
        def corrupt_copy(source, target):
            _copy_database(source, target)
            target.execute("UPDATE queue_leases SET lease_until=lease_until+1 WHERE id=?", (lease,))
            target.commit()
        with patch("ai_earn.state_storage._copy_database", side_effect=corrupt_copy):
            with self.assertRaises(StateStorageError):
                self.state.backup_to(destination, confirmed=True)
        self.assertTrue(destination.exists())  # Failed copies are retained for inspection, not trusted.
        self.assertTrue(self.queue.get_lease(self.alice, lease)["active"])

    def test_migration_failure_confirmation_and_tampering(self):
        prior = StateStorage(self.directory / "v6.sqlite3")
        backup = self.directory / "pre.sqlite3"
        with self.assertRaises(StateStorageError):
            prior.migrate_to_v7(backup, confirmed=1)
        self.assertFalse(backup.exists())
        backup.write_bytes(b"preserve")
        with self.assertRaises(StateStorageError):
            prior.migrate_to_v7(backup, confirmed=True)
        self.assertEqual(backup.read_bytes(), b"preserve")
        failed = self.directory / "failed.sqlite3"
        def fail(conn):
            conn.execute("CREATE TABLE task_queue (id TEXT)")
            raise RuntimeError("failure")
        with patch("ai_earn.state_storage._apply_v7", side_effect=fail):
            with self.assertRaises(RuntimeError):
                prior.migrate_to_v7(failed, confirmed=True)
        for store in (prior, StateStorage(failed)):
            with store.transaction() as conn:
                self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 6)
        with closing(sqlite3.connect(self.state.path)) as conn, conn:
            conn.execute("DROP TABLE queue_leases")
            conn.execute("CREATE TABLE queue_leases(id TEXT)")
        with self.assertRaises(StateStorageError):
            StateStorage(self.state.path)

    def test_migration_wal_snapshot_preserves_committed_frames(self):
        prior = StateStorage(self.directory / "v6.sqlite3")
        backup = self.directory / "wal.sqlite3"
        with closing(sqlite3.connect(prior.path)) as conn:
            self.assertEqual(conn.execute("PRAGMA journal_mode=WAL").fetchone()[0].lower(), "wal")
            conn.execute("UPDATE order_drafts SET title=? WHERE id=?", ("WAL estimate", self.order))
            conn.commit()
            self.assertTrue(Path(str(prior.path) + "-wal").exists())
            prior.migrate_to_v7(backup, confirmed=True)
            with StateStorage(backup).transaction() as snapshot:
                self.assertEqual(snapshot.execute("SELECT title FROM order_drafts WHERE id=?", (self.order,)).fetchone()[0], "WAL estimate")
                self.assertEqual(snapshot.execute("PRAGMA user_version").fetchone()[0], 6)

    def test_migration_rejects_invalid_backup_copy(self):
        prior = StateStorage(self.directory / "v6.sqlite3")
        backup = self.directory / "bad.sqlite3"
        with patch("ai_earn.state_storage._check_integrity", side_effect=[None, StateStorageError("bad copy")]):
            with self.assertRaises(StateStorageError):
                prior.migrate_to_v7(backup, confirmed=True)
        self.assertTrue(backup.exists())
        with prior.transaction() as conn:
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 6)

    def test_crash_during_migration_leaves_restorable_v6_backup(self):
        source = self.directory / "v6.sqlite3"
        backup = self.directory / "crash.sqlite3"
        code = '''
import os, sys
from pathlib import Path
import ai_earn.state_storage as storage
def crash(conn):
    conn.execute("CREATE TABLE task_queue (id TEXT)")
    os._exit(37)
storage._apply_v7 = crash
storage.StateStorage(Path(sys.argv[1])).migrate_to_v7(Path(sys.argv[2]), confirmed=True)
'''
        result = subprocess.run([sys.executable, "-c", code, str(source), str(backup)], timeout=20)
        self.assertEqual(result.returncode, 37)
        restored = self.directory / "restored.sqlite3"
        StateStorage(backup).backup_to(restored, confirmed=True)
        self.assertEqual(InvoiceDrafts(self.auth, StateStorage(restored)).get(self.alice, self.invoice)["status"], "draft")

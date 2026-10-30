"""Disposable local task state and review tests; no task execution."""
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
from ai_earn.order_drafts import OrderDrafts
from ai_earn.state_storage import StateStorage, StateStorageError
from ai_earn.task_queue import TaskQueue
from ai_earn.task_workflows import TaskWorkflows, WorkflowError


class Clock:
    now = 1_800_000_000
    def __call__(self):
        return self.now


@unittest.skipUnless(os.name in {"posix", "nt"}, "requires a supported private filesystem")
class TaskWorkflowTests(unittest.TestCase):
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
        for version in range(2, 8):
            getattr(self.state, f"migrate_to_v{version}")(self.directory / f"v{version-1}.sqlite3", confirmed=True)
        self.queue = TaskQueue(self.auth, self.state)
        self.queued = self.queue.enqueue(self.alice, "Existing queue", operation_key="queue-key")
        self.state.migrate_to_v8(self.directory / "v7.sqlite3", confirmed=True)
        self.tasks = TaskWorkflows(self.auth, self.state)

    def create(self, key="create"):
        return self.tasks.create(self.alice, "Example task", operation_key=key)

    def started(self):
        ident = self.create()
        self.tasks.start(self.alice, ident, operation_key="start")
        return ident

    def test_local_lifecycle_and_review_gate(self):
        ident = self.started()
        with self.assertRaises(WorkflowError):
            self.tasks.review(self.alice, ident, "result", approved=True, confirmed=True, operation_key="review")
        self.tasks.submit(self.alice, ident, "result", operation_key="submit")
        for confirmed in (False, 1):
            with self.assertRaises(WorkflowError):
                self.tasks.review(self.alice, ident, "result", approved=True, confirmed=confirmed, operation_key="review")
        with self.assertRaises(WorkflowError):
            self.tasks.review(self.alice, ident, "different", approved=True, confirmed=True, operation_key="review")
        self.assertEqual(self.tasks.get(self.alice, ident)["status"], "pending_review")
        self.tasks.review(self.alice, ident, "result", approved=True, confirmed=True, operation_key="review")
        self.assertEqual(self.tasks.review(self.alice, ident, "result", approved=True, confirmed=True, operation_key="review"), ident)
        self.assertEqual(self.tasks.get(self.alice, ident)["status"], "completed")
        with self.assertRaises(WorkflowError):
            self.tasks.cancel(self.alice, ident, operation_key="cancel")

    def test_reject_then_resubmit_and_review(self):
        ident = self.started()
        self.tasks.submit(self.alice, ident, "first", operation_key="submit")
        self.tasks.review(self.alice, ident, "first", approved=False, confirmed=True, operation_key="reject")
        self.assertEqual(self.tasks.get(self.alice, ident)["status"], "in_progress")
        self.assertIsNone(self.tasks.get(self.alice, ident)["output_hash"])
        self.tasks.submit(self.alice, ident, "second", operation_key="submit2")
        self.assertEqual(self.tasks.review(self.alice, ident, "first", approved=False, confirmed=True, operation_key="reject"), ident)
        self.assertEqual(self.tasks.get(self.alice, ident)["status"], "pending_review")
        self.tasks.review(self.alice, ident, "second", approved=True, confirmed=True, operation_key="approve")
        self.assertEqual(self.tasks.get(self.alice, ident)["status"], "completed")

    def test_cancel_pending_clears_output_and_disallows_further_changes(self):
        ident = self.started()
        self.tasks.submit(self.alice, ident, "result", operation_key="submit")
        self.tasks.cancel(self.alice, ident, operation_key="cancel")
        self.assertIsNone(self.tasks.get(self.alice, ident)["output_hash"])
        with self.assertRaises(WorkflowError):
            self.tasks.review(self.alice, ident, "result", approved=True, confirmed=True, operation_key="review")
        self.assertEqual(self.tasks.cancel(self.alice, ident, operation_key="cancel"), ident)

    def test_invalid_outputs_and_boolean_decisions(self):
        ident = self.started()
        for output in ("", " ", "x"*4097, "\ud800", "bad\x00text", None):
            with self.subTest(output=repr(output)):
                with self.assertRaises(WorkflowError):
                    self.tasks.submit(self.alice, ident, output, operation_key="submit")
        self.tasks.submit(self.alice, ident, "a\nb\t", operation_key="submit")
        with self.assertRaises(WorkflowError):
            self.tasks.review(self.alice, ident, "a\nb\t", approved=1, confirmed=True, operation_key="review")

    def test_output_bytes_and_digest_are_exact_and_bounded(self):
        ident = self.started()
        value = "ह" * 1365 + "a"
        self.tasks.submit(self.alice, ident, value, operation_key="submit")
        row = self.tasks.get(self.alice, ident)
        self.assertEqual(row["output_size"], 4096)
        self.assertEqual(len(row["output_hash"]), 64)
        with self.assertRaises(WorkflowError):
            self.tasks.review(self.alice, ident, value + "b", approved=True, confirmed=True, operation_key="review")

    def test_cross_owner_and_unknown_resources_denied(self):
        ident = self.started()
        for fn in (lambda: self.tasks.get(self.bob, ident),
                   lambda: self.tasks.get(self.bob, "f"*32),
                   lambda: self.tasks.submit(self.bob, ident, "output", operation_key="submit"),
                   lambda: self.tasks.review(self.bob, ident, "output", approved=True, confirmed=True, operation_key="review"),
                   lambda: self.tasks.cancel(self.bob, ident, operation_key="cancel")):
            with self.assertRaises(AccessDenied):
                fn()
        bob = self.tasks.create(self.bob, "Example task", operation_key="create")
        self.assertNotEqual(ident, bob)

    def test_invalid_expired_and_revoked_sessions(self):
        self.auth.logout(self.bob)
        for token in (self.bob, "", "x"*43):
            with self.assertRaises(AccessDenied):
                self.tasks.create(token, "Example", operation_key="key")
        self.clock.now += SESSION_SECONDS
        with self.assertRaises(AccessDenied):
            self.tasks.create(self.alice, "Example", operation_key="key")

    def test_idempotency_and_cross_resource_collisions(self):
        ident = self.create()
        self.assertEqual(self.create(), ident)
        for fn in (lambda: self.tasks.create(self.alice, "Different", operation_key="create"),
                   lambda: self.tasks.start(self.alice, ident, operation_key="create"),
                   lambda: self.tasks.create(self.alice, "Example", operation_key="queue-key")):
            with self.assertRaises(WorkflowError):
                fn()
        self.tasks.start(self.alice, ident, operation_key="start")
        self.tasks.submit(self.alice, ident, "first", operation_key="submit")
        with self.assertRaises(WorkflowError):
            self.tasks.submit(self.alice, ident, "second", operation_key="submit")

    def test_concurrent_identical_retries_write_once(self):
        ident = self.started()
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(self.tasks.submit, self.alice, ident, "output", operation_key="submit") for _ in range(2)]
            self.assertEqual([f.result(timeout=10) for f in futures], [ident, ident])
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(self.tasks.review, self.alice, ident, "output", approved=True, confirmed=True, operation_key="review") for _ in range(2)]
            self.assertEqual([f.result(timeout=10) for f in futures], [ident, ident])
        with self.state.transaction() as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM state_audit WHERE event='workflow_submitted'").fetchone()[0], 1)
            self.assertEqual(conn.execute("SELECT count(*) FROM state_audit WHERE event='workflow_approved'").fetchone()[0], 1)

    def test_competing_decisions_only_one_transition(self):
        ident = self.started()
        self.tasks.submit(self.alice, ident, "output", operation_key="submit")
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(self.tasks.review, self.alice, ident, "output", approved=decision, confirmed=True, operation_key=key)
                       for decision, key in ((True, "yes"), (False, "no"))]
            succeeded = 0
            for future in futures:
                try:
                    future.result(timeout=10)
                    succeeded += 1
                except WorkflowError:
                    pass
        self.assertEqual(succeeded, 1)
        with self.state.transaction() as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM state_audit WHERE event IN ('workflow_approved','workflow_rejected')").fetchone()[0], 1)

    def test_no_plaintext_output_in_task_audit_or_operation_tables(self):
        ident = self.started()
        self.tasks.submit(self.alice, ident, "synthetic-private-output", operation_key="submit")
        self.tasks.review(self.alice, ident, "synthetic-private-output", approved=True, confirmed=True, operation_key="review")
        with self.state.transaction() as conn:
            for table in ("task_workflows", "state_audit", "state_operations"):
                rows = [tuple(row) for row in conn.execute(f"SELECT * FROM {table}")]
                self.assertNotIn("synthetic-private-output", str(rows))
                self.assertNotIn(self.alice, str(rows))

    def test_failure_injection_rolls_back_mutation_audit_and_key(self):
        ident = self.started()
        for helper in ("_audit", "_remember"):
            with patch("ai_earn.task_workflows." + helper, side_effect=RuntimeError("injected")):
                with self.assertRaises(RuntimeError):
                    self.tasks.submit(self.alice, ident, "output", operation_key="submit")
        self.assertEqual(self.tasks.get(self.alice, ident)["status"], "in_progress")
        with self.state.transaction() as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM state_operations WHERE operation_key='submit'").fetchone()[0], 0)
            self.assertEqual(conn.execute("SELECT count(*) FROM state_audit WHERE event='workflow_submitted'").fetchone()[0], 0)

    def test_review_failure_rolls_back_decision(self):
        ident = self.started()
        self.tasks.submit(self.alice, ident, "output", operation_key="submit")
        with patch("ai_earn.task_workflows._remember", side_effect=RuntimeError("injected")):
            with self.assertRaises(RuntimeError):
                self.tasks.review(self.alice, ident, "output", approved=True, confirmed=True, operation_key="review")
        self.assertEqual(self.tasks.get(self.alice, ident)["status"], "pending_review")
        self.tasks.review(self.alice, ident, "output", approved=True, confirmed=True, operation_key="review")
        self.assertEqual(self.tasks.get(self.alice, ident)["status"], "completed")

    def test_session_expiry_during_mutation_rolls_back(self):
        ident = self.started()
        from ai_earn.task_workflows import _audit
        def expire(*args):
            _audit(*args)
            self.clock.now += SESSION_SECONDS
        with patch("ai_earn.task_workflows._audit", side_effect=expire):
            with self.assertRaises(AccessDenied):
                self.tasks.submit(self.alice, ident, "output", operation_key="submit")
        with self.state.transaction() as conn:
            self.assertEqual(conn.execute("SELECT status FROM task_workflows WHERE id=?", (ident,)).fetchone()[0], "in_progress")

    def test_ambiguous_auth_exit_review_retry(self):
        ident = self.started()
        self.tasks.submit(self.alice, ident, "output", operation_key="submit")
        original = self.auth.storage.transaction
        @contextmanager
        def fail_exit():
            with original() as conn:
                yield conn
                raise RuntimeError("auth exit failure")
        with patch.object(self.auth.storage, "transaction", fail_exit):
            with self.assertRaises(RuntimeError):
                self.tasks.review(self.alice, ident, "output", approved=True, confirmed=True, operation_key="review")
        self.assertEqual(self.tasks.review(self.alice, ident, "output", approved=True, confirmed=True, operation_key="review"), ident)
        self.assertEqual(self.tasks.get(self.alice, ident)["status"], "completed")

    def test_prior_services_and_backup_compatibility(self):
        self.assertEqual(self.queue.enqueue(self.alice, "Existing queue", operation_key="queue-key"), self.queued)
        order = OrderDrafts(self.auth, self.state).create(self.alice, "Estimate", 100, operation_key="order")
        self.assertEqual(OrderDrafts(self.auth, self.state).get(self.alice, order)["status"], "draft")
        prior = StateStorage(self.directory / "v7.sqlite3")
        with self.assertRaises(StateStorageError):
            TaskWorkflows(self.auth, prior).create(self.alice, "Example", operation_key="key")
        ident = self.started()
        self.tasks.submit(self.alice, ident, "output", operation_key="submit")
        backup = self.directory / "v8-copy.sqlite3"
        self.state.backup_to(backup, confirmed=True)
        restored = TaskWorkflows(self.auth, StateStorage(backup))
        restored.review(self.alice, ident, "output", approved=True, confirmed=True, operation_key="review")
        self.assertEqual(restored.get(self.alice, ident)["status"], "completed")
        self.assertEqual(self.tasks.get(self.alice, ident)["status"], "pending_review")

    def test_v7_active_lease_survives_migration_and_recovers(self):
        prior = StateStorage(self.directory / "v7.sqlite3")
        queue = TaskQueue(self.auth, prior)
        lease = queue.claim(self.alice, self.queued, operation_key="claim", lease_seconds=5)
        backup = self.directory / "active-v7.sqlite3"
        prior.migrate_to_v8(backup, confirmed=True)
        self.assertTrue(queue.get_lease(self.alice, lease)["active"])
        self.clock.now += 5
        queue.recover(self.alice, self.queued, lease, operation_key="recover")
        self.assertEqual(queue.get(self.alice, self.queued)["status"], "queued")
        saved = TaskQueue(self.auth, StateStorage(backup))
        self.assertEqual(saved.get(self.alice, self.queued)["status"], "leased")
        self.assertFalse(saved.get_lease(self.alice, lease)["active"])

    def test_v8_backup_refuses_inconsistent_queue_history(self):
        lease = self.queue.claim(self.alice, self.queued, operation_key="claim", lease_seconds=30)
        with closing(sqlite3.connect(self.state.path)) as conn, conn:
            conn.execute("UPDATE queue_leases SET lease_until=lease_until+1 WHERE id=?", (lease,))
            self.assertEqual(conn.execute("PRAGMA quick_check").fetchone()[0], "ok")
            self.assertEqual(conn.execute("PRAGMA foreign_key_check").fetchall(), [])
        copy = self.directory / "invalid-v8.sqlite3"
        with self.assertRaises(StateStorageError):
            self.state.backup_to(copy, confirmed=True)
        self.assertFalse(copy.exists())
        with self.assertRaises(StateStorageError):
            self.queue.get_lease(self.alice, lease)

    def test_schema_rejects_illegal_states_and_output_combinations(self):
        ident = self.create()
        for status, digest, size in (("paid", None, None), ("completed", None, None), ("planned", b"a"*32, 10), ("pending_review", b"a"*31, 10)):
            with self.subTest(status=status, size=size):
                with self.assertRaises(StateStorageError):
                    with self.state.transaction() as conn:
                        conn.execute("UPDATE task_workflows SET status=?,output_hash=?,output_size=? WHERE id=?", (status, digest, size, ident))

    def test_migration_failure_non_overwrite_and_tamper(self):
        prior = StateStorage(self.directory / "v7.sqlite3")
        backup = self.directory / "new.sqlite3"
        for confirmed in (False, 1):
            with self.assertRaises(StateStorageError):
                prior.migrate_to_v8(backup, confirmed=confirmed)
        self.assertFalse(backup.exists())
        backup.write_bytes(b"preserve")
        with self.assertRaises(StateStorageError):
            prior.migrate_to_v8(backup, confirmed=True)
        self.assertEqual(backup.read_bytes(), b"preserve")
        failed = self.directory / "failed.sqlite3"
        def fail(conn):
            conn.execute("CREATE TABLE task_workflows (id TEXT)")
            raise RuntimeError("failure")
        with patch("ai_earn.state_storage._apply_v8", side_effect=fail):
            with self.assertRaises(RuntimeError):
                prior.migrate_to_v8(failed, confirmed=True)
        for store in (prior, StateStorage(failed)):
            with store.transaction() as conn:
                self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 7)
        with closing(sqlite3.connect(self.state.path)) as conn, conn:
            conn.execute("DROP TABLE task_workflows")
            conn.execute("""CREATE TABLE task_workflows (
                id TEXT PRIMARY KEY NOT NULL, owner_id TEXT NOT NULL, title TEXT NOT NULL,
                status TEXT NOT NULL, output_hash BLOB, output_size INTEGER,
                created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL
            )""")
        with self.assertRaises(StateStorageError):
            StateStorage(self.state.path)

    def test_migration_integrity_failure_prevents_upgrade(self):
        prior = StateStorage(self.directory / "v7.sqlite3")
        backup = self.directory / "bad.sqlite3"
        with patch("ai_earn.state_storage._check_integrity", side_effect=[None, StateStorageError("bad copy")]):
            with self.assertRaises(StateStorageError):
                prior.migrate_to_v8(backup, confirmed=True)
        self.assertTrue(backup.exists())
        with prior.transaction() as conn:
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 7)

    def test_wal_migration_includes_committed_frames(self):
        prior = StateStorage(self.directory / "v7.sqlite3")
        backup = self.directory / "wal.sqlite3"
        with closing(sqlite3.connect(prior.path)) as conn:
            self.assertEqual(conn.execute("PRAGMA journal_mode=WAL").fetchone()[0].lower(), "wal")
            conn.execute("UPDATE task_queue SET title=? WHERE id=?", ("WAL queue", self.queued))
            conn.commit()
            self.assertTrue(Path(str(prior.path) + "-wal").exists())
            prior.migrate_to_v8(backup, confirmed=True)
            with StateStorage(backup).transaction() as snapshot:
                self.assertEqual(snapshot.execute("SELECT title FROM task_queue WHERE id=?", (self.queued,)).fetchone()[0], "WAL queue")
                self.assertEqual(snapshot.execute("PRAGMA user_version").fetchone()[0], 7)

    def test_crash_during_migration_leaves_restorable_v7_backup(self):
        source = self.directory / "v7.sqlite3"
        backup = self.directory / "crash.sqlite3"
        code = '''
import os, sys
from pathlib import Path
import ai_earn.state_storage as storage
def crash(conn):
    conn.execute("CREATE TABLE task_workflows (id TEXT)")
    os._exit(37)
storage._apply_v8 = crash
storage.StateStorage(Path(sys.argv[1])).migrate_to_v8(Path(sys.argv[2]), confirmed=True)
'''
        result = subprocess.run([sys.executable, "-c", code, str(source), str(backup)], timeout=20)
        self.assertEqual(result.returncode, 37)
        restored = self.directory / "restored.sqlite3"
        StateStorage(backup).backup_to(restored, confirmed=True)
        self.assertEqual(TaskQueue(self.auth, StateStorage(restored)).get(self.alice, self.queued)["status"], "queued")

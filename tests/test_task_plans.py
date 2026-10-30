"""Disposable tests for planned tasks; no worker, completion, or external action."""
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
from ai_earn.opportunity_drafts import DraftError, OpportunityDrafts
from ai_earn.state_storage import StateStorage, StateStorageError
from ai_earn.task_plans import TaskError, TaskPlans


class Clock:
    now = 1_800_000_000
    def __call__(self):
        return self.now


@unittest.skipUnless(os.name in {"posix", "nt"}, "requires a supported private filesystem")
class TaskPlanTests(unittest.TestCase):
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
        self.state.migrate_to_v2(self.directory / "v1.sqlite3", confirmed=True)
        self.state.migrate_to_v3(self.directory / "v2.sqlite3", confirmed=True)
        self.opportunities = OpportunityDrafts(self.auth, self.state)
        self.opp = self.opportunities.create(self.alice, "Original opportunity", operation_key="opp-create")
        self.state.migrate_to_v4(self.directory / "v3.sqlite3", confirmed=True)
        self.tasks = TaskPlans(self.auth, self.state)

    def test_create_read_cancel_and_idempotent_retries(self):
        identifier = self.tasks.create(self.alice, "Plan something", operation_key="task-create")
        self.assertEqual(self.tasks.create(self.alice, "Plan something", operation_key="task-create"), identifier)
        self.assertEqual(self.tasks.get(self.alice, identifier)["status"], "planned")
        self.assertEqual(self.tasks.cancel(self.alice, identifier, operation_key="task-cancel"), identifier)
        self.assertEqual(self.tasks.cancel(self.alice, identifier, operation_key="task-cancel"), identifier)
        self.assertEqual(self.tasks.get(self.alice, identifier)["status"], "cancelled")
        with self.state.transaction() as conn:
            events = conn.execute("SELECT event, actor_id, resource_id FROM state_audit WHERE resource_type='task_plan' ORDER BY id").fetchall()
            self.assertEqual([row[0] for row in events], ["task_planned", "task_cancelled"])
            self.assertTrue(all(row[2] == identifier for row in events))
            self.assertNotIn(self.alice, str([tuple(row) for row in events]))
            self.assertNotIn("Plan something", str([tuple(row) for row in events]))
            self.assertEqual(conn.execute("SELECT count(*) FROM task_plans").fetchone()[0], 1)

    def test_existing_opportunity_works_after_migration(self):
        self.assertEqual(self.opportunities.create(self.alice, "Original opportunity", operation_key="opp-create"), self.opp)
        self.assertEqual(self.opportunities.get(self.alice, self.opp)["status"], "draft")
        self.opportunities.archive(self.alice, self.opp, operation_key="opp-archive")
        self.assertEqual(self.opportunities.get(self.alice, self.opp)["status"], "archived")
        with StateStorage(self.directory / "v3.sqlite3").transaction() as conn:
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 3)
            self.assertEqual(conn.execute("SELECT status FROM opportunity_drafts WHERE id=?", (self.opp,)).fetchone()[0], "draft")

    def test_cross_account_denial_and_owner_scoped_keys(self):
        identifier = self.tasks.create(self.alice, "Mine", operation_key="same")
        other = self.tasks.create(self.bob, "Other", operation_key="same")
        self.assertNotEqual(identifier, other)
        for action in (lambda: self.tasks.get(self.bob, identifier),
                       lambda: self.tasks.cancel(self.bob, identifier, operation_key="attack"),
                       lambda: self.tasks.get(self.bob, "f" * 32)):
            with self.assertRaises(AccessDenied):
                action()
        self.assertEqual(self.tasks.get(self.alice, identifier)["status"], "planned")

    def test_operation_keys_conflict_across_types_and_inputs(self):
        with self.assertRaises(TaskError):
            self.tasks.create(self.alice, "Task", operation_key="opp-create")
        task = self.tasks.create(self.alice, "First", operation_key="task")
        with self.assertRaises(TaskError):
            self.tasks.create(self.alice, "Second", operation_key="task")
        with self.assertRaises(TaskError):
            self.tasks.cancel(self.alice, task, operation_key="task")
        with self.assertRaises(DraftError):
            self.opportunities.create(self.alice, "Other", operation_key="task")
        with self.state.transaction() as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM state_operations").fetchone()[0], 2)

    def test_invalid_inputs_and_illegal_transitions(self):
        for title in ("", "   ", "x"*201, "line\nline", "\ud800", None):
            with self.subTest(title=repr(title)):
                with self.assertRaises(TaskError):
                    self.tasks.create(self.alice, title, operation_key="key")
        for key in ("", " ", "x"*129, None):
            with self.assertRaises(TaskError):
                self.tasks.create(self.alice, "Test", operation_key=key)
        with self.assertRaises(TaskError):
            self.tasks.get(self.alice, "bad-id")
        identifier = self.tasks.create(self.alice, "Test", operation_key="create")
        self.tasks.cancel(self.alice, identifier, operation_key="cancel")
        with self.assertRaises(TaskError):
            self.tasks.cancel(self.alice, identifier, operation_key="cancel-again")
        with self.state.transaction() as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM state_audit WHERE resource_type='task_plan'").fetchone()[0], 2)

    def test_invalid_revoked_and_expired_sessions_denied(self):
        self.auth.logout(self.bob)
        for token in (self.bob, "x" * 43, ""):
            with self.assertRaises(AccessDenied):
                self.tasks.create(token, "Test", operation_key="key")
        self.clock.now += SESSION_SECONDS
        with self.assertRaises(AccessDenied):
            self.tasks.create(self.alice, "Test", operation_key="key")
        with self.state.transaction() as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM task_plans").fetchone()[0], 0)

    def test_atomic_rollback_for_audit_and_idempotency_failures(self):
        for helper in ("_audit", "_remember"):
            with self.subTest(helper=helper):
                with patch("ai_earn.task_plans." + helper, side_effect=RuntimeError("test failure")):
                    with self.assertRaisesRegex(RuntimeError, "test failure"):
                        self.tasks.create(self.alice, "Test", operation_key="key")
                with self.state.transaction() as conn:
                    self.assertEqual(conn.execute("SELECT count(*) FROM task_plans").fetchone()[0], 0)
                    self.assertEqual(conn.execute("SELECT count(*) FROM state_audit WHERE resource_type='task_plan'").fetchone()[0], 0)
                    self.assertEqual(conn.execute("SELECT count(*) FROM state_operations WHERE resource_type='task_plan'").fetchone()[0], 0)
        self.assertTrue(self.tasks.create(self.alice, "Test", operation_key="key"))

    def test_cancel_failure_rolls_back_transition(self):
        identifier = self.tasks.create(self.alice, "Test", operation_key="create")
        with patch("ai_earn.task_plans._audit", side_effect=RuntimeError("failure")):
            with self.assertRaises(RuntimeError):
                self.tasks.cancel(self.alice, identifier, operation_key="cancel")
        self.assertEqual(self.tasks.get(self.alice, identifier)["status"], "planned")
        self.assertEqual(self.tasks.cancel(self.alice, identifier, operation_key="cancel"), identifier)

    def test_expiry_before_commit_rolls_back_task_and_audit(self):
        from ai_earn.task_plans import _audit
        def expire(*args):
            _audit(*args)
            self.clock.now += SESSION_SECONDS
        with patch("ai_earn.task_plans._audit", side_effect=expire):
            with self.assertRaises(AccessDenied):
                self.tasks.create(self.alice, "Test", operation_key="key")
        with self.state.transaction() as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM task_plans").fetchone()[0], 0)
            self.assertEqual(conn.execute("SELECT count(*) FROM state_audit WHERE resource_type='task_plan'").fetchone()[0], 0)
            self.assertEqual(conn.execute("SELECT count(*) FROM state_operations WHERE resource_type='task_plan'").fetchone()[0], 0)

    def test_cancel_concurrent_retries_produce_one_audit(self):
        identifier = self.tasks.create(self.alice, "Test", operation_key="create")
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(self.tasks.cancel, self.alice, identifier, operation_key="cancel") for _ in range(2)]
            self.assertEqual([f.result(timeout=10) for f in futures], [identifier, identifier])
        with self.state.transaction() as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM state_audit WHERE event='task_cancelled'").fetchone()[0], 1)

    def test_concurrent_retries_and_ambiguous_auth_exit(self):
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(self.tasks.create, self.alice, "Test", operation_key="key") for _ in range(2)]
            ids = [f.result(timeout=10) for f in futures]
        self.assertEqual(ids[0], ids[1])
        original = self.auth.storage.transaction
        @contextmanager
        def failing_auth_exit():
            with original() as conn:
                yield conn
                raise RuntimeError("auth exit failure")
        with patch.object(self.auth.storage, "transaction", failing_auth_exit):
            with self.assertRaisesRegex(RuntimeError, "auth exit failure"):
                self.tasks.create(self.alice, "Other", operation_key="other")
        another = self.tasks.create(self.alice, "Other", operation_key="other")
        self.assertEqual(self.tasks.get(self.alice, another)["title"], "Other")
        with self.state.transaction() as conn:
            for table in ("task_plans",):
                self.assertEqual(conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0], 2)
            self.assertEqual(conn.execute("SELECT count(*) FROM state_audit WHERE resource_type='task_plan'").fetchone()[0], 2)

    def test_version_four_backup_restore_and_old_schema_refusal(self):
        identifier = self.tasks.create(self.alice, "Test", operation_key="create")
        copy = self.directory / "v4-copy.sqlite3"
        self.state.backup_to(copy, confirmed=True)
        self.assertEqual(TaskPlans(self.auth, StateStorage(copy)).get(self.alice, identifier)["title"], "Test")
        old = TaskPlans(self.auth, StateStorage(self.directory / "v3.sqlite3"))
        with self.assertRaises(StateStorageError):
            old.create(self.alice, "Test", operation_key="key")

    def test_migration_confirmation_and_failure_preserve_backup(self):
        prior = StateStorage(self.directory / "v3.sqlite3")
        backup = self.directory / "before-v4.sqlite3"
        with self.assertRaises(StateStorageError):
            prior.migrate_to_v4(backup)
        self.assertFalse(backup.exists())
        def fail(conn):
            conn.execute("CREATE TABLE task_plans (id TEXT)")
            raise RuntimeError("migration failure")
        with patch("ai_earn.state_storage._apply_v4", side_effect=fail):
            with self.assertRaisesRegex(RuntimeError, "migration failure"):
                prior.migrate_to_v4(backup, confirmed=True)
        for store in (prior, StateStorage(backup)):
            with store.transaction() as conn:
                self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 3)
                self.assertEqual(conn.execute("SELECT title FROM opportunity_drafts WHERE id=?", (self.opp,)).fetchone()[0], "Original opportunity")
                self.assertIsNone(conn.execute("SELECT name FROM sqlite_master WHERE name='task_plans'").fetchone())

    def test_crash_during_migration_leaves_restorable_v3_backup(self):
        source = self.directory / "v3.sqlite3"
        backup = self.directory / "crash-backup.sqlite3"
        code = '''
import os, sys
from pathlib import Path
import ai_earn.state_storage as storage

def crash(conn):
    conn.execute("CREATE TABLE task_plans (id TEXT)")
    os._exit(37)

storage._apply_v4 = crash
storage.StateStorage(Path(sys.argv[1])).migrate_to_v4(Path(sys.argv[2]), confirmed=True)
'''
        result = subprocess.run([sys.executable, "-c", code, str(source), str(backup)], timeout=20)
        self.assertEqual(result.returncode, 37)
        restored = self.directory / "crash-restored.sqlite3"
        StateStorage(backup).backup_to(restored, confirmed=True)
        restored_opportunities = OpportunityDrafts(self.auth, StateStorage(restored))
        self.assertEqual(restored_opportunities.get(self.alice, self.opp)["title"], "Original opportunity")
        self.assertEqual(restored_opportunities.create(self.alice, "Original opportunity", operation_key="opp-create"), self.opp)

    def test_migration_works_in_wal_mode(self):
        prior = StateStorage(self.directory / "v3.sqlite3")
        with closing(sqlite3.connect(prior.path)) as conn:
            self.assertEqual(conn.execute("PRAGMA journal_mode=WAL").fetchone()[0].lower(), "wal")
        backup = self.directory / "wal-v3.sqlite3"
        prior.migrate_to_v4(backup, confirmed=True)
        with prior.transaction() as conn:
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 4)
        with StateStorage(backup).transaction() as conn:
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 3)

    def test_tampered_v4_schema_is_refused(self):
        with closing(sqlite3.connect(self.state.path)) as conn, conn:
            conn.execute("DROP TABLE task_plans")
            conn.execute("""CREATE TABLE task_plans (
                id TEXT PRIMARY KEY NOT NULL, owner_id TEXT NOT NULL, title TEXT NOT NULL,
                status TEXT NOT NULL, created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL
            )""")
        with self.assertRaises(StateStorageError):
            StateStorage(self.state.path)
        with StateStorage(self.directory / "v3.sqlite3").transaction() as conn:
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 3)

    def test_migration_refuses_existing_backup_without_modification(self):
        prior = StateStorage(self.directory / "v3.sqlite3")
        backup = self.directory / "existing.sqlite3"
        backup.write_bytes(b"preserve")
        with self.assertRaises(StateStorageError):
            prior.migrate_to_v4(backup, confirmed=True)
        self.assertEqual(backup.read_bytes(), b"preserve")
        with prior.transaction() as conn:
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 3)

    def test_schema_rejects_completion_or_execution_states(self):
        owner = self.auth.profile(self.alice)["id"]
        for status in ("running", "completed", "failed"):
            with self.subTest(status=status):
                with self.assertRaises(StateStorageError):
                    with self.state.transaction() as conn:
                        conn.execute("INSERT INTO task_plans VALUES (?, ?, ?, ?, ?, ?)", ("a"*32, owner, "Test", status, self.clock.now, self.clock.now))

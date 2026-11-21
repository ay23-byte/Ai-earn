"""Disposable order estimates only; no customer, contract, invoice, or payment."""
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
from ai_earn.order_drafts import OrderDrafts, OrderError
from ai_earn.state_storage import StateStorage, StateStorageError
from ai_earn.task_plans import TaskError, TaskPlans


class Clock:
    now = 1_800_000_000
    def __call__(self):
        return self.now


@unittest.skipUnless(os.name in {"posix", "nt"}, "requires a supported private filesystem")
class OrderDraftTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
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
        self.state.migrate_to_v4(self.directory / "v3.sqlite3", confirmed=True)
        self.tasks = TaskPlans(self.auth, self.state)
        self.opportunities = OpportunityDrafts(self.auth, self.state)
        self.task = self.tasks.create(self.alice, "Existing plan", operation_key="task-key")
        self.opp = self.opportunities.create(self.alice, "Existing draft", operation_key="opp-key")
        self.state.migrate_to_v5(self.directory / "v4.sqlite3", confirmed=True)
        self.orders = OrderDrafts(self.auth, self.state)

    def test_create_cancel_and_idempotent_retries(self):
        ident = self.orders.create(self.alice, "Sample scope", 12550, operation_key="order")
        self.assertEqual(self.orders.create(self.alice, "Sample scope", 12550, operation_key="order"), ident)
        order = self.orders.get(self.alice, ident)
        self.assertEqual((order["status"], order["currency"], order["proposed_amount_paise"]), ("draft", "INR", 12550))
        self.assertEqual(self.orders.cancel(self.alice, ident, operation_key="cancel"), ident)
        self.assertEqual(self.orders.cancel(self.alice, ident, operation_key="cancel"), ident)
        self.assertEqual(self.orders.get(self.alice, ident)["status"], "cancelled")
        with self.state.transaction() as conn:
            rows = conn.execute("SELECT event,actor_id,resource_id FROM state_audit WHERE resource_type='order_draft' ORDER BY id").fetchall()
            self.assertEqual([row[0] for row in rows], ["order_draft_created", "order_draft_cancelled"])
            self.assertTrue(all(row[2] == ident for row in rows))
            self.assertNotIn(self.alice, str([tuple(row) for row in rows]))
            self.assertNotIn("Sample scope", str([tuple(row) for row in rows]))
            self.assertEqual(conn.execute("SELECT count(*) FROM order_drafts").fetchone()[0], 1)

    def test_list_is_paginated_filtered_and_owner_scoped(self):
        first = self.orders.create(self.alice, "First scope", 100, operation_key="list-first")
        second = self.orders.create(self.alice, "Second scope", 200, operation_key="list-second")
        other = self.orders.create(self.bob, "Other owner scope", 300, operation_key="list-other")
        self.orders.cancel(self.alice, second, operation_key="list-cancel")

        rows = self.orders.list(self.alice, limit=1)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["id"], min(first, second))
        next_rows = self.orders.list(self.alice, after_id=rows[0]["id"], limit=10)
        self.assertEqual([row["id"] for row in next_rows], [max(first, second)])
        self.assertEqual([row["id"] for row in self.orders.list(self.bob)], [other])
        self.assertTrue(all(row["id"] != other for row in rows + next_rows))
        self.assertEqual([row["id"] for row in self.orders.list(self.alice, status="cancelled")], [second])
        self.assertEqual([row["id"] for row in self.orders.list(self.alice, status="draft")], [first])
        for kwargs in ({"limit": 0}, {"limit": 101}, {"limit": True}, {"status": "paid"}, {"after_id": "bad"}):
            with self.subTest(kwargs=kwargs), self.assertRaises(OrderError):
                self.orders.list(self.alice, **kwargs)

    def test_existing_task_and_opportunity_remain_usable_and_retries_work(self):
        self.assertEqual(self.tasks.create(self.alice, "Existing plan", operation_key="task-key"), self.task)
        self.assertEqual(self.opportunities.create(self.alice, "Existing draft", operation_key="opp-key"), self.opp)
        self.tasks.cancel(self.alice, self.task, operation_key="cancel-task")
        self.opportunities.archive(self.alice, self.opp, operation_key="archive-opp")
        with StateStorage(self.directory / "v4.sqlite3").transaction() as conn:
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 4)
            self.assertEqual(conn.execute("SELECT status FROM task_plans WHERE id=?", (self.task,)).fetchone()[0], "planned")
            self.assertEqual(conn.execute("SELECT status FROM opportunity_drafts WHERE id=?", (self.opp,)).fetchone()[0], "draft")

    def test_proposed_amount_strict_integer_bounds(self):
        for amount in (-1, 1_000_000_000_001, True, 1.0, "100", None):
            with self.subTest(amount=amount):
                with self.assertRaises(OrderError):
                    self.orders.create(self.alice, "Scope", amount, operation_key="key")
        for amount in (0, 1_000_000_000_000):
            ident = self.orders.create(self.alice, "Scope", amount, operation_key=f"key-{amount}")
            self.assertEqual(self.orders.get(self.alice, ident)["proposed_amount_paise"], amount)

    def test_cross_owner_denied_and_keys_are_scoped(self):
        order = self.orders.create(self.alice, "Scope", 100, operation_key="same")
        self.assertNotEqual(order, self.orders.create(self.bob, "Scope", 100, operation_key="same"))
        for action in (lambda: self.orders.get(self.bob, order),
                       lambda: self.orders.cancel(self.bob, order, operation_key="attack"),
                       lambda: self.orders.get(self.bob, "f"*32)):
            with self.assertRaises(AccessDenied):
                action()
        self.assertEqual(self.orders.get(self.alice, order)["status"], "draft")

    def test_key_conflicts_include_scope_amount_and_other_record_types(self):
        ident = self.orders.create(self.alice, "Scope", 100, operation_key="same")
        for action in (lambda: self.orders.create(self.alice, "Other", 100, operation_key="same"),
                       lambda: self.orders.create(self.alice, "Scope", 101, operation_key="same"),
                       lambda: self.orders.cancel(self.alice, ident, operation_key="same"),
                       lambda: self.orders.create(self.alice, "Scope", 100, operation_key="task-key")):
            with self.assertRaises(OrderError):
                action()
        with self.assertRaises(TaskError):
            self.tasks.create(self.alice, "Task", operation_key="same")
        with self.assertRaises(DraftError):
            self.opportunities.create(self.alice, "Opportunity", operation_key="same")
        with self.state.transaction() as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM order_drafts").fetchone()[0], 1)
            self.assertEqual(conn.execute("SELECT count(*) FROM state_audit WHERE resource_type='order_draft'").fetchone()[0], 1)

    def test_validation_and_illegal_transitions(self):
        for title in ("", " ", "x"*201, "hello\nworld", "\ud800", None):
            with self.assertRaises(OrderError):
                self.orders.create(self.alice, title, 100, operation_key="key")
        for key in ("", " ", "x"*129, None):
            with self.assertRaises(OrderError):
                self.orders.create(self.alice, "Scope", 100, operation_key=key)
        with self.assertRaises(OrderError):
            self.orders.get(self.alice, "bad")
        order = self.orders.create(self.alice, "Scope", 100, operation_key="created")
        self.orders.cancel(self.alice, order, operation_key="cancelled")
        with self.assertRaises(OrderError):
            self.orders.cancel(self.alice, order, operation_key="again")

    def test_invalid_revoked_and_expired_sessions_denied(self):
        self.auth.logout(self.bob)
        for token in (self.bob, "x"*43, ""):
            with self.assertRaises(AccessDenied):
                self.orders.create(token, "Scope", 100, operation_key="key")
        self.clock.now += SESSION_SECONDS
        with self.assertRaises(AccessDenied):
            self.orders.create(self.alice, "Scope", 100, operation_key="key")
        with self.state.transaction() as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM order_drafts").fetchone()[0], 0)

    def test_atomic_rollback_and_expiry_before_commit(self):
        for helper in ("_audit", "_remember"):
            with patch("ai_earn.order_drafts." + helper, side_effect=RuntimeError("injected failure")):
                with self.assertRaisesRegex(RuntimeError, "injected failure"):
                    self.orders.create(self.alice, "Scope", 100, operation_key="key")
        from ai_earn.order_drafts import _audit
        def expire(*args):
            _audit(*args)
            self.clock.now += SESSION_SECONDS
        with patch("ai_earn.order_drafts._audit", side_effect=expire):
            with self.assertRaises(AccessDenied):
                self.orders.create(self.alice, "Scope", 100, operation_key="key")
        with self.state.transaction() as conn:
            for table in ("order_drafts",):
                self.assertEqual(conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0], 0)
            self.assertEqual(conn.execute("SELECT count(*) FROM state_operations WHERE resource_type='order_draft'").fetchone()[0], 0)
            self.assertEqual(conn.execute("SELECT count(*) FROM state_audit WHERE resource_type='order_draft'").fetchone()[0], 0)

    def test_cancel_rollback(self):
        ident = self.orders.create(self.alice, "Scope", 100, operation_key="create")
        with patch("ai_earn.order_drafts._audit", side_effect=RuntimeError("failure")):
            with self.assertRaises(RuntimeError):
                self.orders.cancel(self.alice, ident, operation_key="cancel")
        self.assertEqual(self.orders.get(self.alice, ident)["status"], "draft")
        self.assertEqual(self.orders.cancel(self.alice, ident, operation_key="cancel"), ident)

    def test_concurrent_and_ambiguous_commit_retries(self):
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(self.orders.create, self.alice, "Scope", 100, operation_key="key") for _ in range(2)]
            ids = [f.result(timeout=10) for f in futures]
        self.assertEqual(ids[0], ids[1])
        original = self.auth.storage.transaction
        @contextmanager
        def fail_on_exit():
            with original() as conn:
                yield conn
                raise RuntimeError("auth exit failure")
        with patch.object(self.auth.storage, "transaction", fail_on_exit):
            with self.assertRaisesRegex(RuntimeError, "auth exit failure"):
                self.orders.create(self.alice, "Other", 500, operation_key="other")
        ident = self.orders.create(self.alice, "Other", 500, operation_key="other")
        self.assertEqual(self.orders.get(self.alice, ident)["proposed_amount_paise"], 500)
        with self.state.transaction() as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM order_drafts").fetchone()[0], 2)
            self.assertEqual(conn.execute("SELECT count(*) FROM state_audit WHERE resource_type='order_draft'").fetchone()[0], 2)

    def test_schema_rejects_bad_amount_currency_and_financial_states(self):
        owner = self.auth.profile(self.alice)["id"]
        for amount, currency, status in ((-1, "INR", "draft"), (100, "USD", "draft"), (100, "INR", "paid"), (100.5, "INR", "draft")):
            with self.subTest(amount=amount, currency=currency, status=status):
                with self.assertRaises(StateStorageError):
                    with self.state.transaction() as conn:
                        conn.execute("INSERT INTO order_drafts VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                                     ("a"*32, owner, "Scope", amount, currency, status, self.clock.now, self.clock.now))

    def test_concurrent_cancel_retries_have_single_audit(self):
        ident = self.orders.create(self.alice, "Scope", 100, operation_key="create")
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(self.orders.cancel, self.alice, ident, operation_key="cancel") for _ in range(2)]
            self.assertEqual([f.result(timeout=10) for f in futures], [ident, ident])
        with self.state.transaction() as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM state_audit WHERE event='order_draft_cancelled'").fetchone()[0], 1)

    def test_migration_backup_integrity_failure_prevents_upgrade(self):
        prior = StateStorage(self.directory / "v4.sqlite3")
        backup = self.directory / "partial.sqlite3"
        with patch("ai_earn.state_storage._check_integrity", side_effect=[None, StateStorageError("bad copy")]):
            with self.assertRaises(StateStorageError):
                prior.migrate_to_v5(backup, confirmed=True)
        self.assertTrue(backup.exists())
        with prior.transaction() as conn:
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 4)

    def test_migration_works_with_wal_mode(self):
        prior = StateStorage(self.directory / "v4.sqlite3")
        backup = self.directory / "wal-v4.sqlite3"
        with closing(sqlite3.connect(prior.path)) as wal_conn:
            self.assertEqual(wal_conn.execute("PRAGMA journal_mode=WAL").fetchone()[0].lower(), "wal")
            wal_conn.execute("UPDATE task_plans SET title=? WHERE id=?", ("WAL plan", self.task))
            wal_conn.commit()
            self.assertTrue(Path(str(prior.path) + "-wal").exists())
            prior.migrate_to_v5(backup, confirmed=True)
            with prior.transaction() as conn:
                self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 5)
                self.assertEqual(conn.execute("SELECT title FROM task_plans WHERE id=?", (self.task,)).fetchone()[0], "WAL plan")
            with StateStorage(backup).transaction() as conn:
                self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 4)
                self.assertEqual(conn.execute("SELECT title FROM task_plans WHERE id=?", (self.task,)).fetchone()[0], "WAL plan")

    def test_migration_failure_and_non_overwrite_preserve_v4(self):
        prior = StateStorage(self.directory / "v4.sqlite3")
        backup = self.directory / "backup-v4.sqlite3"
        for confirmation in (False, 1):
            with self.assertRaises(StateStorageError):
                prior.migrate_to_v5(backup, confirmed=confirmation)
            self.assertFalse(backup.exists())
        backup.write_bytes(b"preserve")
        with self.assertRaises(StateStorageError):
            prior.migrate_to_v5(backup, confirmed=True)
        self.assertEqual(backup.read_bytes(), b"preserve")
        failed_backup = self.directory / "failure-v4.sqlite3"
        def fail(conn):
            conn.execute("CREATE TABLE order_drafts (id TEXT)")
            raise RuntimeError("migration failure")
        with patch("ai_earn.state_storage._apply_v5", side_effect=fail):
            with self.assertRaisesRegex(RuntimeError, "migration failure"):
                prior.migrate_to_v5(failed_backup, confirmed=True)
        for store in (prior, StateStorage(failed_backup)):
            with store.transaction() as conn:
                self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 4)
                self.assertEqual(conn.execute("SELECT status FROM task_plans WHERE id=?", (self.task,)).fetchone()[0], "planned")
                self.assertIsNone(conn.execute("SELECT name FROM sqlite_master WHERE name='order_drafts'").fetchone())

    def test_backup_restore_and_prior_schema_refusal(self):
        ident = self.orders.create(self.alice, "Scope", 100, operation_key="create")
        copy = self.directory / "v5-copy.sqlite3"
        self.state.backup_to(copy, confirmed=True)
        self.assertEqual(OrderDrafts(self.auth, StateStorage(copy)).get(self.alice, ident)["proposed_amount_paise"], 100)
        prior = OrderDrafts(self.auth, StateStorage(self.directory / "v4.sqlite3"))
        with self.assertRaises(StateStorageError):
            prior.create(self.alice, "Scope", 100, operation_key="key")

    def test_v5_schema_tamper_is_refused(self):
        with closing(sqlite3.connect(self.state.path)) as conn, conn:
            conn.execute("DROP TABLE order_drafts")
            conn.execute("""CREATE TABLE order_drafts (
                id TEXT PRIMARY KEY NOT NULL, owner_id TEXT NOT NULL, title TEXT NOT NULL,
                proposed_amount_paise INTEGER NOT NULL, currency TEXT NOT NULL, status TEXT NOT NULL,
                created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL
            )""")
        with self.assertRaises(StateStorageError):
            StateStorage(self.state.path)
        with StateStorage(self.directory / "v4.sqlite3").transaction() as conn:
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 4)

    def test_crash_during_migration_leaves_restorable_v4_backup(self):
        source = self.directory / "v4.sqlite3"
        backup = self.directory / "crash-backup.sqlite3"
        code = '''
import os, sys
from pathlib import Path
import ai_earn.state_storage as storage

def crash(conn):
    conn.execute("CREATE TABLE order_drafts (id TEXT)")
    os._exit(37)

storage._apply_v5 = crash
storage.StateStorage(Path(sys.argv[1])).migrate_to_v5(Path(sys.argv[2]), confirmed=True)
'''
        result = subprocess.run([sys.executable, "-c", code, str(source), str(backup)], timeout=20)
        self.assertEqual(result.returncode, 37)
        restored = self.directory / "crash-restored.sqlite3"
        StateStorage(backup).backup_to(restored, confirmed=True)
        tasks = TaskPlans(self.auth, StateStorage(restored))
        self.assertEqual(tasks.create(self.alice, "Existing plan", operation_key="task-key"), self.task)

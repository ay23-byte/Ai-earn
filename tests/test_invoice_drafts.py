"""Disposable invoice draft snapshots; no actual invoices or payments."""
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
from ai_earn.invoice_drafts import InvoiceDrafts, InvoiceDraftError
from ai_earn.opportunity_drafts import OpportunityDrafts
from ai_earn.order_drafts import OrderDrafts
from ai_earn.state_storage import StateStorage, StateStorageError
from ai_earn.task_plans import TaskPlans


class Clock:
    now = 1_800_000_000

    def __call__(self):
        return self.now


@unittest.skipUnless(os.name in {"posix", "nt"}, "requires a supported private filesystem")
class InvoiceDraftTests(unittest.TestCase):
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
        for version in range(2, 6):
            getattr(self.state, f"migrate_to_v{version}")(self.directory / f"v{version-1}.sqlite3", confirmed=True)
        self.orders = OrderDrafts(self.auth, self.state)
        self.order = self.orders.create(self.alice, "Disposable scope", 12550, operation_key="order")
        self.state.migrate_to_v6(self.directory / "v5.sqlite3", confirmed=True)
        self.invoices = InvoiceDrafts(self.auth, self.state)

    def test_create_snapshot_cancel_and_retry(self):
        ident = self.invoices.create(self.alice, self.order, operation_key="create")
        self.assertEqual(self.invoices.create(self.alice, self.order, operation_key="create"), ident)
        row = self.invoices.get(self.alice, ident)
        self.assertEqual((row["order_id"], row["proposed_amount_paise"], row["currency"], row["status"]),
                         (self.order, 12550, "INR", "draft"))
        self.assertEqual(self.invoices.cancel(self.alice, ident, operation_key="cancel"), ident)
        self.assertEqual(self.invoices.cancel(self.alice, ident, operation_key="cancel"), ident)
        with self.assertRaises(InvoiceDraftError):
            self.invoices.cancel(self.alice, ident, operation_key="other")
        with self.state.transaction() as conn:
            events = conn.execute("SELECT event FROM state_audit WHERE resource_type='invoice_draft' ORDER BY id").fetchall()
            self.assertEqual([r[0] for r in events], ["invoice_draft_created", "invoice_draft_cancelled"])
            self.assertEqual(conn.execute("SELECT count(*) FROM invoice_drafts").fetchone()[0], 1)

    def test_owner_isolation_includes_parent_and_indistinguishable_missing(self):
        ident = self.invoices.create(self.alice, self.order, operation_key="create")
        for fn in (lambda: self.invoices.get(self.bob, ident),
                   lambda: self.invoices.get(self.bob, "f" * 32),
                   lambda: self.invoices.cancel(self.bob, ident, operation_key="cancel"),
                   lambda: self.invoices.create(self.bob, self.order, operation_key="create"),
                   lambda: self.invoices.create(self.bob, "f" * 32, operation_key="create")):
            with self.assertRaises(AccessDenied):
                fn()
        bob_order = self.orders.create(self.bob, "Other", 0, operation_key="order")
        self.assertNotEqual(ident, self.invoices.create(self.bob, bob_order, operation_key="create"))

    def test_parent_cancellation_blocks_new_draft_but_not_retry(self):
        ident = self.invoices.create(self.alice, self.order, operation_key="create")
        self.orders.cancel(self.alice, self.order, operation_key="cancel-order")
        self.assertEqual(self.invoices.create(self.alice, self.order, operation_key="create"), ident)
        self.assertEqual(self.invoices.get(self.alice, ident)["status"], "draft")
        with self.assertRaises(InvoiceDraftError):
            self.invoices.create(self.alice, self.order, operation_key="new")
        self.invoices.cancel(self.alice, ident, operation_key="cancel-invoice")

    def test_concurrent_parent_cancellation_serializes_creation(self):
        with ThreadPoolExecutor(max_workers=2) as pool:
            create = pool.submit(self.invoices.create, self.alice, self.order, operation_key="create")
            cancel = pool.submit(self.orders.cancel, self.alice, self.order, operation_key="cancel-order")
            try:
                ident = create.result(timeout=10)
            except InvoiceDraftError:
                ident = None
            self.assertEqual(cancel.result(timeout=10), self.order)
        self.assertEqual(self.orders.get(self.alice, self.order)["status"], "cancelled")
        with self.state.transaction() as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM invoice_drafts").fetchone()[0], int(ident is not None))
        if ident is not None:
            self.assertEqual(self.invoices.create(self.alice, self.order, operation_key="create"), ident)

    def test_invalid_inputs_and_sessions(self):
        for order_id, key in (("bad", "key"), (self.order, ""), (self.order, "x" * 129), (None, "key")):
            with self.assertRaises(InvoiceDraftError):
                self.invoices.create(self.alice, order_id, operation_key=key)
        self.auth.logout(self.bob)
        for token in (self.bob, "x" * 43, ""):
            with self.assertRaises(AccessDenied):
                self.invoices.create(token, self.order, operation_key="key")
        self.clock.now += SESSION_SECONDS
        with self.assertRaises(AccessDenied):
            self.invoices.create(self.alice, self.order, operation_key="key")

    def test_cross_resource_and_request_key_conflicts(self):
        ident = self.invoices.create(self.alice, self.order, operation_key="create")
        other = self.orders.create(self.alice, "Another", 500, operation_key="other-order")
        for fn in (lambda: self.invoices.create(self.alice, other, operation_key="create"),
                   lambda: self.invoices.cancel(self.alice, ident, operation_key="create"),
                   lambda: self.invoices.create(self.alice, self.order, operation_key="order")):
            with self.assertRaises(InvoiceDraftError):
                fn()
        from ai_earn.order_drafts import OrderError
        with self.assertRaises(OrderError):
            self.orders.create(self.alice, "Another", 1, operation_key="create")

    def test_concurrent_create_and_cancel_retries(self):
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(self.invoices.create, self.alice, self.order, operation_key="create") for _ in range(2)]
            ids = [f.result(timeout=10) for f in futures]
        self.assertEqual(ids[0], ids[1])
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(self.invoices.cancel, self.alice, ids[0], operation_key="cancel") for _ in range(2)]
            self.assertEqual([f.result(timeout=10) for f in futures], ids)
        with self.state.transaction() as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM invoice_drafts").fetchone()[0], 1)
            self.assertEqual(conn.execute("SELECT count(*) FROM state_audit WHERE resource_type='invoice_draft'").fetchone()[0], 2)

    def test_atomic_failure_and_expiry_before_commit(self):
        for helper in ("_audit", "_remember"):
            with patch("ai_earn.invoice_drafts." + helper, side_effect=RuntimeError("injected")):
                with self.assertRaisesRegex(RuntimeError, "injected"):
                    self.invoices.create(self.alice, self.order, operation_key="key")
        from ai_earn.invoice_drafts import _audit
        def expire(*args):
            _audit(*args)
            self.clock.now += SESSION_SECONDS
        with patch("ai_earn.invoice_drafts._audit", side_effect=expire):
            with self.assertRaises(AccessDenied):
                self.invoices.create(self.alice, self.order, operation_key="key")
        with self.state.transaction() as conn:
            for table in ("invoice_drafts",):
                self.assertEqual(conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0], 0)
            for table in ("state_audit", "state_operations"):
                self.assertEqual(conn.execute(f"SELECT count(*) FROM {table} WHERE resource_type='invoice_draft'").fetchone()[0], 0)

    def test_cancel_failure_rolls_back(self):
        ident = self.invoices.create(self.alice, self.order, operation_key="create")
        with patch("ai_earn.invoice_drafts._remember", side_effect=RuntimeError("injected")):
            with self.assertRaises(RuntimeError):
                self.invoices.cancel(self.alice, ident, operation_key="cancel")
        self.assertEqual(self.invoices.get(self.alice, ident)["status"], "draft")
        self.assertEqual(self.invoices.cancel(self.alice, ident, operation_key="cancel"), ident)

    def test_ambiguous_auth_exit_retry_is_safe(self):
        original = self.auth.storage.transaction
        @contextmanager
        def fail_on_exit():
            with original() as conn:
                yield conn
                raise RuntimeError("auth exit failure")
        with patch.object(self.auth.storage, "transaction", fail_on_exit):
            with self.assertRaises(RuntimeError):
                self.invoices.create(self.alice, self.order, operation_key="key")
        ident = self.invoices.create(self.alice, self.order, operation_key="key")
        self.assertEqual(self.invoices.get(self.alice, ident)["order_id"], self.order)
        with self.state.transaction() as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM invoice_drafts").fetchone()[0], 1)

    def test_schema_constraints_and_foreign_key(self):
        owner = self.auth.profile(self.alice)["id"]
        for parent, amount, currency, status in (("f" * 32, 1, "INR", "draft"),
                                                  (self.order, -1, "INR", "draft"),
                                                  (self.order, 1.5, "INR", "draft"),
                                                  (self.order, 1, "USD", "draft"),
                                                  (self.order, 1, "INR", "issued")):
            with self.assertRaises(StateStorageError):
                with self.state.transaction() as conn:
                    conn.execute("INSERT INTO invoice_drafts VALUES (?,?,?,?,?,?,?,?)",
                                 ("a" * 32, owner, parent, amount, currency, status, self.clock.now, self.clock.now))

    def test_foreign_key_rejects_different_owner_even_with_existing_order(self):
        bob_owner = self.auth.profile(self.bob)["id"]
        with self.assertRaises(StateStorageError):
            with self.state.transaction() as conn:
                conn.execute("INSERT INTO invoice_drafts VALUES (?,?,?,?,?,?,?,?)",
                             ("a" * 32, bob_owner, self.order, 12550, "INR", "draft", self.clock.now, self.clock.now))
        with self.state.transaction() as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM invoice_drafts").fetchone()[0], 0)

    def test_backup_refuses_foreign_key_inconsistent_source(self):
        ident = self.invoices.create(self.alice, self.order, operation_key="create")
        with closing(sqlite3.connect(self.state.path)) as conn, conn:
            # Simulate a faulty external writer that disabled foreign keys.
            conn.execute("PRAGMA foreign_keys=OFF")
            conn.execute("UPDATE invoice_drafts SET order_id=? WHERE id=?", ("f" * 32, ident))
            self.assertEqual(conn.execute("PRAGMA quick_check").fetchone()[0], "ok")
        destination = self.directory / "invalid-copy.sqlite3"
        with self.assertRaises(StateStorageError):
            self.state.backup_to(destination, confirmed=True)
        self.assertFalse(destination.exists())

    def test_backup_refuses_foreign_key_inconsistent_copy(self):
        ident = self.invoices.create(self.alice, self.order, operation_key="create")
        destination = self.directory / "bad-copy.sqlite3"
        from ai_earn.state_storage import _copy_database
        def corrupt_copy(source, target):
            _copy_database(source, target)
            target.execute("PRAGMA foreign_keys=OFF")
            target.execute("UPDATE invoice_drafts SET order_id=? WHERE id=?", ("f" * 32, ident))
            target.commit()
        with patch("ai_earn.state_storage._copy_database", side_effect=corrupt_copy):
            with self.assertRaises(StateStorageError):
                self.state.backup_to(destination, confirmed=True)
        self.assertTrue(destination.exists())
        self.assertEqual(self.invoices.get(self.alice, ident)["order_id"], self.order)

    def test_unique_parent_index_tamper_is_refused(self):
        with closing(sqlite3.connect(self.state.path)) as conn, conn:
            conn.execute("DROP INDEX order_drafts_id_owner_idx")
            conn.execute("CREATE INDEX order_drafts_id_owner_idx ON order_drafts(id, owner_id)")
        with self.assertRaises(StateStorageError):
            StateStorage(self.state.path)

    def test_earlier_services_and_backup_remain_usable(self):
        self.assertEqual(self.orders.create(self.alice, "Disposable scope", 12550, operation_key="order"), self.order)
        tasks = TaskPlans(self.auth, self.state)
        opportunities = OpportunityDrafts(self.auth, self.state)
        task = tasks.create(self.alice, "Plan", operation_key="task")
        opp = opportunities.create(self.alice, "Opportunity", operation_key="opp")
        self.assertEqual(tasks.get(self.alice, task)["status"], "planned")
        self.assertEqual(opportunities.get(self.alice, opp)["status"], "draft")
        prior = StateStorage(self.directory / "v5.sqlite3")
        self.assertEqual(OrderDrafts(self.auth, prior).get(self.alice, self.order)["status"], "draft")
        with self.assertRaises(StateStorageError):
            InvoiceDrafts(self.auth, prior).create(self.alice, self.order, operation_key="invoice")
        ident = self.invoices.create(self.alice, self.order, operation_key="invoice")
        copy = self.directory / "v6-copy.sqlite3"
        self.state.backup_to(copy, confirmed=True)
        self.assertEqual(InvoiceDrafts(self.auth, StateStorage(copy)).get(self.alice, ident)["order_id"], self.order)

    def test_migration_refusal_failure_and_integrity(self):
        prior = StateStorage(self.directory / "v5.sqlite3")
        backup = self.directory / "new.sqlite3"
        for flag in (False, 1):
            with self.assertRaises(StateStorageError):
                prior.migrate_to_v6(backup, confirmed=flag)
        self.assertFalse(backup.exists())
        backup.write_bytes(b"preserve")
        with self.assertRaises(StateStorageError):
            prior.migrate_to_v6(backup, confirmed=True)
        self.assertEqual(backup.read_bytes(), b"preserve")
        failed = self.directory / "failed.sqlite3"
        def fail(conn):
            conn.execute("CREATE TABLE invoice_drafts (id TEXT)")
            raise RuntimeError("injected")
        with patch("ai_earn.state_storage._apply_v6", side_effect=fail):
            with self.assertRaises(RuntimeError):
                prior.migrate_to_v6(failed, confirmed=True)
        for store in (prior, StateStorage(failed)):
            with store.transaction() as conn:
                self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 5)
                self.assertIsNone(conn.execute("SELECT name FROM sqlite_master WHERE name='invoice_drafts'").fetchone())
        bad = self.directory / "bad.sqlite3"
        with patch("ai_earn.state_storage._check_integrity", side_effect=[None, StateStorageError("bad copy")]):
            with self.assertRaises(StateStorageError):
                prior.migrate_to_v6(bad, confirmed=True)
        self.assertTrue(bad.exists())
        with prior.transaction() as conn:
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 5)

    def test_wal_backup_contains_committed_frames(self):
        prior = StateStorage(self.directory / "v5.sqlite3")
        backup = self.directory / "wal.sqlite3"
        with closing(sqlite3.connect(prior.path)) as conn:
            self.assertEqual(conn.execute("PRAGMA journal_mode=WAL").fetchone()[0].lower(), "wal")
            conn.execute("UPDATE order_drafts SET title=? WHERE id=?", ("WAL scope", self.order))
            conn.commit()
            self.assertTrue(Path(str(prior.path) + "-wal").exists())
            prior.migrate_to_v6(backup, confirmed=True)
            with StateStorage(backup).transaction() as snapshot:
                self.assertEqual(snapshot.execute("SELECT title FROM order_drafts WHERE id=?", (self.order,)).fetchone()[0], "WAL scope")
                self.assertEqual(snapshot.execute("PRAGMA user_version").fetchone()[0], 5)

    def test_tamper_is_refused(self):
        with closing(sqlite3.connect(self.state.path)) as conn, conn:
            conn.execute("DROP TABLE invoice_drafts")
            conn.execute("""CREATE TABLE invoice_drafts (
                id TEXT PRIMARY KEY NOT NULL, owner_id TEXT NOT NULL, order_id TEXT NOT NULL,
                proposed_amount_paise INTEGER NOT NULL, currency TEXT NOT NULL, status TEXT NOT NULL,
                created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL
            )""")
        with self.assertRaises(StateStorageError):
            StateStorage(self.state.path)

    def test_crash_leaves_restorable_v5_backup(self):
        source = self.directory / "v5.sqlite3"
        backup = self.directory / "crash.sqlite3"
        code = '''
import os, sys
from pathlib import Path
import ai_earn.state_storage as storage
def crash(conn):
    conn.execute("CREATE TABLE invoice_drafts (id TEXT)")
    os._exit(37)
storage._apply_v6 = crash
storage.StateStorage(Path(sys.argv[1])).migrate_to_v6(Path(sys.argv[2]), confirmed=True)
'''
        result = subprocess.run([sys.executable, "-c", code, str(source), str(backup)], timeout=20)
        self.assertEqual(result.returncode, 37)
        restored = self.directory / "restored.sqlite3"
        StateStorage(backup).backup_to(restored, confirmed=True)
        orders = OrderDrafts(self.auth, StateStorage(restored))
        self.assertEqual(orders.create(self.alice, "Disposable scope", 12550, operation_key="order"), self.order)

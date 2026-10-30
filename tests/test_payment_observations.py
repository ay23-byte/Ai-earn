"""Disposable local placeholders only; no payment or provider verification."""
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
from ai_earn.opportunity_drafts import OpportunityDrafts
from ai_earn.order_drafts import OrderDrafts
from ai_earn.state_audit import StateAudit
from ai_earn.task_plans import TaskPlans
from ai_earn.payment_observations import ObservationError, PaymentObservations
from ai_earn.state_storage import StateStorage, StateStorageError
from ai_earn.task_queue import TaskQueue
from ai_earn.task_workflows import TaskWorkflows


class Clock:
    now = 1_800_000_000
    def __call__(self):
        return self.now


@unittest.skipUnless(os.name in {"posix", "nt"}, "requires a supported private filesystem")
class PaymentObservationTests(unittest.TestCase):
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
        for version in range(2, 9):
            getattr(self.state, f"migrate_to_v{version}")(self.directory / f"v{version-1}.sqlite3", confirmed=True)
        self.orders = OrderDrafts(self.auth, self.state)
        self.invoices = InvoiceDrafts(self.auth, self.state)
        self.order = self.orders.create(self.alice, "Synthetic estimate", 12550, operation_key="order")
        self.invoice = self.invoices.create(self.alice, self.order, operation_key="invoice")
        self.queue = TaskQueue(self.auth, self.state)
        self.task = self.queue.enqueue(self.alice, "Synthetic task", operation_key="queue")
        self.state.migrate_to_v9(self.directory / "v8.sqlite3", confirmed=True)
        self.observations = PaymentObservations(self.auth, self.state)

    def create(self, key="create"):
        return self.observations.create(self.alice, self.invoice, operation_key=key)

    def test_unverified_state_machine_and_exact_retries(self):
        ident = self.create()
        self.assertEqual(self.create(), ident)
        row = self.observations.get(self.alice, ident)
        self.assertEqual((row["invoice_id"], row["proposed_amount_paise"], row["currency"], row["status"]),
                         (self.invoice, 12550, "INR", "unverified"))
        self.observations.flag(self.alice, ident, operation_key="flag")
        self.assertEqual(self.observations.flag(self.alice, ident, operation_key="flag"), ident)
        self.observations.unflag(self.alice, ident, operation_key="unflag")
        self.observations.discard(self.alice, ident, operation_key="discard")
        self.assertEqual(self.observations.get(self.alice, ident)["status"], "discarded")
        for fn in (lambda: self.observations.flag(self.alice, ident, operation_key="new"),
                   lambda: self.observations.discard(self.alice, ident, operation_key="new")):
            with self.assertRaises(ObservationError):
                fn()
        with self.state.transaction() as conn:
            rows = conn.execute("SELECT event FROM state_audit WHERE resource_type='payment_observation' ORDER BY id").fetchall()
            self.assertEqual([r[0] for r in rows], ["payment_observation_created", "payment_observation_flag", "payment_observation_unflag", "payment_observation_discard"])
            self.assertEqual(conn.execute("SELECT count(*) FROM payment_observations").fetchone()[0], 1)

    def test_cross_owner_and_missing_parent_denial(self):
        ident = self.create()
        for fn in (lambda: self.observations.get(self.bob, ident),
                   lambda: self.observations.flag(self.bob, ident, operation_key="flag"),
                   lambda: self.observations.create(self.bob, self.invoice, operation_key="create"),
                   lambda: self.observations.create(self.bob, "f" * 32, operation_key="create")):
            with self.assertRaises(AccessDenied):
                fn()
        order = self.orders.create(self.bob, "Other", 0, operation_key="order")
        invoice = self.invoices.create(self.bob, order, operation_key="invoice")
        self.assertNotEqual(ident, self.observations.create(self.bob, invoice, operation_key="create"))

    def test_cancelled_parent_blocks_new_but_not_exact_retry(self):
        ident = self.create()
        self.invoices.cancel(self.alice, self.invoice, operation_key="cancel-invoice")
        self.assertEqual(self.create(), ident)
        with self.assertRaises(ObservationError):
            self.create("new")
        self.assertEqual(self.observations.get(self.alice, ident)["status"], "unverified")
        self.observations.discard(self.alice, ident, operation_key="discard")

    def test_cancelled_order_blocks_new_observation(self):
        self.orders.cancel(self.alice, self.order, operation_key="cancel-order")
        with self.assertRaises(ObservationError):
            self.create()
        with self.state.transaction() as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM payment_observations").fetchone()[0], 0)

    def test_sessions_and_invalid_input_denied(self):
        for parent, key in (("bad", "key"), (self.invoice, ""), (self.invoice, "x" * 129), (None, "key")):
            with self.assertRaises(ObservationError):
                self.observations.create(self.alice, parent, operation_key=key)
        self.auth.logout(self.bob)
        for token in (self.bob, "", "x" * 43):
            with self.assertRaises(AccessDenied):
                self.observations.create(token, self.invoice, operation_key="key")
        self.clock.now += SESSION_SECONDS
        with self.assertRaises(AccessDenied):
            self.observations.create(self.alice, self.invoice, operation_key="key")

    def test_cross_resource_and_transition_key_conflicts(self):
        ident = self.create()
        other_order = self.orders.create(self.alice, "Other", 50, operation_key="other-order")
        other_invoice = self.invoices.create(self.alice, other_order, operation_key="other-invoice")
        for fn in (lambda: self.observations.create(self.alice, other_invoice, operation_key="create"),
                   lambda: self.observations.flag(self.alice, ident, operation_key="create"),
                   lambda: self.observations.create(self.alice, self.invoice, operation_key="invoice")):
            with self.assertRaises(ObservationError):
                fn()
        from ai_earn.invoice_drafts import InvoiceDraftError
        with self.assertRaises(InvoiceDraftError):
            self.invoices.create(self.alice, self.order, operation_key="create")

    def test_concurrent_create_and_flag_retries(self):
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(self.observations.create, self.alice, self.invoice, operation_key="create") for _ in range(2)]
            ids = [f.result(timeout=10) for f in futures]
        self.assertEqual(ids[0], ids[1])
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(self.observations.flag, self.alice, ids[0], operation_key="flag") for _ in range(2)]
            self.assertEqual([f.result(timeout=10) for f in futures], ids)
        with self.state.transaction() as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM payment_observations").fetchone()[0], 1)
            self.assertEqual(conn.execute("SELECT count(*) FROM state_audit WHERE resource_type='payment_observation'").fetchone()[0], 2)

    def test_audit_omits_session_and_amount(self):
        ident = self.create()
        self.observations.flag(self.alice, ident, operation_key="flag")
        with self.state.transaction() as conn:
            rows = [tuple(row) for row in conn.execute("SELECT * FROM state_audit WHERE resource_type='payment_observation'")]
            self.assertNotIn(self.alice, str(rows))
            self.assertNotIn("12550", str(rows))
            self.assertTrue(all(row[-1] == ident for row in rows))

    def test_concurrent_parent_cancellation_serializes_creation(self):
        with ThreadPoolExecutor(max_workers=2) as pool:
            create = pool.submit(self.observations.create, self.alice, self.invoice, operation_key="create")
            cancel = pool.submit(self.invoices.cancel, self.alice, self.invoice, operation_key="cancel-invoice")
            try:
                ident = create.result(timeout=10)
            except ObservationError:
                ident = None
            self.assertEqual(cancel.result(timeout=10), self.invoice)
        with self.state.transaction() as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM payment_observations").fetchone()[0], int(ident is not None))
        if ident:
            self.assertEqual(self.create(), ident)

    def test_create_and_transition_failures_rollback(self):
        for helper in ("_audit", "_remember"):
            with patch("ai_earn.payment_observations." + helper, side_effect=RuntimeError("injected")):
                with self.assertRaises(RuntimeError):
                    self.create()
        ident = self.create()
        with patch("ai_earn.payment_observations._remember", side_effect=RuntimeError("injected")):
            with self.assertRaises(RuntimeError):
                self.observations.flag(self.alice, ident, operation_key="flag")
        self.assertEqual(self.observations.get(self.alice, ident)["status"], "unverified")
        self.observations.flag(self.alice, ident, operation_key="flag")

    def test_expiry_before_commit_rolls_back(self):
        from ai_earn.payment_observations import _audit
        def expire(*args):
            _audit(*args)
            self.clock.now += SESSION_SECONDS
        with patch("ai_earn.payment_observations._audit", side_effect=expire):
            with self.assertRaises(AccessDenied):
                self.create()
        with self.state.transaction() as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM payment_observations").fetchone()[0], 0)
            self.assertEqual(conn.execute("SELECT count(*) FROM state_operations WHERE resource_type='payment_observation'").fetchone()[0], 0)

    def test_ambiguous_auth_exit_retry(self):
        original = self.auth.storage.transaction
        @contextmanager
        def fail_exit():
            with original() as conn:
                yield conn
                raise RuntimeError("auth exit failure")
        with patch.object(self.auth.storage, "transaction", fail_exit):
            with self.assertRaises(RuntimeError):
                self.create()
        ident = self.create()
        self.assertEqual(self.observations.get(self.alice, ident)["status"], "unverified")
        with self.state.transaction() as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM payment_observations").fetchone()[0], 1)

    def test_sql_constraints_reject_financial_claims_and_cross_owner(self):
        bob = self.auth.profile(self.bob)["id"]
        alice = self.auth.profile(self.alice)["id"]
        for owner, amount, currency, status in ((bob, 1, "INR", "unverified"),
                                                 (alice, -1, "INR", "unverified"),
                                                 (alice, 1, "USD", "unverified"),
                                                 (alice, 1, "INR", "paid"),
                                                 (alice, 1, "INR", "confirmed"),
                                                 (alice, 1, "INR", "settled"),
                                                 (alice, 1.5, "INR", "unverified")):
            with self.assertRaises(StateStorageError):
                with self.state.transaction() as conn:
                    conn.execute("INSERT INTO payment_observations VALUES (?,?,?,?,?,?,?,?)",
                                 ("a" * 32, owner, self.invoice, amount, currency, status, self.clock.now, self.clock.now))

    def test_prior_services_backup_and_prior_schema(self):
        self.assertEqual(self.invoices.create(self.alice, self.order, operation_key="invoice"), self.invoice)
        self.assertEqual(self.queue.enqueue(self.alice, "Synthetic task", operation_key="queue"), self.task)
        task = TaskWorkflows(self.auth, self.state).create(self.alice, "Local", operation_key="workflow")
        self.assertEqual(TaskWorkflows(self.auth, self.state).get(self.alice, task)["status"], "planned")
        plan = TaskPlans(self.auth, self.state).create(self.alice, "Plan", operation_key="plan")
        opportunity = OpportunityDrafts(self.auth, self.state).create(self.alice, "Idea", operation_key="opportunity")
        self.assertEqual(TaskPlans(self.auth, self.state).get(self.alice, plan)["status"], "planned")
        self.assertEqual(OpportunityDrafts(self.auth, self.state).get(self.alice, opportunity)["status"], "draft")
        self.assertTrue(StateAudit(self.auth, self.state).list_events(self.alice))
        prior = StateStorage(self.directory / "v8.sqlite3")
        with self.assertRaises(StateStorageError):
            PaymentObservations(self.auth, prior).create(self.alice, self.invoice, operation_key="key")
        ident = self.create()
        backup = self.directory / "v9-copy.sqlite3"
        self.state.backup_to(backup, confirmed=True)
        self.assertEqual(PaymentObservations(self.auth, StateStorage(backup)).get(self.alice, ident)["status"], "unverified")

    def test_migration_confirmation_failure_nonoverwrite_and_tamper(self):
        prior = StateStorage(self.directory / "v8.sqlite3")
        backup = self.directory / "new.sqlite3"
        for confirmation in (False, 1):
            with self.assertRaises(StateStorageError):
                prior.migrate_to_v9(backup, confirmed=confirmation)
        self.assertFalse(backup.exists())
        backup.write_bytes(b"preserve")
        with self.assertRaises(StateStorageError):
            prior.migrate_to_v9(backup, confirmed=True)
        self.assertEqual(backup.read_bytes(), b"preserve")
        failed = self.directory / "failure.sqlite3"
        def fail(conn):
            conn.execute("CREATE TABLE payment_observations (id TEXT)")
            raise RuntimeError("failure")
        with patch("ai_earn.state_storage._apply_v9", side_effect=fail):
            with self.assertRaises(RuntimeError):
                prior.migrate_to_v9(failed, confirmed=True)
        for store in (prior, StateStorage(failed)):
            with store.transaction() as conn:
                self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 8)
        with closing(sqlite3.connect(self.state.path)) as conn, conn:
            conn.execute("DROP TABLE payment_observations")
            conn.execute("CREATE TABLE payment_observations(id TEXT)")
        with self.assertRaises(StateStorageError):
            StateStorage(self.state.path)

    def test_unique_parent_index_tamper_is_refused(self):
        with closing(sqlite3.connect(self.state.path)) as conn, conn:
            conn.execute("DROP INDEX invoice_drafts_id_owner_idx")
            conn.execute("CREATE INDEX invoice_drafts_id_owner_idx ON invoice_drafts(id, owner_id)")
        with self.assertRaises(StateStorageError):
            StateStorage(self.state.path)

    def test_active_v8_lease_survives_upgrade(self):
        prior = StateStorage(self.directory / "v8.sqlite3")
        queue = TaskQueue(self.auth, prior)
        lease = queue.claim(self.alice, self.task, operation_key="claim", lease_seconds=5)
        backup = self.directory / "active-v8.sqlite3"
        prior.migrate_to_v9(backup, confirmed=True)
        self.assertTrue(queue.get_lease(self.alice, lease)["active"])
        self.clock.now += 5
        queue.recover(self.alice, self.task, lease, operation_key="recover")
        self.assertEqual(queue.get(self.alice, self.task)["status"], "queued")
        self.assertEqual(TaskQueue(self.auth, StateStorage(backup)).get(self.alice, self.task)["status"], "leased")

    def test_inconsistent_v8_queue_prevents_upgrade(self):
        prior = StateStorage(self.directory / "v8.sqlite3")
        lease = TaskQueue(self.auth, prior).claim(self.alice, self.task, operation_key="claim", lease_seconds=5)
        with closing(sqlite3.connect(prior.path)) as conn, conn:
            conn.execute("UPDATE queue_leases SET lease_until=lease_until+1 WHERE id=?", (lease,))
            self.assertEqual(conn.execute("PRAGMA quick_check").fetchone()[0], "ok")
        backup = self.directory / "inconsistent.sqlite3"
        with self.assertRaises(StateStorageError):
            prior.migrate_to_v9(backup, confirmed=True)
        self.assertFalse(backup.exists())
        with closing(sqlite3.connect(prior.path)) as conn:
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 8)

    def test_migration_integrity_failure_leaves_source_unchanged(self):
        prior = StateStorage(self.directory / "v8.sqlite3")
        backup = self.directory / "bad.sqlite3"
        with patch("ai_earn.state_storage._check_integrity", side_effect=[None, StateStorageError("bad copy")]):
            with self.assertRaises(StateStorageError):
                prior.migrate_to_v9(backup, confirmed=True)
        self.assertTrue(backup.exists())
        with prior.transaction() as conn:
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 8)

    def test_wal_backup_includes_committed_frames(self):
        prior = StateStorage(self.directory / "v8.sqlite3")
        backup = self.directory / "wal.sqlite3"
        with closing(sqlite3.connect(prior.path)) as conn:
            self.assertEqual(conn.execute("PRAGMA journal_mode=WAL").fetchone()[0].lower(), "wal")
            conn.execute("UPDATE task_queue SET title=? WHERE id=?", ("WAL task", self.task))
            conn.commit()
            self.assertTrue(Path(str(prior.path) + "-wal").exists())
            prior.migrate_to_v9(backup, confirmed=True)
            with StateStorage(backup).transaction() as snapshot:
                self.assertEqual(snapshot.execute("SELECT title FROM task_queue WHERE id=?", (self.task,)).fetchone()[0], "WAL task")
                self.assertEqual(snapshot.execute("PRAGMA user_version").fetchone()[0], 8)

    def test_crash_during_migration_leaves_restorable_v8_backup(self):
        source = self.directory / "v8.sqlite3"
        backup = self.directory / "crash.sqlite3"
        code = '''
import os, sys
from pathlib import Path
import ai_earn.state_storage as storage
def crash(conn):
    conn.execute("CREATE TABLE payment_observations (id TEXT)")
    os._exit(37)
storage._apply_v9 = crash
storage.StateStorage(Path(sys.argv[1])).migrate_to_v9(Path(sys.argv[2]), confirmed=True)
'''
        result = subprocess.run([sys.executable, "-c", code, str(source), str(backup)], timeout=20)
        self.assertEqual(result.returncode, 37)
        restored = self.directory / "restored.sqlite3"
        StateStorage(backup).backup_to(restored, confirmed=True)
        self.assertEqual(InvoiceDrafts(self.auth, StateStorage(restored)).get(self.alice, self.invoice)["status"], "draft")

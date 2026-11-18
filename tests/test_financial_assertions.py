"""Local asserted stages only; no contracts, invoices, payments or revenue."""
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from ai_earn.auth import AccessDenied, AuthService, SESSION_SECONDS
from ai_earn.auth_storage import AuthStorage
from ai_earn.state_storage import StateStorage, StateStorageError


class Clock:
    now = 1_800_000_000
    def __call__(self):
        return self.now


@unittest.skipUnless(os.name in {"posix", "nt"}, "requires a supported private filesystem")
class FinancialAssertionsTests(unittest.TestCase):
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
        for version in range(2, 13):
            getattr(self.state, f"migrate_to_v{version}")(self.directory / f"before-v{version}.sqlite3", confirmed=True)

    def upgrade(self):
        self.state.migrate_to_v13(self.directory / "before-v13.sqlite3", confirmed=True)
        from ai_earn.financial_assertions import FinancialAssertions
        return FinancialAssertions(self.auth, self.state)

    def test_migration_requires_confirmation_backup_and_older_version(self):
        with self.assertRaises(StateStorageError):
            self.state.migrate_to_v13(self.directory / "before-v13.sqlite3")
        service = self.upgrade()
        with StateStorage(self.directory / "before-v13.sqlite3").transaction() as conn:
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 12)
        with self.assertRaises(StateStorageError):
            self.state.migrate_to_v13(self.directory / "another.sqlite3", confirmed=True)
        with self.state.transaction() as conn:
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 13)
        self.assertEqual(service.list(self.alice), [])

    def test_legal_stages_never_confirm_or_settle_and_retries_are_historical(self):
        service = self.upgrade()
        ident = service.create(self.alice, "Synthetic", 12550, operation_key="create")
        self.assertEqual(service.create(self.alice, "Synthetic", 12550, operation_key="create"), ident)
        for stage in ("quote_asserted", "contract_asserted", "invoice_asserted", "initiated_asserted"):
            self.assertEqual(service.advance(self.alice, ident, stage, confirmed=True, operation_key=stage), ident)
        view = service.get(self.alice, ident)
        self.assertEqual(view["stage"], "initiated_asserted")
        self.assertEqual((view["verification"], view["amount_kind"]), ("unverified", "asserted_not_revenue"))
        self.assertEqual(service.list(self.alice)[0]["verification"], "unverified")
        self.assertEqual(service.advance(self.alice, ident, "quote_asserted", confirmed=True, operation_key="quote_asserted"), ident)
        service.withdraw(self.alice, ident, confirmed=True, operation_key="withdraw")
        self.assertEqual(service.get(self.alice, ident)["stage"], "withdrawn")
        self.assertEqual(service.withdraw(self.alice, ident, confirmed=True, operation_key="withdraw"), ident)
        self.state.backup_to(self.directory / "after.sqlite3", confirmed=True)

    def test_invalid_transitions_amounts_confirmation_and_owner_denied(self):
        from ai_earn.financial_assertions import FinancialAssertionError
        service = self.upgrade()
        for value in (-1, True, 1.0, "10", None, 1_000_000_000_001):
            with self.assertRaises(FinancialAssertionError):
                service.create(self.alice, "synthetic", value, operation_key="invalid")
        ident = service.create(self.alice, "synthetic", 0, operation_key="create")
        for stage in ("confirmed", "settled", "initiated_asserted", "potential", "withdrawn", "bad", None):
            with self.assertRaises(FinancialAssertionError):
                service.advance(self.alice, ident, stage, confirmed=True, operation_key="bad")
        for confirmation in (False, None, 1):
            with self.assertRaises(FinancialAssertionError):
                service.advance(self.alice, ident, "quote_asserted", confirmed=confirmation, operation_key="bad")
        with self.assertRaises(AccessDenied):
            service.get(self.bob, ident)
        with self.assertRaises(AccessDenied):
            service.advance(self.bob, ident, "quote_asserted", confirmed=True, operation_key="bad")
        with self.assertRaises(FinancialAssertionError):
            service.create(self.alice, "different", 0, operation_key="create")
        self.assertEqual(service.list(self.bob), [])

    def test_rollback_session_expiry_and_same_key_concurrency(self):
        import ai_earn.financial_assertions as module
        service = self.upgrade()
        with patch.object(module, "_audit", side_effect=RuntimeError("injected")):
            with self.assertRaises(RuntimeError):
                service.create(self.alice, "synthetic", 1, operation_key="create")
        self.assertEqual(service.list(self.alice), [])
        with ThreadPoolExecutor(max_workers=2) as pool:
            calls = [pool.submit(service.create, self.alice, "synthetic", 1, operation_key="create") for _ in range(2)]
            first, second = [c.result(timeout=20) for c in calls]
        self.assertEqual(first, second)
        original = module._audit
        def expire(*args):
            original(*args)
            self.clock.now += SESSION_SECONDS
        with patch.object(module, "_audit", side_effect=expire):
            with self.assertRaises(AccessDenied):
                service.advance(self.alice, first, "quote_asserted", confirmed=True, operation_key="quote")
        self.alice = self.auth.login("alice", "disposable test password")
        self.assertEqual(service.get(self.alice, first)["stage"], "potential")
        service.advance(self.alice, first, "quote_asserted", confirmed=True, operation_key="quote")

    def test_prior_services_remain_available_on_v13(self):
        service = self.upgrade()
        from ai_earn.order_drafts import OrderDrafts
        from ai_earn.task_queue import TaskQueue, QueueError
        from ai_earn.task_workflows import TaskWorkflows
        from ai_earn.task_associations import TaskAssociations
        order = OrderDrafts(self.auth, self.state).create(self.alice, "sample", 100, operation_key="order")
        queue = TaskQueue(self.auth, self.state)
        workflows = TaskWorkflows(self.auth, self.state)
        associations = TaskAssociations(self.auth, self.state)
        q = queue.enqueue(self.alice, "task", operation_key="queue")
        w = workflows.create(self.alice, "work", operation_key="workflow")
        pair = associations.create_managed(self.alice, q, w, operation_key="managed")
        self.assertEqual(associations.ready_managed(self.alice)[0]["association_id"], pair)
        with self.assertRaises(QueueError):
            queue.claim(self.alice, q, operation_key="direct", lease_seconds=30)
        self.assertEqual(OrderDrafts(self.auth, self.state).get(self.alice, order)["status"], "draft")
        self.assertEqual(service.list(self.alice), [])

    def test_v13_managed_pair_can_complete_and_draft_services_remain_usable(self):
        self.upgrade()
        from ai_earn.task_queue import TaskQueue
        from ai_earn.task_workflows import TaskWorkflows
        from ai_earn.task_associations import TaskAssociations
        from ai_earn.financial_draft_workflows import FinancialDraftWorkflows
        from ai_earn.order_drafts import OrderDrafts
        from ai_earn.invoice_drafts import InvoiceDrafts
        from ai_earn.payment_observations import PaymentObservations
        queue, workflows = TaskQueue(self.auth, self.state), TaskWorkflows(self.auth, self.state)
        associations = TaskAssociations(self.auth, self.state)
        q = queue.enqueue(self.alice, "task", operation_key="q")
        w = workflows.create(self.alice, "task", operation_key="w")
        pair = associations.create_managed(self.alice, q, w, operation_key="pair")
        lease = associations.start_pair(self.alice, pair, lease_seconds=30, operation_key="start")
        associations.submit_pair(self.alice, pair, lease, "output", operation_key="submit")
        associations.complete_pair(self.alice, pair, lease, "output", confirmed=True, operation_key="complete")
        self.assertEqual(associations.inspect(self.alice, pair)["queue_status"], "completed")
        order = OrderDrafts(self.auth, self.state).create(self.alice, "order", 100, operation_key="order")
        invoice = InvoiceDrafts(self.auth, self.state).create(self.alice, order, operation_key="invoice")
        observation = PaymentObservations(self.auth, self.state).create(self.alice, invoice, operation_key="observation")
        FinancialDraftWorkflows(self.auth, self.state).withdraw(self.alice, order, confirmed=True, operation_key="withdraw")
        self.assertEqual(PaymentObservations(self.auth, self.state).get(self.alice, observation)["status"], "discarded")
        self.state.backup_to(self.directory / "compatibility.sqlite3", confirmed=True)

    def test_creation_binding_detects_swapped_same_owner_results_and_amount_changes(self):
        service = self.upgrade()
        first = service.create(self.alice, "same", 100, operation_key="first")
        second = service.create(self.alice, "same", 100, operation_key="second")
        bob = service.create(self.bob, "private", 200, operation_key="first")
        with self.state.transaction() as conn:
            conn.execute("UPDATE state_operations SET resource_id=? WHERE operation_key='first' AND resource_type='financial_assertion' AND resource_id=?", (second, first))
        with self.assertRaises(StateStorageError):
            service.create(self.alice, "same", 100, operation_key="first")
        with self.assertRaises(StateStorageError):
            self.state.backup_to(self.directory / "swapped.sqlite3", confirmed=True)
        self.assertEqual(service.get(self.bob, bob)["amount_paise"], 200)

    def test_direct_amount_tampering_is_detected_but_coordinated_forgery_is_outside_boundary(self):
        service = self.upgrade()
        ident = service.create(self.alice, "synthetic", 100, operation_key="create")
        with self.state.transaction() as conn:
            conn.execute("UPDATE financial_assertions SET amount_paise=101 WHERE id=?", (ident,))
        with self.assertRaises(StateStorageError):
            service.get(self.alice, ident)
        with self.assertRaises(StateStorageError):
            self.state.backup_to(self.directory / "changed-amount.sqlite3", confirmed=True)

    def test_pagination_input_and_expiry(self):
        from ai_earn.financial_assertions import FinancialAssertionError
        service = self.upgrade()
        ids = [service.create(self.alice, "synthetic", n, operation_key=f"item{n}") for n in range(3)]
        page = service.list(self.alice, limit=2)
        next_page = service.list(self.alice, after_id=page[-1]["id"], limit=2)
        self.assertEqual([v["id"] for v in page + next_page], sorted(ids))
        for value in (True, 0, 101, "1", None):
            with self.assertRaises(FinancialAssertionError):
                service.list(self.alice, limit=value)
        with self.assertRaises(FinancialAssertionError):
            service.list(self.alice, after_id="invalid")
        self.clock.now += SESSION_SECONDS
        with self.assertRaises(AccessDenied):
            service.list(self.alice)

    def test_migration_rollback_reserved_resource_and_existing_backup(self):
        # A previously unrecognized retry resource type cannot be silently adopted.
        with self.state.transaction() as conn:
            conn.execute("INSERT INTO state_operations VALUES (?,?,?,?,?)", ("a"*32, "reserved", b"x"*32, "financial_assertion", "b"*32))
        backup = self.directory / "reserved.sqlite3"
        with self.assertRaises(StateStorageError):
            self.state.migrate_to_v13(backup, confirmed=True)
        with self.state.transaction() as conn:
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 12)
        self.assertTrue(backup.exists())
        with self.assertRaises(StateStorageError):
            self.state.migrate_to_v13(backup, confirmed=True)

    def test_recovery_comparison_includes_v13_assertions_and_refuses_cross_version(self):
        from ai_earn.state_recovery_comparison import StateRecoveryComparison
        service = self.upgrade()
        service.create(self.alice, "synthetic", 100, operation_key="create")
        old = StateStorage(self.directory / "before-v13.sqlite3")
        with self.assertRaises(StateStorageError):
            StateRecoveryComparison(self.auth, self.state, old).compare_records(self.alice)
        backup = self.directory / "same.sqlite3"
        self.state.backup_to(backup, confirmed=True)
        comparison = StateRecoveryComparison(self.auth, self.state, StateStorage(backup)).compare_records(self.alice)
        self.assertEqual(comparison["tables"]["financial_assertions"]["matching"], 1)

    def test_controlled_creation_crashes_before_and_after_commit(self):
        service = self.upgrade()
        code = r"""
from contextlib import contextmanager
from pathlib import Path
import json, os, sys
import ai_earn.financial_assertions as module
from ai_earn.auth import AuthService
from ai_earn.auth_storage import AuthStorage
from ai_earn.state_storage import StateStorage
p = json.load(sys.stdin)
auth = AuthService(AuthStorage(Path(p['auth']), create=False), lambda: 1800000000)
service = module.FinancialAssertions(auth, StateStorage(Path(p['state'])))
if p['point'] == 'before':
    original = module._audit
    def crash(*args):
        original(*args)
        os._exit(73)
    module._audit = crash
else:
    original = auth.storage.transaction
    @contextmanager
    def crash_after_state_commit():
        with original() as conn:
            yield conn
            os._exit(74)
    auth.storage.transaction = crash_after_state_commit
service.create(p['token'], 'synthetic', 123, operation_key='create')
"""
        def run(point):
            payload = dict(auth=str(self.auth.storage.path), state=str(self.state.path), token=self.alice, point=point)
            return subprocess.run([sys.executable, "-c", code], input=json.dumps(payload), text=True, capture_output=True, timeout=30)
        self.assertEqual(run("before").returncode, 73)
        self.assertEqual(service.list(self.alice), [])
        self.assertEqual(run("after").returncode, 74)
        ident = service.create(self.alice, "synthetic", 123, operation_key="create")
        self.assertEqual([row["id"] for row in service.list(self.alice)], [ident])
        self.state.backup_to(self.directory / "crash.sqlite3", confirmed=True)

    def test_migration_process_crash_retains_v12_and_checked_backup(self):
        code = r"""
from pathlib import Path
import os, sys
import ai_earn.state_storage as module
s = module.StateStorage(Path(sys.argv[1]))
original = module._apply_v13
def crash(conn):
    original(conn)
    os._exit(73)
module._apply_v13 = crash
s.migrate_to_v13(Path(sys.argv[2]), confirmed=True)
"""
        backup = self.directory / "crash-before-v13.sqlite3"
        result = subprocess.run([sys.executable, "-c", code, str(self.state.path), str(backup)], capture_output=True, timeout=30)
        self.assertEqual(result.returncode, 73)
        for path in (self.state.path, backup):
            with StateStorage(path).transaction() as conn:
                self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 12)
        self.state.migrate_to_v13(self.directory / "retry-before-v13.sqlite3", confirmed=True)

    def test_transition_collision_and_two_competing_keys(self):
        from ai_earn.financial_assertions import FinancialAssertionError
        service = self.upgrade()
        ident = service.create(self.alice, "synthetic", 100, operation_key="create")
        with ThreadPoolExecutor(max_workers=2) as pool:
            calls = [pool.submit(service.advance, self.alice, ident, "quote_asserted", confirmed=True, operation_key=key)
                     for key in ("quote-a", "quote-b")]
            outcomes = []
            for call in calls:
                try:
                    outcomes.append(call.result(timeout=20))
                except FinancialAssertionError:
                    outcomes.append(None)
        self.assertEqual(sum(v == ident for v in outcomes), 1)
        self.assertEqual(service.get(self.alice, ident)["stage"], "quote_asserted")
        self.state.backup_to(self.directory / "competing.sqlite3", confirmed=True)

    def test_checked_backup_refuses_changed_stage_history(self):
        service = self.upgrade()
        ident = service.create(self.alice, "synthetic", 100, operation_key="create")
        with self.state.transaction() as conn:
            conn.execute("UPDATE financial_assertions SET stage='invoice_asserted' WHERE id=?", (ident,))
        with self.assertRaises(StateStorageError):
            self.state.backup_to(self.directory / "bad.sqlite3", confirmed=True)

"""Disposable persistent synthetic payment events: never real financial data."""
from concurrent.futures import ThreadPoolExecutor
import hashlib
import hmac
import json
import os
import subprocess
import sys
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from ai_earn.auth import AccessDenied, AuthService, SESSION_SECONDS
from ai_earn.auth_storage import AuthStorage
from ai_earn.state_storage import StateStorage, StateStorageError

KEY = b"disposable-test-key-not-for-use!"
NOW = 1_800_000_000


class Clock:
    now = NOW
    def __call__(self):
        return self.now


def payload(pid, event_id="evt1", kind="confirmation", sequence=1, amount=1200, at=NOW):
    return json.dumps(dict(event_id=event_id, event_type=kind, sequence=sequence, payment_id=pid,
                           amount_paise=amount, currency="INR", occurred_at=at), separators=(",", ":")).encode()


def sign(body, key=KEY):
    return hmac.new(key, body, hashlib.sha256).hexdigest()


@unittest.skipUnless(os.name in {"posix", "nt"}, "requires a supported private filesystem")
class DurableSandboxPaymentsTests(unittest.TestCase):
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
        for version in range(2, 14):
            getattr(self.state, f"migrate_to_v{version}")(self.directory / f"before-v{version}.sqlite3", confirmed=True)

    def upgrade(self):
        self.state.migrate_to_v14(self.directory / "before-v14.sqlite3", confirmed=True)
        from ai_earn.durable_sandbox_payments import DurableSandboxPayments
        return DurableSandboxPayments(self.auth, self.state, KEY)

    def test_snapshot_comparison_covers_owned_events_and_divergence(self):
        from ai_earn.durable_sandbox_payments import DurableSandboxPayments
        from ai_earn.state_recovery_comparison import StateRecoveryComparison
        service = self.upgrade()
        alice = service.create(self.alice, 1200, operation_key="create")
        bob = service.create(self.bob, 1200, operation_key="create")
        for token, pid in ((self.alice, alice), (self.bob, bob)):
            body = payload(pid)
            service.accept(token, pid, body, sign(body), operation_key="first")
        path = self.directory / "snapshot.sqlite3"
        self.state.backup_to(path, confirmed=True)
        candidate = StateStorage(path)
        comparison = StateRecoveryComparison(self.auth, self.state, candidate)
        expected = {"matching": 1, "current_only": 0, "candidate_only": 0, "conflicts": 0}
        self.assertEqual(comparison.compare_snapshot(self.alice)["sandbox_payment_events"], expected)
        service_copy = DurableSandboxPayments(self.auth, candidate, KEY)
        body = payload(alice, "evt2", "settlement", 2)
        service.accept(self.alice, alice, body, sign(body), operation_key="next")
        alternate = payload(alice, "evt2", "settlement", 2, at=NOW - 1)
        service_copy.accept(self.alice, alice, alternate, sign(alternate), operation_key="next")
        result = comparison.compare_snapshot(self.alice)
        self.assertEqual(result["sandbox_payment_events"], {"matching": 1, "current_only": 0, "candidate_only": 0, "conflicts": 1})
        self.assertEqual(result["operations"]["conflicts"], 1)
        self.assertEqual(comparison.compare_snapshot(self.bob)["sandbox_payment_events"], expected)
        with self.assertRaises(AccessDenied):
            comparison.compare_snapshot("invalid")

    def test_snapshot_comparison_detects_one_sided_event_and_old_schema(self):
        from ai_earn.state_recovery_comparison import StateRecoveryComparison
        before = self.directory / "v13-copy.sqlite3"
        self.state.backup_to(before, confirmed=True)
        old = StateRecoveryComparison(self.auth, self.state, StateStorage(before)).compare_snapshot(self.alice)
        self.assertIsNone(old["sandbox_payment_events"])
        service = self.upgrade()
        pid = service.create(self.alice, 1200, operation_key="create")
        path = self.directory / "old-copy.sqlite3"
        self.state.backup_to(path, confirmed=True)
        body = payload(pid)
        service.accept(self.alice, pid, body, sign(body), operation_key="event")
        comparison = StateRecoveryComparison(self.auth, self.state, StateStorage(path))
        self.assertEqual(comparison.compare_snapshot(self.alice)["sandbox_payment_events"],
                         {"matching": 0, "current_only": 1, "candidate_only": 0, "conflicts": 0})
        reverse = StateRecoveryComparison(self.auth, StateStorage(path), self.state)
        self.assertEqual(reverse.compare_snapshot(self.alice)["sandbox_payment_events"]["candidate_only"], 1)

    def test_snapshot_comparison_fails_closed_on_corruption_and_session_expiry(self):
        from ai_earn.state_recovery_comparison import StateRecoveryComparison
        import ai_earn.state_recovery_comparison as module
        service = self.upgrade()
        pid = service.create(self.alice, 1200, operation_key="create")
        body = payload(pid)
        service.accept(self.alice, pid, body, sign(body), operation_key="event")
        path = self.directory / "snapshot.sqlite3"
        self.state.backup_to(path, confirmed=True)
        candidate = StateStorage(path)
        comparison = StateRecoveryComparison(self.auth, self.state, candidate)
        original = module._compare_sandbox_events
        def expire(*args):
            result = original(*args)
            self.clock.now += SESSION_SECONDS
            return result
        with patch.object(module, "_compare_sandbox_events", side_effect=expire):
            with self.assertRaises(AccessDenied):
                comparison.compare_snapshot(self.alice)
        self.alice = self.auth.login("alice", "disposable test password")
        with candidate.transaction() as conn:
            conn.execute("UPDATE sandbox_payment_events SET body_hash=?", (b"x" * 32,))
        with self.assertRaises(StateStorageError):
            comparison.compare_snapshot(self.alice)
        with self.assertRaises(StateStorageError):
            StateRecoveryComparison(self.auth, self.state, self.state).compare_snapshot(self.alice)

    def test_migration_preserves_backup_and_requires_confirmation(self):
        with self.assertRaises(StateStorageError):
            self.state.migrate_to_v14(self.directory / "before-v14.sqlite3")
        service = self.upgrade()
        with StateStorage(self.directory / "before-v14.sqlite3").transaction() as conn:
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 13)
        self.assertEqual(service.list(self.alice), [])

    def test_event_persists_across_reopen_and_exact_retries_are_historical(self):
        service = self.upgrade()
        pid = service.create(self.alice, 1200, operation_key="create")
        first = payload(pid)
        result = service.accept(self.alice, pid, first, sign(first), operation_key="first")
        self.assertEqual(result, {"event_id": "evt1", "acknowledged_stage": "sandbox_confirmed", "scope": "synthetic_only"})
        from ai_earn.durable_sandbox_payments import DurableSandboxPayments
        reopened = DurableSandboxPayments(self.auth, StateStorage(self.state.path), KEY)
        self.assertEqual(reopened.accept(self.alice, pid, first, sign(first), operation_key="first"), result)
        second = payload(pid, "evt2", "settlement", 2)
        self.assertEqual(reopened.accept(self.alice, pid, second, sign(second), operation_key="second")["acknowledged_stage"], "sandbox_settled")
        self.assertEqual(reopened.accept(self.alice, pid, first, sign(first), operation_key="first"), result)
        self.assertEqual(reopened.get(self.alice, pid)["stage"], "sandbox_settled")
        self.state.backup_to(self.directory / "after.sqlite3", confirmed=True)

    def test_denial_invalid_signatures_and_conflicting_or_out_of_order_events(self):
        from ai_earn.durable_sandbox_payments import DurableSandboxError
        service = self.upgrade()
        pid = service.create(self.alice, 1200, operation_key="create")
        body = payload(pid)
        with self.assertRaises(AccessDenied):
            service.get(self.bob, pid)
        with self.assertRaises(AccessDenied):
            service.accept(self.bob, pid, body, sign(body), operation_key="event")
        with self.assertRaises(DurableSandboxError):
            service.accept(self.alice, pid, body, "0"*64, operation_key="event")
        bad = payload(pid, "evt2", "settlement", 2)
        with self.assertRaises(DurableSandboxError):
            service.accept(self.alice, pid, bad, sign(bad), operation_key="event")
        service.accept(self.alice, pid, body, sign(body), operation_key="event")
        conflict = payload(pid, "evt1", "settlement", 2)
        with self.assertRaises(DurableSandboxError):
            service.accept(self.alice, pid, conflict, sign(conflict), operation_key="other")
        self.assertEqual(service.get(self.alice, pid)["stage"], "sandbox_confirmed")

    def test_concurrency_and_rollback(self):
        from ai_earn.durable_sandbox_payments import DurableSandboxPayments
        service = self.upgrade()
        pid = service.create(self.alice, 1200, operation_key="create")
        body = payload(pid)
        import ai_earn.durable_sandbox_payments as module
        with patch.object(module, "_audit", side_effect=RuntimeError("injected")):
            with self.assertRaises(RuntimeError):
                service.accept(self.alice, pid, body, sign(body), operation_key="event")
        self.assertEqual(service.get(self.alice, pid)["stage"], "sandbox_awaiting")
        with ThreadPoolExecutor(max_workers=2) as pool:
            calls = [pool.submit(service.accept, self.alice, pid, body, sign(body), operation_key="event") for _ in range(2)]
            results = [f.result(timeout=20) for f in calls]
        self.assertEqual(results[0], results[1])
        self.state.backup_to(self.directory / "race.sqlite3", confirmed=True)
        restored = DurableSandboxPayments(self.auth, StateStorage(self.directory / "race.sqlite3"), KEY)
        self.assertEqual(restored.accept(self.alice, pid, body, sign(body), operation_key="event"), results[0])

    def test_v14_prior_financial_and_managed_workflows_remain_usable(self):
        service = self.upgrade()
        from ai_earn.financial_assertions import FinancialAssertions
        from ai_earn.task_queue import TaskQueue, QueueError
        from ai_earn.task_workflows import TaskWorkflows
        from ai_earn.task_associations import TaskAssociations
        assertion = FinancialAssertions(self.auth, self.state).create(self.alice, "sample", 100, operation_key="assertion")
        self.assertEqual(FinancialAssertions(self.auth, self.state).get(self.alice, assertion)["verification"], "unverified")
        queue, workflows = TaskQueue(self.auth, self.state), TaskWorkflows(self.auth, self.state)
        pairs = TaskAssociations(self.auth, self.state)
        q = queue.enqueue(self.alice, "task", operation_key="q")
        w = workflows.create(self.alice, "task", operation_key="w")
        pair = pairs.create_managed(self.alice, q, w, operation_key="pair")
        with self.assertRaises(QueueError):
            queue.claim(self.alice, q, lease_seconds=10, operation_key="direct")
        lease = pairs.start_pair(self.alice, pair, lease_seconds=30, operation_key="start")
        pairs.submit_pair(self.alice, pair, lease, "output", operation_key="submit")
        pairs.complete_pair(self.alice, pair, lease, "output", confirmed=True, operation_key="complete")
        pid = service.create(self.alice, 1200, operation_key="synthetic")
        self.assertEqual(service.get(self.alice, pid)["scope"], "synthetic_only")
        self.state.backup_to(self.directory / "compatibility.sqlite3", confirmed=True)

    def test_forged_signature_stale_event_and_wrong_key_cannot_advance(self):
        from ai_earn.durable_sandbox_payments import DurableSandboxError, DurableSandboxPayments
        service = self.upgrade()
        pid = service.create(self.alice, 1200, operation_key="create")
        wrong = DurableSandboxPayments(self.auth, self.state, b"x" * 32)
        body = payload(pid)
        with self.assertRaises(DurableSandboxError):
            wrong.accept(self.alice, pid, body, sign(body, b"x" * 32), operation_key="wrong")
        self.clock.now += 301
        with self.assertRaises(DurableSandboxError):
            service.accept(self.alice, pid, body, sign(body), operation_key="stale")
        self.assertEqual(service.get(self.alice, pid)["stage"], "sandbox_awaiting")
        self.assertNotIn("key_fingerprint", service.get(self.alice, pid))

    def test_owner_event_ids_conflict_across_payments_and_are_isolated_between_owners(self):
        from ai_earn.durable_sandbox_payments import DurableSandboxError
        service = self.upgrade()
        first = service.create(self.alice, 1200, operation_key="first")
        second = service.create(self.alice, 1200, operation_key="second")
        bob = service.create(self.bob, 1200, operation_key="first")
        body = payload(first)
        result = service.accept(self.alice, first, body, sign(body), operation_key="event")
        # An exact duplicate under a new key does not reserve that unused key.
        self.assertEqual(service.accept(self.alice, first, body, sign(body), operation_key="unused"), result)
        self.assertNotEqual(service.create(self.alice, 1, operation_key="unused"), first)
        other = payload(second)
        with self.assertRaises(DurableSandboxError):
            service.accept(self.alice, second, other, sign(other), operation_key="other")
        bbody = payload(bob)
        self.assertEqual(service.accept(self.bob, bob, bbody, sign(bbody), operation_key="event")["scope"], "synthetic_only")
        self.state.backup_to(self.directory / "owners.sqlite3", confirmed=True)

    def test_freshness_and_session_expiry_recheck_roll_back(self):
        service = self.upgrade()
        pid = service.create(self.alice, 1200, operation_key="create")
        body = payload(pid)
        import ai_earn.durable_sandbox_payments as module
        original = module._audit
        def expire(*args):
            original(*args)
            self.clock.now += SESSION_SECONDS
        # Isolate the session recheck from the independent freshness guard.
        with patch.object(module, "_audit", side_effect=expire), patch.object(service, "_recheck_event"):
            with self.assertRaises(AccessDenied):
                service.accept(self.alice, pid, body, sign(body), operation_key="event")
        self.alice = self.auth.login("alice", "disposable test password")
        self.assertEqual(service.get(self.alice, pid)["stage"], "sandbox_awaiting")
        fresh = payload(pid, at=self.clock.now)
        service.accept(self.alice, pid, fresh, sign(fresh), operation_key="event")

    def test_corrupt_payload_hash_and_wrong_operation_result_fail_closed(self):
        service = self.upgrade()
        pid = service.create(self.alice, 1200, operation_key="create")
        body = payload(pid)
        service.accept(self.alice, pid, body, sign(body), operation_key="event")
        with self.state.transaction() as conn:
            conn.execute("UPDATE sandbox_payment_events SET body_hash=? WHERE payment_id=?", (b"x"*32, pid))
        with self.assertRaises(StateStorageError):
            service.get(self.alice, pid)
        with self.assertRaises(StateStorageError):
            self.state.backup_to(self.directory / "tampered.sqlite3", confirmed=True)

    def test_controlled_process_crashes_around_event_commit(self):
        service = self.upgrade()
        pid = service.create(self.alice, 1200, operation_key="create")
        code = r"""
from contextlib import contextmanager
from pathlib import Path
import hashlib, hmac, json, os, sys
import ai_earn.durable_sandbox_payments as module
from ai_earn.auth import AuthService
from ai_earn.auth_storage import AuthStorage
from ai_earn.state_storage import StateStorage
p = json.load(sys.stdin)
auth = AuthService(AuthStorage(Path(p['auth']), create=False), lambda: 1800000000)
service = module.DurableSandboxPayments(auth, StateStorage(Path(p['state'])), b'disposable-test-key-not-for-use!')
body = json.dumps(dict(event_id='evt1', event_type='confirmation', sequence=1, payment_id=p['id'], amount_paise=1200, currency='INR', occurred_at=1800000000), separators=(',', ':')).encode()
signature = hmac.new(b'disposable-test-key-not-for-use!', body, hashlib.sha256).hexdigest()
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
service.accept(p['token'], p['id'], body, signature, operation_key='event')
"""
        def run(point):
            data = dict(auth=str(self.auth.storage.path), state=str(self.state.path), token=self.alice, id=pid, point=point)
            return subprocess.run([sys.executable, "-c", code], input=json.dumps(data), text=True, capture_output=True, timeout=30)
        self.assertEqual(run("before").returncode, 73)
        self.assertEqual(service.get(self.alice, pid)["stage"], "sandbox_awaiting")
        self.assertEqual(run("after").returncode, 74)
        body = payload(pid)
        self.assertEqual(service.accept(self.alice, pid, body, sign(body), operation_key="event")["acknowledged_stage"], "sandbox_confirmed")
        self.state.backup_to(self.directory / "crash.sqlite3", confirmed=True)

    def test_v14_cross_version_comparison_refused_and_synthetic_records_recognized(self):
        from ai_earn.state_recovery_comparison import StateRecoveryComparison
        service = self.upgrade()
        service.create(self.alice, 1200, operation_key="create")
        before = StateStorage(self.directory / "before-v14.sqlite3")
        with self.assertRaises(StateStorageError):
            StateRecoveryComparison(self.auth, self.state, before).compare_records(self.alice)
        path = self.directory / "compare.sqlite3"
        self.state.backup_to(path, confirmed=True)
        result = StateRecoveryComparison(self.auth, self.state, StateStorage(path)).compare_records(self.alice)
        self.assertEqual(result["tables"]["sandbox_payments"]["matching"], 1)

    def test_event_expiring_before_commit_rolls_back(self):
        import ai_earn.durable_sandbox_payments as module
        from ai_earn.durable_sandbox_payments import DurableSandboxError
        service = self.upgrade()
        pid = service.create(self.alice, 1200, operation_key="create")
        body = payload(pid)
        original = module._audit
        def expire(*args):
            original(*args)
            self.clock.now += 301
        with patch.object(module, "_audit", side_effect=expire):
            with self.assertRaises(DurableSandboxError):
                service.accept(self.alice, pid, body, sign(body), operation_key="event")
        self.assertEqual(service.get(self.alice, pid)["stage"], "sandbox_awaiting")
        fresh = payload(pid, at=self.clock.now)
        service.accept(self.alice, pid, fresh, sign(fresh), operation_key="event")

    def test_invalid_configuration_inputs_and_cross_resource_key_conflict(self):
        from ai_earn.durable_sandbox_payments import DurableSandboxError, DurableSandboxPayments
        self.upgrade()
        for key in (None, "invalid", b"short", b"x"*129):
            with self.assertRaises(DurableSandboxError):
                DurableSandboxPayments(self.auth, self.state, key)
        service = DurableSandboxPayments(self.auth, self.state, KEY)
        for amount in (True, -1, 1.0, "1200", None, 1_000_000_000_001):
            with self.assertRaises(DurableSandboxError):
                service.create(self.alice, amount, operation_key="invalid")
        pid = service.create(self.alice, 1200, operation_key="create")
        body = payload(pid)
        with self.assertRaises(DurableSandboxError):
            service.accept(self.alice, pid, body, sign(body), operation_key="create")
        for limit in (0, 101, True, "1", None):
            with self.assertRaises(DurableSandboxError):
                service.list(self.alice, limit=limit)
        self.assertEqual(service.get(self.alice, pid)["stage"], "sandbox_awaiting")

    def test_stale_copies_can_diverge_despite_local_durable_idempotency(self):
        from ai_earn.durable_sandbox_payments import DurableSandboxPayments
        from ai_earn.state_recovery_comparison import StateRecoveryComparison
        service = self.upgrade()
        pid = service.create(self.alice, 1200, operation_key="create")
        backup = self.directory / "stale.sqlite3"
        self.state.backup_to(backup, confirmed=True)
        first = payload(pid, "evt1", "confirmation")
        service.accept(self.alice, pid, first, sign(first), operation_key="event")
        stale = StateStorage(backup)
        copy = DurableSandboxPayments(self.auth, stale, KEY)
        failure = payload(pid, "evt1", "failure")
        copy.accept(self.alice, pid, failure, sign(failure), operation_key="event")
        self.assertEqual(copy.get(self.alice, pid)["stage"], "sandbox_failed")
        self.assertEqual(service.get(self.alice, pid)["stage"], "sandbox_confirmed")
        self.assertGreater(StateRecoveryComparison(self.auth, self.state, stale).compare(self.alice)["conflicts"], 0)

    def test_v14_migration_process_crash_preserves_preupgrade_backup(self):
        code = r"""
from pathlib import Path
import os, sys
import ai_earn.state_storage as module
s = module.StateStorage(Path(sys.argv[1]))
original = module._apply_v14
def crash(conn):
    original(conn)
    os._exit(73)
module._apply_v14 = crash
s.migrate_to_v14(Path(sys.argv[2]), confirmed=True)
"""
        backup = self.directory / "crash-before-v14.sqlite3"
        result = subprocess.run([sys.executable, "-c", code, str(self.state.path), str(backup)], capture_output=True, timeout=30)
        self.assertEqual(result.returncode, 73)
        for path in (self.state.path, backup):
            with StateStorage(path).transaction() as conn:
                self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 13)
        self.state.migrate_to_v14(self.directory / "retry-before-v14.sqlite3", confirmed=True)

    def test_migration_refuses_reserved_result_and_does_not_overwrite_backup(self):
        with self.state.transaction() as conn:
            conn.execute("INSERT INTO state_operations VALUES (?,?,?,?,?)", ("a"*32, "reserved", b"x"*32, "sandbox_payment", "b"*32))
        backup = self.directory / "reserved.sqlite3"
        with self.assertRaises(StateStorageError):
            self.state.migrate_to_v14(backup, confirmed=True)
        with self.state.transaction() as conn:
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 13)
        self.assertTrue(backup.exists())
        with self.assertRaises(StateStorageError):
            self.state.migrate_to_v14(backup, confirmed=True)

    def test_creation_result_swap_and_test_key_fingerprint_changes_are_detected(self):
        service = self.upgrade()
        first = service.create(self.alice, 1200, operation_key="first")
        second = service.create(self.alice, 1200, operation_key="second")
        with self.state.transaction() as conn:
            conn.execute("UPDATE state_operations SET resource_id=? WHERE operation_key='first' AND resource_type='sandbox_payment' AND resource_id=?", (second, first))
        with self.assertRaises(StateStorageError):
            service.create(self.alice, 1200, operation_key="first")
        with self.assertRaises(StateStorageError):
            self.state.backup_to(self.directory / "swapped.sqlite3", confirmed=True)
        with self.state.transaction() as conn:
            conn.execute("UPDATE state_operations SET resource_id=? WHERE operation_key='first'", (first,))
            conn.execute("UPDATE sandbox_payments SET key_fingerprint=? WHERE id=?", (b"x"*32, first))
        with self.assertRaises(StateStorageError):
            service.get(self.alice, first)
        with self.assertRaises(StateStorageError):
            self.state.backup_to(self.directory / "fingerprint.sqlite3", confirmed=True)

    def test_backups_refuse_event_history_tampering(self):
        service = self.upgrade()
        pid = service.create(self.alice, 1200, operation_key="create")
        body = payload(pid)
        service.accept(self.alice, pid, body, sign(body), operation_key="event")
        with self.state.transaction() as conn:
            conn.execute("DELETE FROM sandbox_payment_events WHERE payment_id=?", (pid,))
        with self.assertRaises(StateStorageError):
            service.get(self.alice, pid)
        with self.assertRaises(StateStorageError):
            self.state.backup_to(self.directory / "bad.sqlite3", confirmed=True)

"""Abrupt-process mutation recovery with temporary, synthetic data only."""
from contextlib import closing
import json
import os
import queue
import threading
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from ai_earn.auth import AccessDenied, AuthService
from ai_earn.auth_storage import AuthStorage
from ai_earn.invoice_drafts import InvoiceDrafts
from ai_earn.order_drafts import OrderDrafts
from ai_earn.payment_observations import PaymentObservations
from ai_earn.state_storage import StateStorage
from ai_earn.task_queue import TaskQueue
from ai_earn.task_workflows import TaskWorkflows


# The token is a disposable test token passed to the child on stdin, not argv.
_CHILD = r'''
from contextlib import contextmanager
import json, os, sys
from pathlib import Path
from unittest.mock import patch
from ai_earn.auth import AuthService
from ai_earn.auth_storage import AuthStorage
from ai_earn.state_storage import StateStorage
from ai_earn.task_queue import TaskQueue
from ai_earn.task_workflows import TaskWorkflows
from ai_earn.payment_observations import PaymentObservations

p = json.loads(sys.stdin.read())
auth = AuthService(AuthStorage(Path(p["auth"]), create=False), lambda: 1800000000)
state = StateStorage(Path(p["state"]))
if p["kind"] == "queue":
    import ai_earn.task_queue as module
    def operation():
        TaskQueue(auth, state).claim(p["token"], p["id"], operation_key="crash-key", lease_seconds=60)
elif p["kind"] == "review":
    import ai_earn.task_workflows as module
    def operation():
        TaskWorkflows(auth, state).review(p["token"], p["id"], "synthetic result", approved=True, confirmed=True, operation_key="crash-key")
else:
    import ai_earn.payment_observations as module
    def operation():
        PaymentObservations(auth, state).create(p["token"], p["id"], operation_key="crash-key")

if p["point"] == "before":
    original = module._remember
    def crash(*args):
        original(*args)
        os._exit(71)
    with patch.object(module, "_remember", side_effect=crash):
        operation()
else:
    original = auth.storage.transaction
    @contextmanager
    def crash_after_state_commit():
        with original() as conn:
            yield conn
            os._exit(72)
    with patch.object(auth.storage, "transaction", crash_after_state_commit):
        operation()
os._exit(99)
'''


# Two separate processes share only disposable files and a small stdin barrier.
_COMPETE = r'''
import json, sys
from pathlib import Path
from unittest.mock import patch
from ai_earn.auth import AuthService
from ai_earn.auth_storage import AuthStorage
from ai_earn.state_storage import StateStorage
from ai_earn.task_queue import TaskQueue, QueueError
import ai_earn.task_queue as module

p = json.loads(sys.stdin.readline())
auth = AuthService(AuthStorage(Path(p["auth"]), create=False), lambda: 1800000000)
queue = TaskQueue(auth, StateStorage(Path(p["state"])))
if p["role"] == "leader":
    original = module._audit
    def hold(*args):
        original(*args)
        print("LOCKED", flush=True)
        if sys.stdin.readline().strip() != "release":
            raise RuntimeError("Barrier not released")
    with patch.object(module, "_audit", side_effect=hold):
        lease = queue.claim(p["token"], p["id"], operation_key=p["key"], lease_seconds=60)
    print("OK:" + lease, flush=True)
else:
    print("READY", flush=True)
    if sys.stdin.readline().strip() != "start":
        raise RuntimeError("Barrier not started")
    print("ATTEMPT", flush=True)
    try:
        lease = queue.claim(p["token"], p["id"], operation_key=p["key"], lease_seconds=60)
        print("OK:" + lease, flush=True)
    except QueueError:
        print("DENIED", flush=True)
'''


class Clock:
    now = 1_800_000_000
    def __call__(self):
        return self.now


@unittest.skipUnless(os.name in {"posix", "nt"}, "requires a supported private filesystem")
class StateCrashRecoveryTests(unittest.TestCase):
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
        self.auth.register("alice", "disposable test password")
        self.token = self.auth.login("alice", "disposable test password")
        self.state = StateStorage(self.directory / "state.sqlite3", create=True, confirmed=True)
        for version in range(2, 10):
            getattr(self.state, f"migrate_to_v{version}")(self.directory / f"v{version-1}.sqlite3", confirmed=True)
        self.queue = TaskQueue(self.auth, self.state)
        self.tasks = TaskWorkflows(self.auth, self.state)
        self.observations = PaymentObservations(self.auth, self.state)
        orders = OrderDrafts(self.auth, self.state)
        invoices = InvoiceDrafts(self.auth, self.state)
        order = orders.create(self.token, "Synthetic estimate", 100, operation_key="order")
        self.invoice = invoices.create(self.token, order, operation_key="invoice")

    def _crash(self, kind, point, ident):
        payload = json.dumps({"auth": str(self.auth.storage.path), "state": str(self.state.path),
                              "token": self.token, "id": ident, "kind": kind, "point": point})
        result = subprocess.run([sys.executable, "-c", _CHILD], input=payload, text=True,
                                capture_output=True, timeout=25)
        self.assertEqual(result.returncode, 71 if point == "before" else 72,
                         "Child did not reach the intended crash point.")

    def _operation_rows(self):
        with self.state.transaction() as conn:
            return conn.execute("SELECT resource_type, resource_id FROM state_operations WHERE operation_key='crash-key'").fetchall()

    def test_queue_claim_process_crash_before_commit_rolls_back(self):
        ident = self.queue.enqueue(self.token, "Synthetic queue item", operation_key="queue")
        self._crash("queue", "before", ident)
        self.assertEqual(self.queue.get(self.token, ident)["status"], "queued")
        self.assertEqual(self._operation_rows(), [])
        with self.state.transaction() as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM queue_leases WHERE queue_id=?", (ident,)).fetchone()[0], 0)
            self.assertEqual(conn.execute("SELECT count(*) FROM state_audit WHERE event='queue_leased' AND resource_id=?", (ident,)).fetchone()[0], 0)
        reopened = TaskQueue(self.auth, StateStorage(self.state.path))
        lease = reopened.claim(self.token, ident, operation_key="crash-key", lease_seconds=60)
        self.assertTrue(reopened.get_lease(self.token, lease)["active"])

    def test_queue_claim_process_crash_after_commit_retries_once(self):
        ident = self.queue.enqueue(self.token, "Synthetic queue item", operation_key="queue")
        self._crash("queue", "after", ident)
        rows = self._operation_rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][0], "queue_lease")
        lease = self.queue.claim(self.token, ident, operation_key="crash-key", lease_seconds=60)
        self.assertEqual(lease, rows[0][1])
        self.assertTrue(self.queue.get_lease(self.token, lease)["active"])
        with self.state.transaction() as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM queue_leases WHERE queue_id=?", (ident,)).fetchone()[0], 1)
            self.assertEqual(conn.execute("SELECT count(*) FROM state_audit WHERE event='queue_leased' AND resource_id=?", (ident,)).fetchone()[0], 1)

    def _pending_review(self):
        ident = self.tasks.create(self.token, "Synthetic task", operation_key="workflow")
        self.tasks.start(self.token, ident, operation_key="start")
        self.tasks.submit(self.token, ident, "synthetic result", operation_key="submit")
        return ident

    def test_review_process_crash_before_commit_rolls_back(self):
        ident = self._pending_review()
        self._crash("review", "before", ident)
        self.assertEqual(self.tasks.get(self.token, ident)["status"], "pending_review")
        self.assertEqual(self._operation_rows(), [])
        with self.state.transaction() as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM state_audit WHERE event='workflow_approved' AND resource_id=?", (ident,)).fetchone()[0], 0)
        reopened = TaskWorkflows(self.auth, StateStorage(self.state.path))
        reopened.review(self.token, ident, "synthetic result", approved=True, confirmed=True, operation_key="crash-key")
        self.assertEqual(reopened.get(self.token, ident)["status"], "completed")

    def test_review_process_crash_after_commit_retries_once(self):
        ident = self._pending_review()
        self._crash("review", "after", ident)
        self.assertEqual(self.tasks.get(self.token, ident)["status"], "completed")
        self.assertEqual(self.tasks.review(self.token, ident, "synthetic result", approved=True, confirmed=True, operation_key="crash-key"), ident)
        with self.state.transaction() as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM state_audit WHERE event='workflow_approved' AND resource_id=?", (ident,)).fetchone()[0], 1)
            self.assertEqual(conn.execute("SELECT count(*) FROM state_operations WHERE operation_key='crash-key'").fetchone()[0], 1)

    def test_placeholder_process_crash_before_commit_rolls_back(self):
        self._crash("payment", "before", self.invoice)
        self.assertEqual(self._operation_rows(), [])
        with self.state.transaction() as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM payment_observations").fetchone()[0], 0)
            self.assertEqual(conn.execute("SELECT count(*) FROM state_audit WHERE resource_type='payment_observation'").fetchone()[0], 0)
        reopened = PaymentObservations(self.auth, StateStorage(self.state.path))
        ident = reopened.create(self.token, self.invoice, operation_key="crash-key")
        self.assertEqual(reopened.get(self.token, ident)["status"], "unverified")

    def test_placeholder_process_crash_after_commit_retries_once(self):
        self._crash("payment", "after", self.invoice)
        rows = self._operation_rows()
        self.assertEqual(len(rows), 1)
        ident = self.observations.create(self.token, self.invoice, operation_key="crash-key")
        self.assertEqual(ident, rows[0][1])
        self.assertEqual(self.observations.get(self.token, ident)["status"], "unverified")
        with self.state.transaction() as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM payment_observations").fetchone()[0], 1)
            self.assertEqual(conn.execute("SELECT count(*) FROM state_audit WHERE resource_type='payment_observation'").fetchone()[0], 1)

    def test_restart_requires_live_session_before_ambiguous_retry(self):
        self._crash("payment", "after", self.invoice)
        rows = self._operation_rows()
        self.assertEqual(len(rows), 1)
        self.auth.logout(self.token)
        restarted_auth = AuthService(AuthStorage(self.auth.storage.path, create=False), self.clock)
        restarted = PaymentObservations(restarted_auth, StateStorage(self.state.path))
        with self.assertRaises(AccessDenied):
            restarted.create(self.token, self.invoice, operation_key="crash-key")
        new_token = restarted_auth.login("alice", "disposable test password")
        ident = restarted.create(new_token, self.invoice, operation_key="crash-key")
        self.assertEqual(ident, rows[0][1])
        self.assertEqual(restarted.get(new_token, ident)["status"], "unverified")
        with self.state.transaction() as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM payment_observations").fetchone()[0], 1)

    def _readline(self, process):
        lines = queue.Queue()
        thread = threading.Thread(target=lambda: lines.put(process.stdout.readline()), daemon=True)
        thread.start()
        try:
            return lines.get(timeout=20).strip()
        except queue.Empty as exc:
            raise AssertionError("Child did not reach the expected barrier.") from exc

    def _competing_claims(self, second_key):
        ident = self.queue.enqueue(self.token, "Synthetic queue item", operation_key="queue")
        common = {"auth": str(self.auth.storage.path), "state": str(self.state.path),
                  "token": self.token, "id": ident}
        leader = follower = None
        try:
            follower = subprocess.Popen([sys.executable, "-c", _COMPETE], stdin=subprocess.PIPE,
                                        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            follower.stdin.write(json.dumps(dict(common, role="follower", key=second_key)) + "\n")
            follower.stdin.flush()
            self.assertEqual(self._readline(follower), "READY")
            leader = subprocess.Popen([sys.executable, "-c", _COMPETE], stdin=subprocess.PIPE,
                                      stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            leader.stdin.write(json.dumps(dict(common, role="leader", key="first")) + "\n")
            leader.stdin.flush()
            self.assertEqual(self._readline(leader), "LOCKED")
            follower.stdin.write("start\n")
            follower.stdin.flush()
            self.assertEqual(self._readline(follower), "ATTEMPT")
            leader.stdin.write("release\n")
            leader.stdin.close()
            leader.stdin = None
            follower.stdin.close()
            follower.stdin = None
            leader_out, _ = leader.communicate(timeout=25)
            follower_out, _ = follower.communicate(timeout=25)
            self.assertEqual(leader.returncode, 0, "Leader process failed.")
            self.assertEqual(follower.returncode, 0, "Follower process failed.")
            self.assertTrue(leader_out.strip().startswith("OK:"))
            first = leader_out.strip()[3:]
            self.assertTrue(self.queue.get_lease(self.token, first)["active"])
            if second_key == "first":
                self.assertEqual(follower_out.strip(), "OK:" + first)
            else:
                self.assertEqual(follower_out.strip(), "DENIED")
            with self.state.transaction() as conn:
                self.assertEqual(conn.execute("SELECT count(*) FROM queue_leases WHERE queue_id=?", (ident,)).fetchone()[0], 1)
                self.assertEqual(conn.execute("SELECT count(*) FROM state_audit WHERE event='queue_leased' AND resource_id=?", (ident,)).fetchone()[0], 1)
        finally:
            for process in (leader, follower):
                if process is not None:
                    if process.poll() is None:
                        process.kill()
                    process.communicate(timeout=10)

    def test_separate_processes_same_key_share_one_claim(self):
        self._competing_claims("first")

    def test_separate_processes_distinct_keys_claim_only_once(self):
        self._competing_claims("second")

    def test_wal_process_crash_rolls_back_then_recovers(self):
        ident = self.queue.enqueue(self.token, "Synthetic queue item", operation_key="queue")
        with closing(sqlite3.connect(self.state.path)) as wal:
            self.assertEqual(wal.execute("PRAGMA journal_mode=WAL").fetchone()[0].lower(), "wal")
            self._crash("queue", "before", ident)
            self.assertEqual(self.queue.get(self.token, ident)["status"], "queued")
            self.assertEqual(self._operation_rows(), [])
            lease = self.queue.claim(self.token, ident, operation_key="crash-key", lease_seconds=60)
            self.assertTrue(self.queue.get_lease(self.token, lease)["active"])
            backup = self.directory / "recovered-wal.sqlite3"
            self.state.backup_to(backup, confirmed=True)
            self.assertTrue(TaskQueue(self.auth, StateStorage(backup)).get_lease(self.token, lease)["active"])

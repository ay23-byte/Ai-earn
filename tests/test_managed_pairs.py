"""Disposable v12 managed pairs; not external execution or a security boundary."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import json
import os
import queue as thread_queue
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

from ai_earn.auth import AccessDenied, AuthService, SESSION_SECONDS
from ai_earn.auth_storage import AuthStorage
from ai_earn.state_storage import StateStorage, StateStorageError
from ai_earn.state_recovery_comparison import StateRecoveryComparison
from ai_earn.task_associations import AssociationError, TaskAssociations
from ai_earn.task_queue import QueueError, TaskQueue
from ai_earn.task_workflows import TaskWorkflows, WorkflowError


class Clock:
    now = 1_800_000_000
    def __call__(self):
        return self.now


@unittest.skipUnless(os.name in {"posix", "nt"}, "requires a supported private filesystem")
class ManagedPairTests(unittest.TestCase):
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
        for version in range(2, 12):
            getattr(self.state, f"migrate_to_v{version}")(self.directory / f"before-v{version}.sqlite3", confirmed=True)
        self.queue = TaskQueue(self.auth, self.state)
        self.workflows = TaskWorkflows(self.auth, self.state)
        self.associations = TaskAssociations(self.auth, self.state)
        self.item = self.queue.enqueue(self.alice, "Synthetic", operation_key="queue", backoff_seconds=0)
        self.workflow = self.workflows.create(self.alice, "Synthetic", operation_key="workflow")

    def upgrade(self):
        self.state.migrate_to_v12(self.directory / "before-v12.sqlite3", confirmed=True)

    def managed(self):
        self.upgrade()
        return self.associations.create_managed(self.alice, self.item, self.workflow, operation_key="managed")

    def test_managed_discovery_scopes_pages_and_does_not_mutate(self):
        first = self.managed()
        q = self.queue.enqueue(self.alice, "Higher priority", operation_key="high-q", priority=9)
        w = self.workflows.create(self.alice, "Higher priority", operation_key="high-w")
        second = self.associations.create_managed(self.alice, q, w, operation_key="high-pair")
        bob_q = self.queue.enqueue(self.bob, "Private owner", operation_key="bob-q")
        bob_w = self.workflows.create(self.bob, "Private owner", operation_key="bob-w")
        bob_pair = self.associations.create_managed(self.bob, bob_q, bob_w, operation_key="bob-pair")
        # Legacy pairs must not enter a managed-only listing.
        legacy_q = self.queue.enqueue(self.alice, "legacy", operation_key="legacy-q")
        legacy_w = self.workflows.create(self.alice, "legacy", operation_key="legacy-w")
        legacy = self.associations.create(self.alice, legacy_q, legacy_w, operation_key="legacy")
        with self.state.transaction() as conn:
            counts = tuple(conn.execute("SELECT count(*) FROM " + table).fetchone()[0] for table in ("state_audit", "state_operations"))
        page = self.associations.list_managed(self.alice, limit=1)
        next_page = self.associations.list_managed(self.alice, after_id=page[-1]["association_id"], limit=1)
        ids = [page[0]["association_id"], next_page[0]["association_id"]]
        self.assertEqual(ids, sorted((first, second)))
        self.assertEqual(self.associations.list_managed(self.alice, after_id=ids[-1], limit=1), [])
        self.assertEqual([v["association_id"] for v in self.associations.list_managed(self.bob)], [bob_pair])
        self.assertNotIn(legacy, ids)
        self.assertNotIn("Synthetic", str(page + next_page))
        self.assertNotIn("output_hash", str(page + next_page))
        self.assertEqual([v["association_id"] for v in self.associations.ready_managed(self.alice)], [second, first])
        self.assertEqual([v["association_id"] for v in self.associations.ready_managed(self.alice, limit=1)], [second])
        self.assertEqual([v["association_id"] for v in self.associations.ready_managed(self.bob)], [bob_pair])
        with self.state.transaction() as conn:
            self.assertEqual(counts, tuple(conn.execute("SELECT count(*) FROM " + table).fetchone()[0] for table in ("state_audit", "state_operations")))

    def test_managed_discovery_handles_expiry_recovery_completion_and_restart(self):
        pair = self.managed()
        initial = self.associations.list_managed(self.alice)[0]
        self.assertEqual(initial, self.associations.inspect(self.alice, pair))
        self.assertTrue(initial["queue_ready_at_observation"])
        lease = self.associations.start_pair(self.alice, pair, operation_key="start", lease_seconds=5)
        self.assertEqual(self.associations.ready_managed(self.alice), [])
        self.assertTrue(self.associations.list_managed(self.alice)[0]["lease_active_at_observation"])
        self.associations.submit_pair(self.alice, pair, lease, "output", operation_key="submit")
        self.clock.now += 5
        self.assertFalse(self.associations.list_managed(self.alice)[0]["lease_active_at_observation"])
        self.associations.recover_pair(self.alice, pair, lease, operation_key="recover")
        ready = self.associations.ready_managed(self.alice)[0]
        self.assertEqual((ready["association_id"], ready["workflow_status"]), (pair, "pending_review"))
        lease2 = self.associations.resume_pair(self.alice, pair, operation_key="resume", lease_seconds=15)
        self.associations.complete_pair(self.alice, pair, lease2, "output", confirmed=True, operation_key="complete")
        completed = self.associations.list_managed(self.alice)[0]
        self.assertEqual((completed["queue_status"], completed["workflow_status"]), ("completed", "completed"))
        self.assertFalse(completed["lease_active_at_observation"])
        self.assertEqual(self.associations.ready_managed(self.alice), [])
        backup = self.directory / "discover-backup.sqlite3"
        restore = self.directory / "discover-restore.sqlite3"
        self.state.backup_to(backup, confirmed=True)
        StateStorage(backup).backup_to(restore, confirmed=True)
        restored = TaskAssociations(self.auth, StateStorage(restore))
        self.assertEqual(restored.list_managed(self.alice), [completed])
        self.assertEqual(restored.ready_managed(self.alice), [])

    def test_managed_discovery_validates_input_auth_and_version(self):
        for name in ("list_managed", "ready_managed"):
            with self.assertRaises(StateStorageError):
                getattr(self.associations, name)(self.alice)
        self.upgrade()
        for name in ("list_managed", "ready_managed"):
            for bad in (0, 101, -1, True, "1", None):
                with self.assertRaises(AssociationError):
                    getattr(self.associations, name)(self.alice, limit=bad)
            with self.assertRaises(AccessDenied):
                getattr(self.associations, name)("")
        for bad in ("", "z"*32, "0"*31, 0, True):
            with self.assertRaises(AssociationError):
                self.associations.list_managed(self.alice, after_id=bad)
        self.assertEqual(self.associations.list_managed(self.bob), [])

    def test_managed_discovery_deadlines_and_cancelled_records(self):
        self.upgrade()
        q = self.queue.enqueue(self.alice, "deadline", operation_key="deadline-q", deadline_at=self.clock.now + 5)
        w = self.workflows.create(self.alice, "deadline", operation_key="deadline-w")
        pair = self.associations.create_managed(self.alice, q, w, operation_key="deadline-pair")
        self.assertEqual([v["association_id"] for v in self.associations.ready_managed(self.alice)], [pair])
        self.clock.now += 5
        self.assertEqual(self.associations.ready_managed(self.alice), [])
        observation = self.associations.list_managed(self.alice)[0]
        self.assertTrue(observation["deadline_passed_at_observation"])
        self.associations.abandon_pair(self.alice, pair, confirmed=True, operation_key="abandon")
        observation = self.associations.list_managed(self.alice)[0]
        self.assertEqual((observation["queue_status"], observation["workflow_status"]), ("cancelled", "cancelled"))

    def test_managed_discovery_fails_closed_on_missing_marker_and_other_owner_isolated(self):
        pair = self.managed()
        q = self.queue.enqueue(self.bob, "bob", operation_key="bob-q")
        w = self.workflows.create(self.bob, "bob", operation_key="bob-w")
        bob_pair = self.associations.create_managed(self.bob, q, w, operation_key="bob-pair")
        with self.state.transaction() as conn:
            conn.execute("DELETE FROM managed_task_pairs WHERE id=?", (pair,))
        for name in ("list_managed", "ready_managed"):
            with self.assertRaises(StateStorageError):
                getattr(self.associations, name)(self.alice)
            self.assertEqual([v["association_id"] for v in getattr(self.associations, name)(self.bob)], [bob_pair])

    def test_managed_discovery_rechecks_session_before_return(self):
        self.managed()
        import ai_earn.task_associations as module
        original = module._status_view
        def expire(*args):
            result = original(*args)
            self.clock.now += SESSION_SECONDS
            return result
        with patch.object(module, "_status_view", side_effect=expire):
            with self.assertRaises(AccessDenied):
                self.associations.list_managed(self.alice)
        # No write was performed despite an invalidated observation.
        with self.state.transaction() as conn:
            self.assertEqual(conn.execute("SELECT status FROM task_queue WHERE id=?", (self.item,)).fetchone()[0], "queued")

    def test_managed_ready_observation_is_stale_after_start(self):
        pair = self.managed()
        before = self.associations.ready_managed(self.alice)
        self.assertEqual([item["association_id"] for item in before], [pair])
        lease = self.associations.start_pair(self.alice, pair, operation_key="start", lease_seconds=30)
        self.assertTrue(before[0]["queue_ready_at_observation"])
        self.assertEqual(self.associations.ready_managed(self.alice), [])
        self.assertTrue(self.queue.get_lease(self.alice, lease)["active"])

    def test_managed_ready_respects_future_availability(self):
        self.upgrade()
        q = self.queue.enqueue(self.alice, "later", operation_key="later-q", available_at=self.clock.now + 10)
        w = self.workflows.create(self.alice, "later", operation_key="later-w")
        pair = self.associations.create_managed(self.alice, q, w, operation_key="later-pair")
        self.assertEqual(self.associations.ready_managed(self.alice), [])
        self.assertFalse(self.associations.list_managed(self.alice)[0]["queue_ready_at_observation"])
        self.clock.now += 10
        self.assertEqual([item["association_id"] for item in self.associations.ready_managed(self.alice)], [pair])

    def test_managed_discovery_fails_if_owned_corruption_is_outside_page(self):
        first = self.managed()
        q = self.queue.enqueue(self.alice, "another", operation_key="another-q")
        w = self.workflows.create(self.alice, "another", operation_key="another-w")
        second = self.associations.create_managed(self.alice, q, w, operation_key="another-pair")
        corrupted = max(first, second)
        with self.state.transaction() as conn:
            conn.execute("DELETE FROM managed_task_pairs WHERE id=?", (corrupted,))
        for method in (self.associations.list_managed, self.associations.ready_managed):
            with self.assertRaises(StateStorageError):
                method(self.alice, limit=1)

    def test_managed_discovery_reopens_in_fresh_process(self):
        pair = self.managed()
        backup = self.directory / "discovery-process.sqlite3"
        self.state.backup_to(backup, confirmed=True)
        code = r"""
import json, sys
from pathlib import Path
from ai_earn.auth import AuthService
from ai_earn.auth_storage import AuthStorage
from ai_earn.state_storage import StateStorage
from ai_earn.task_associations import TaskAssociations
p = json.load(sys.stdin)
auth = AuthService(AuthStorage(Path(p['auth']), create=False), lambda: 1800000000)
pairs = TaskAssociations(auth, StateStorage(Path(p['state'])))
assert [v['association_id'] for v in pairs.list_managed(p['token'])] == [p['pair']]
assert [v['association_id'] for v in pairs.ready_managed(p['token'])] == [p['pair']]
"""
        payload = dict(auth=str(self.auth.storage.path), state=str(backup), token=self.alice, pair=pair)
        result = subprocess.run([sys.executable, "-c", code], input=json.dumps(payload), text=True, capture_output=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_ready_managed_rechecks_session_before_return(self):
        self.managed()
        import ai_earn.task_associations as module
        original = module._status_view
        def expire(*args):
            result = original(*args)
            self.clock.now += SESSION_SECONDS
            return result
        with patch.object(module, "_status_view", side_effect=expire):
            with self.assertRaises(AccessDenied):
                self.associations.ready_managed(self.alice)

    def test_atomic_claim_next_uses_priority_and_retries_historical_lease(self):
        first = self.managed()
        q = self.queue.enqueue(self.alice, "high", operation_key="high-q", priority=9, backoff_seconds=0)
        w = self.workflows.create(self.alice, "high", operation_key="high-w")
        high = self.associations.create_managed(self.alice, q, w, operation_key="high-pair")
        result = self.associations.claim_next_managed(self.alice, lease_seconds=5, operation_key="next")
        self.assertEqual(result["association_id"], high)
        self.assertTrue(self.queue.get_lease(self.alice, result["lease_id"])["active"])
        self.assertEqual(self.workflows.get(self.alice, w)["status"], "in_progress")
        self.assertEqual(self.associations.claim_next_managed(self.alice, lease_seconds=5, operation_key="next"), result)
        with self.assertRaises(AssociationError):
            self.associations.claim_next_managed(self.alice, lease_seconds=6, operation_key="next")
        self.assertEqual(self.associations.claim_next_managed(self.alice, lease_seconds=30, operation_key="next2")["association_id"], first)
        self.clock.now += 5
        self.assertEqual(self.associations.claim_next_managed(self.alice, lease_seconds=5, operation_key="next"), result)
        self.assertFalse(self.queue.get_lease(self.alice, result["lease_id"])["active"])
        backup = self.directory / "claim-next.sqlite3"
        self.state.backup_to(backup, confirmed=True)
        restored = TaskAssociations(self.auth, StateStorage(backup))
        self.assertEqual(restored.claim_next_managed(self.alice, lease_seconds=5, operation_key="next"), result)

    def test_atomic_claim_next_resumes_pending_review_and_no_ready_does_not_reserve_key(self):
        pair = self.managed()
        self.assertIsNone(self.associations.claim_next_managed(self.bob, lease_seconds=5, operation_key="later"))
        lease = self.associations.start_pair(self.alice, pair, operation_key="start", lease_seconds=5)
        self.associations.submit_pair(self.alice, pair, lease, "output", operation_key="submit")
        self.assertIsNone(self.associations.claim_next_managed(self.alice, lease_seconds=5, operation_key="resume"))
        self.clock.now += 5
        self.associations.recover_pair(self.alice, pair, lease, operation_key="recover")
        result = self.associations.claim_next_managed(self.alice, lease_seconds=5, operation_key="resume")
        self.assertEqual(result["association_id"], pair)
        self.assertNotEqual(result["lease_id"], lease)
        self.assertEqual(self.workflows.get(self.alice, self.workflow)["status"], "pending_review")
        self.associations.complete_pair(self.alice, pair, result["lease_id"], "output", confirmed=True, operation_key="complete")
        self.assertEqual(self.queue.get(self.alice, self.item)["status"], "completed")
        self.assertEqual(self.associations.claim_next_managed(self.alice, lease_seconds=5, operation_key="resume"), result)
        self.assertFalse(self.queue.get_lease(self.alice, result["lease_id"])["active"])
        bob_q = self.queue.enqueue(self.bob, "bob", operation_key="bob-q")
        bob_w = self.workflows.create(self.bob, "bob", operation_key="bob-w")
        bob_pair = self.associations.create_managed(self.bob, bob_q, bob_w, operation_key="bob-pair")
        self.assertEqual(self.associations.claim_next_managed(self.bob, lease_seconds=5, operation_key="later")["association_id"], bob_pair)
        self.state.backup_to(self.directory / "claim-next-resume.sqlite3", confirmed=True)

    def test_atomic_claim_next_rejects_invalid_input_version_and_conflicting_key(self):
        with self.assertRaises(StateStorageError):
            self.associations.claim_next_managed(self.alice, lease_seconds=5, operation_key="next")
        self.upgrade()
        for bad in (0, 3601, True, "5", None):
            with self.assertRaises(AssociationError):
                self.associations.claim_next_managed(self.alice, lease_seconds=bad, operation_key="next")
        with self.assertRaises(AccessDenied):
            self.associations.claim_next_managed("", lease_seconds=5, operation_key="next")
        with self.assertRaises(AssociationError):
            self.associations.claim_next_managed(self.alice, lease_seconds=5, operation_key="queue")
        with self.state.transaction() as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM queue_leases").fetchone()[0], 0)

    def test_atomic_claim_next_ignores_legacy_other_owner_and_future_deadline(self):
        self.upgrade()
        legacy_q = self.queue.enqueue(self.alice, "legacy", operation_key="legacy-q", priority=9)
        legacy_w = self.workflows.create(self.alice, "legacy", operation_key="legacy-w")
        self.associations.create(self.alice, legacy_q, legacy_w, operation_key="legacy")
        future_q = self.queue.enqueue(self.alice, "future", operation_key="future-q", available_at=self.clock.now+10, deadline_at=self.clock.now+20)
        future_w = self.workflows.create(self.alice, "future", operation_key="future-w")
        future = self.associations.create_managed(self.alice, future_q, future_w, operation_key="future-pair")
        bob_q = self.queue.enqueue(self.bob, "bob", operation_key="bob-q")
        bob_w = self.workflows.create(self.bob, "bob", operation_key="bob-w")
        bob = self.associations.create_managed(self.bob, bob_q, bob_w, operation_key="bob-pair")
        self.assertIsNone(self.associations.claim_next_managed(self.alice, lease_seconds=30, operation_key="next"))
        self.assertEqual(self.associations.claim_next_managed(self.bob, lease_seconds=30, operation_key="next")["association_id"], bob)
        self.clock.now += 10
        result = self.associations.claim_next_managed(self.alice, lease_seconds=30, operation_key="next")
        self.assertEqual(result["association_id"], future)
        self.assertEqual(self.queue.get_lease(self.alice, result["lease_id"])["lease_until"], self.clock.now+10)
        self.assertEqual(self.queue.get(self.alice, legacy_q)["status"], "queued")

    def test_atomic_claim_next_concurrent_keys_and_same_key(self):
        pair = self.managed()
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: self.associations.claim_next_managed(self.alice, lease_seconds=30, operation_key="next"), range(2)))
        self.assertEqual(results[0], results[1])
        self.assertEqual(results[0]["association_id"], pair)
        self.assertIsNone(self.associations.claim_next_managed(self.alice, lease_seconds=30, operation_key="new"))
        with self.state.transaction() as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM queue_leases WHERE queue_id=?", (self.item,)).fetchone()[0], 1)
            self.assertEqual(conn.execute("SELECT count(*) FROM state_audit WHERE event='queue_leased' AND resource_id=?", (self.item,)).fetchone()[0], 1)
        self.state.backup_to(self.directory / "claim-next-concurrent.sqlite3", confirmed=True)

    def test_atomic_claim_next_failure_expiry_and_internal_collision_rollback(self):
        pair = self.managed()
        with patch("ai_earn.task_associations._pair_child_key", return_value="queue"):
            with self.assertRaises(StateStorageError):
                self.associations.claim_next_managed(self.alice, lease_seconds=5, operation_key="next")
        with patch("ai_earn.task_associations._workflow_audit", side_effect=RuntimeError("injected")):
            with self.assertRaises(RuntimeError):
                self.associations.claim_next_managed(self.alice, lease_seconds=5, operation_key="next")
        with patch("ai_earn.task_associations._audit", side_effect=RuntimeError("injected binding audit")):
            with self.assertRaises(RuntimeError):
                self.associations.claim_next_managed(self.alice, lease_seconds=5, operation_key="next")
        import ai_earn.task_associations as module
        original = module._workflow_audit
        def expire(*args):
            original(*args)
            self.clock.now += 5
        with patch.object(module, "_workflow_audit", side_effect=expire):
            with self.assertRaises(AssociationError):
                self.associations.claim_next_managed(self.alice, lease_seconds=5, operation_key="next")
        self.assertEqual(self.associations.inspect(self.alice, pair)["queue_status"], "queued")
        self.assertEqual(self.workflows.get(self.alice, self.workflow)["status"], "planned")
        with self.state.transaction() as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM queue_leases").fetchone()[0], 0)
            self.assertEqual(conn.execute("SELECT count(*) FROM state_operations WHERE operation_key='next'").fetchone()[0], 0)

    def test_atomic_claim_next_detects_owned_marker_corruption(self):
        pair = self.managed()
        with self.state.transaction() as conn:
            conn.execute("DELETE FROM managed_task_pairs WHERE id=?", (pair,))
        with self.assertRaises(StateStorageError):
            self.associations.claim_next_managed(self.alice, lease_seconds=5, operation_key="next")
        self.assertIsNone(self.associations.claim_next_managed(self.bob, lease_seconds=5, operation_key="next"))
        with self.state.transaction() as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM queue_leases").fetchone()[0], 0)

    def test_atomic_claim_next_distinct_keys_compete_without_double_lease(self):
        pair = self.managed()
        with ThreadPoolExecutor(max_workers=2) as pool:
            calls = [pool.submit(self.associations.claim_next_managed, self.alice, lease_seconds=30, operation_key=key) for key in ("a", "b")]
            results = [call.result(timeout=15) for call in calls]
        self.assertEqual(sum(result is None for result in results), 1)
        self.assertEqual(next(result for result in results if result is not None)["association_id"], pair)
        with self.state.transaction() as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM queue_leases WHERE queue_id=?", (self.item,)).fetchone()[0], 1)
        self.state.backup_to(self.directory / "different-key-race.sqlite3", confirmed=True)

    def test_atomic_claim_next_separate_process_competition(self):
        self.upgrade()
        code = r"""
import json, sys
from pathlib import Path
from ai_earn.auth import AuthService
from ai_earn.auth_storage import AuthStorage
from ai_earn.state_storage import StateStorage
from ai_earn.task_associations import TaskAssociations
p = json.loads(sys.stdin.readline())
auth = AuthService(AuthStorage(Path(p['auth']), create=False), lambda: 1800000000)
service = TaskAssociations(auth, StateStorage(Path(p['state'])))
print('READY', flush=True)
sys.stdin.readline()
result = service.claim_next_managed(p['token'], lease_seconds=30, operation_key=p['key'])
print(json.dumps(result), flush=True)
"""
        def readline(process):
            lines = thread_queue.Queue()
            threading.Thread(target=lambda: lines.put(process.stdout.readline()), daemon=True).start()
            try:
                return lines.get(timeout=20).strip()
            except thread_queue.Empty as exc:
                raise AssertionError("Child did not reach barrier.") from exc
        for same in (True, False):
            suffix = "same" if same else "different"
            q = self.queue.enqueue(self.alice, "synthetic", operation_key=suffix + "-q")
            w = self.workflows.create(self.alice, "synthetic", operation_key=suffix + "-w")
            pair = self.associations.create_managed(self.alice, q, w, operation_key=suffix + "-pair")
            processes = []
            try:
                for index in range(2):
                    key = suffix + ("-shared" if same else f"-key-{index}")
                    data = dict(auth=str(self.auth.storage.path), state=str(self.state.path), token=self.alice, key=key)
                    process = subprocess.Popen([sys.executable, "-c", code], stdin=subprocess.PIPE,
                                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                    processes.append(process)
                    process.stdin.write(json.dumps(data) + "\n")
                    process.stdin.flush()
                    self.assertEqual(readline(process), "READY")
                for process in processes:
                    process.stdin.write("go\n")
                    process.stdin.close()
                    process.stdin = None
                outputs = []
                for process in processes:
                    out, _ = process.communicate(timeout=30)
                    self.assertEqual(process.returncode, 0, "Child process failed.")
                    outputs.append(json.loads(out.strip()))
                if same:
                    self.assertEqual(outputs[0], outputs[1])
                    self.assertEqual(outputs[0]["association_id"], pair)
                else:
                    self.assertEqual(sum(item is None for item in outputs), 1)
                    self.assertEqual(next(item for item in outputs if item is not None)["association_id"], pair)
                with self.state.transaction() as conn:
                    self.assertEqual(conn.execute("SELECT count(*) FROM queue_leases WHERE queue_id=?", (q,)).fetchone()[0], 1)
            finally:
                for process in processes:
                    if process.poll() is None:
                        process.kill()
                    process.communicate(timeout=10)
        self.state.backup_to(self.directory / "next-process-race.sqlite3", confirmed=True)

    def test_atomic_claim_next_resumes_in_progress_without_restarting_workflow(self):
        pair = self.managed()
        lease = self.associations.start_pair(self.alice, pair, operation_key="start", lease_seconds=30)
        self.associations.retry_pair(self.alice, pair, lease, operation_key="retry")
        next_lease = self.associations.claim_next_managed(self.alice, lease_seconds=5, operation_key="next")
        self.assertEqual(next_lease["association_id"], pair)
        self.assertEqual(self.workflows.get(self.alice, self.workflow)["status"], "in_progress")
        with self.state.transaction() as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM state_audit WHERE event='workflow_started' AND resource_id=?", (self.workflow,)).fetchone()[0], 1)
        self.state.backup_to(self.directory / "progress-resumed.sqlite3", confirmed=True)

    def test_atomic_claim_next_session_expiry_and_ambiguous_auth_exit(self):
        pair = self.managed()
        import ai_earn.task_associations as module
        original_audit = module._workflow_audit
        def expire(*args):
            original_audit(*args)
            self.clock.now += SESSION_SECONDS
        with patch.object(module, "_workflow_audit", side_effect=expire):
            with self.assertRaises(AccessDenied):
                self.associations.claim_next_managed(self.alice, lease_seconds=3600, operation_key="next")
        with self.state.transaction() as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM queue_leases").fetchone()[0], 0)
        self.clock.now -= SESSION_SECONDS
        original_transaction = self.auth.storage.transaction
        @contextmanager
        def fail_after_commit():
            with original_transaction() as conn:
                yield conn
                raise RuntimeError("injected auth exit")
        with patch.object(self.auth.storage, "transaction", fail_after_commit):
            with self.assertRaises(RuntimeError):
                self.associations.claim_next_managed(self.alice, lease_seconds=30, operation_key="next")
        result = self.associations.claim_next_managed(self.alice, lease_seconds=30, operation_key="next")
        self.assertEqual(result["association_id"], pair)
        self.assertTrue(self.queue.get_lease(self.alice, result["lease_id"])["active"])
        self.state.backup_to(self.directory / "next-ambiguous.sqlite3", confirmed=True)

    def test_atomic_claim_next_rejects_swapped_same_owner_managed_results(self):
        self.managed()
        q = self.queue.enqueue(self.alice, "second", operation_key="second-q", priority=9)
        w = self.workflows.create(self.alice, "second", operation_key="second-w")
        self.associations.create_managed(self.alice, q, w, operation_key="second-pair")
        first = self.associations.claim_next_managed(self.alice, lease_seconds=30, operation_key="first")
        second = self.associations.claim_next_managed(self.alice, lease_seconds=30, operation_key="second")
        with self.state.transaction() as conn:
            conn.execute("UPDATE state_operations SET resource_id=? WHERE operation_key='first'", (second["lease_id"],))
            conn.execute("UPDATE state_operations SET resource_id=? WHERE operation_key='second'", (first["lease_id"],))
        for key in ("first", "second"):
            with self.assertRaises(StateStorageError):
                self.associations.claim_next_managed(self.alice, lease_seconds=30, operation_key=key)

    def test_atomic_claim_next_rejects_tampered_retry_result(self):
        self.managed()
        self.associations.claim_next_managed(self.alice, lease_seconds=30, operation_key="next")
        other = self.queue.enqueue(self.alice, "legacy", operation_key="legacy-q")
        other_lease = self.queue.claim(self.alice, other, operation_key="legacy-claim", lease_seconds=30)
        with self.state.transaction() as conn:
            conn.execute("UPDATE state_operations SET resource_id=? WHERE operation_key='next'", (other_lease,))
        with self.assertRaises(StateStorageError):
            self.associations.claim_next_managed(self.alice, lease_seconds=30, operation_key="next")

    def test_atomic_claim_next_controlled_process_crash_before_and_after_commit(self):
        pair = self.managed()
        code = r"""
from contextlib import contextmanager
from pathlib import Path
import json, os, sys
import ai_earn.task_associations as module
from ai_earn.auth import AuthService
from ai_earn.auth_storage import AuthStorage
from ai_earn.state_storage import StateStorage
p = json.load(sys.stdin)
auth = AuthService(AuthStorage(Path(p['auth']), create=False), lambda: 1800000000)
service = module.TaskAssociations(auth, StateStorage(Path(p['state'])))
if p['point'] == 'before':
    original = module._workflow_audit
    def crash(*args):
        original(*args)
        os._exit(73)
    module._workflow_audit = crash
else:
    original = auth.storage.transaction
    @contextmanager
    def crash_after_state_commit():
        with original() as conn:
            yield conn
            os._exit(74)
    auth.storage.transaction = crash_after_state_commit
service.claim_next_managed(p['token'], lease_seconds=30, operation_key='next')
"""
        def run(point):
            payload = dict(auth=str(self.auth.storage.path), state=str(self.state.path), token=self.alice, point=point)
            return subprocess.run([sys.executable, "-c", code], input=json.dumps(payload), text=True, capture_output=True, timeout=30)
        self.assertEqual(run("before").returncode, 73)
        self.assertEqual(self.queue.get(self.alice, self.item)["status"], "queued")
        self.assertEqual(self.workflows.get(self.alice, self.workflow)["status"], "planned")
        self.assertEqual(run("after").returncode, 74)
        result = self.associations.claim_next_managed(self.alice, lease_seconds=30, operation_key="next")
        self.assertEqual(result["association_id"], pair)
        self.assertTrue(self.queue.get_lease(self.alice, result["lease_id"])["active"])
        self.state.backup_to(self.directory / "claim-next-crash.sqlite3", confirmed=True)

    def test_migration_confirmation_backup_and_legacy_compatibility(self):
        target = self.directory / "before-v12.sqlite3"
        with self.assertRaises(StateStorageError):
            self.state.migrate_to_v12(target)
        self.assertFalse(target.exists())
        # An existing v11 association remains legacy and independently mutable.
        legacy = self.associations.create(self.alice, self.item, self.workflow, operation_key="legacy")
        self.upgrade()
        with StateStorage(target).transaction() as conn:
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 11)
            self.assertIsNone(conn.execute("SELECT 1 FROM sqlite_master WHERE name='managed_task_pairs'").fetchone())
        self.assertEqual(self.associations.create(self.alice, self.item, self.workflow, operation_key="legacy"), legacy)
        self.assertEqual(self.workflows.start(self.alice, self.workflow, operation_key="direct"), self.workflow)
        self.assertEqual(self.queue.claim(self.alice, self.item, operation_key="direct-claim", lease_seconds=10) != "", True)
        self.state.backup_to(self.directory / "legacy-v12.sqlite3", confirmed=True)

    def test_v11_refuses_managed_creation_without_mutation(self):
        with self.assertRaises(StateStorageError):
            self.associations.create_managed(self.alice, self.item, self.workflow, operation_key="managed")
        self.assertEqual(self.queue.get(self.alice, self.item)["status"], "queued")
        self.assertEqual(self.workflows.get(self.alice, self.workflow)["status"], "planned")

    def test_owned_pair_prevents_direct_bypass_and_completes(self):
        pair = self.managed()
        self.assertEqual(self.associations.create_managed(self.alice, self.item, self.workflow, operation_key="managed"), pair)
        for method in (
            lambda: self.queue.claim(self.alice, self.item, operation_key="direct-claim", lease_seconds=30),
            lambda: self.queue.cancel(self.alice, self.item, operation_key="direct-cancel"),
        ):
            with self.assertRaises(QueueError):
                method()
        for method in (
            lambda: self.workflows.start(self.alice, self.workflow, operation_key="direct-start"),
            lambda: self.workflows.cancel(self.alice, self.workflow, operation_key="direct-wcancel"),
            lambda: self.workflows.submit(self.alice, self.workflow, "output", operation_key="direct-submit"),
            lambda: self.workflows.review(self.alice, self.workflow, "output", confirmed=True, approved=True, operation_key="direct-review"),
        ):
            with self.assertRaises(WorkflowError):
                method()
        self.assertEqual(self.queue.ready(self.alice), [])
        self.assertTrue(self.associations.inspect(self.alice, pair)["queue_ready_at_observation"])
        lease = self.associations.start_pair(self.alice, pair, operation_key="start", lease_seconds=30)
        self.associations.submit_pair(self.alice, pair, lease, "output", operation_key="submit")
        with self.assertRaises(AssociationError):
            self.associations.review_pair(self.alice, pair, lease, "output", confirmed=True, approved=True, operation_key="review")
        self.assertEqual(self.workflows.get(self.alice, self.workflow)["status"], "pending_review")
        self.associations.complete_pair(self.alice, pair, lease, "output", confirmed=True, operation_key="complete")
        self.assertEqual(self.queue.get(self.alice, self.item)["status"], "completed")
        self.assertFalse(self.queue.get_lease(self.alice, lease)["active"])
        backup = self.directory / "managed-complete.sqlite3"
        self.state.backup_to(backup, confirmed=True)
        copy = StateStorage(backup)
        self.assertEqual(TaskAssociations(self.auth, copy).complete_pair(self.alice, pair, lease, "output", confirmed=True, operation_key="complete"), pair)
        self.assertEqual(TaskQueue(self.auth, copy).get(self.alice, self.item)["status"], "completed")
        counts = StateRecoveryComparison(self.auth, self.state, copy).compare_records(self.alice)["tables"]
        self.assertEqual(counts["managed_task_pairs"]["matching"], 1)

    def test_retry_recovery_resume_and_abandon_never_strands_pair(self):
        pair = self.managed()
        lease = self.associations.start_pair(self.alice, pair, operation_key="start", lease_seconds=5)
        with self.assertRaises(QueueError):
            self.queue.retry(self.alice, self.item, lease, operation_key="direct-retry")
        self.associations.retry_pair(self.alice, pair, lease, operation_key="retry")
        with self.assertRaises(WorkflowError):
            self.workflows.submit(self.alice, self.workflow, "output", operation_key="bypass-submit")
        self.assertEqual(self.associations.abandon_pair(self.alice, pair, confirmed=True, operation_key="abandon"), pair)
        self.assertEqual(self.queue.get(self.alice, self.item)["status"], "cancelled")
        self.assertEqual(self.workflows.get(self.alice, self.workflow)["status"], "cancelled")
        self.state.backup_to(self.directory / "abandoned.sqlite3", confirmed=True)

    def test_expired_pending_review_can_resume_or_be_abandoned_when_exhausted(self):
        self.upgrade()
        q = self.queue.enqueue(self.alice, "Once", operation_key="once-q", max_attempts=1, backoff_seconds=0)
        w = self.workflows.create(self.alice, "Once", operation_key="once-w")
        pair = self.associations.create_managed(self.alice, q, w, operation_key="once-pair")
        lease = self.associations.start_pair(self.alice, pair, operation_key="start", lease_seconds=5)
        self.associations.submit_pair(self.alice, pair, lease, "output", operation_key="submit")
        self.clock.now += 5
        with self.assertRaises(QueueError):
            self.queue.recover(self.alice, q, lease, operation_key="direct-recover")
        self.associations.recover_pair(self.alice, pair, lease, operation_key="recover")
        self.assertEqual(self.queue.get(self.alice, q)["status"], "exhausted")
        self.assertEqual(self.workflows.get(self.alice, w)["status"], "pending_review")
        self.associations.abandon_pair(self.alice, pair, confirmed=True, operation_key="abandon")
        self.assertEqual(self.queue.get(self.alice, q)["status"], "exhausted")
        self.assertEqual(self.workflows.get(self.alice, w)["status"], "cancelled")
        self.state.backup_to(self.directory / "exhausted.sqlite3", confirmed=True)

    def test_managed_creation_denials_and_conflicting_retries(self):
        self.upgrade()
        for token, q, w in ((self.bob, self.item, self.workflow), (self.alice, "f"*32, self.workflow), ("", self.item, self.workflow)):
            with self.assertRaises(AccessDenied):
                self.associations.create_managed(token, q, w, operation_key="managed")
        pair = self.associations.create_managed(self.alice, self.item, self.workflow, operation_key="managed")
        with self.assertRaises(AssociationError):
            self.associations.create(self.alice, self.item, self.workflow, operation_key="managed")
        with self.assertRaises(AssociationError):
            self.associations.create_managed(self.alice, self.item, self.workflow, operation_key="another")
        self.assertEqual(self.associations.get(self.alice, pair)["id"], pair)

    def test_creation_collision_and_failure_are_atomic(self):
        self.upgrade()
        for name, kwargs in (("_pair_child_key", {"return_value": "queue"}), ("_managed_audit", {"side_effect": RuntimeError("injected")})):
            with patch("ai_earn.task_associations." + name, **kwargs):
                with self.assertRaises((RuntimeError, StateStorageError)):
                    self.associations.create_managed(self.alice, self.item, self.workflow, operation_key="managed")
        with self.state.transaction() as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM managed_task_pairs").fetchone()[0], 0)
            self.assertEqual(conn.execute("SELECT count(*) FROM task_associations").fetchone()[0], 0)
            self.assertEqual(conn.execute("SELECT count(*) FROM state_operations WHERE operation_key='managed'").fetchone()[0], 0)

    def test_managed_transition_failures_roll_back_and_remain_recoverable(self):
        pair = self.managed()
        with patch("ai_earn.task_associations._workflow_audit", side_effect=RuntimeError("injected")):
            with self.assertRaises(RuntimeError):
                self.associations.start_pair(self.alice, pair, operation_key="start", lease_seconds=15)
        self.assertEqual(self.queue.get(self.alice, self.item)["status"], "queued")
        self.assertEqual(self.workflows.get(self.alice, self.workflow)["status"], "planned")
        lease = self.associations.start_pair(self.alice, pair, operation_key="start", lease_seconds=15)
        self.associations.submit_pair(self.alice, pair, lease, "output", operation_key="submit")
        with patch("ai_earn.task_associations._completion_audit", side_effect=RuntimeError("injected")):
            with self.assertRaises(RuntimeError):
                self.associations.complete_pair(self.alice, pair, lease, "output", confirmed=True, operation_key="complete")
        self.assertEqual(self.queue.get(self.alice, self.item)["status"], "leased")
        self.assertEqual(self.workflows.get(self.alice, self.workflow)["status"], "pending_review")
        self.associations.complete_pair(self.alice, pair, lease, "output", confirmed=True, operation_key="complete")
        self.assertEqual(self.queue.get(self.alice, self.item)["status"], "completed")
        self.state.backup_to(self.directory / "after-rollback.sqlite3", confirmed=True)

    def test_competing_calls_and_exact_historical_retries(self):
        self.upgrade()
        with ThreadPoolExecutor(max_workers=2) as pool:
            values = list(pool.map(lambda _: self.associations.create_managed(self.alice, self.item, self.workflow, operation_key="managed"), range(2)))
        self.assertEqual(values[0], values[1])
        # Exact prior creation acknowledgements do not mutate or authorize work.
        self.assertEqual(self.queue.enqueue(self.alice, "Synthetic", operation_key="queue", backoff_seconds=0), self.item)
        self.assertEqual(self.workflows.create(self.alice, "Synthetic", operation_key="workflow"), self.workflow)
        self.assertEqual(self.associations.cancel_pair(self.alice, values[0], operation_key="cancel"), values[0])
        self.assertEqual(self.associations.create_managed(self.alice, self.item, self.workflow, operation_key="managed"), values[0])
        self.state.backup_to(self.directory / "cancelled.sqlite3", confirmed=True)

    def test_session_expiry_rolls_back_and_bad_marker_is_refused(self):
        self.upgrade()
        import ai_earn.task_associations as module
        original = module._managed_audit
        def expire(*args):
            original(*args)
            self.clock.now += SESSION_SECONDS
        with patch.object(module, "_managed_audit", side_effect=expire):
            with self.assertRaises(AccessDenied):
                self.associations.create_managed(self.alice, self.item, self.workflow, operation_key="managed")
        with self.state.transaction() as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM managed_task_pairs").fetchone()[0], 0)
            self.assertEqual(conn.execute("SELECT count(*) FROM task_associations").fetchone()[0], 0)

    def test_managed_recovery_resume_rejection_and_completion(self):
        pair = self.managed()
        first = self.associations.start_pair(self.alice, pair, operation_key="start", lease_seconds=5)
        self.associations.submit_pair(self.alice, pair, first, "output", operation_key="submit")
        self.clock.now += 5
        self.associations.recover_pair(self.alice, pair, first, operation_key="recover")
        second = self.associations.resume_pair(self.alice, pair, operation_key="resume", lease_seconds=15)
        self.assertNotEqual(first, second)
        self.associations.review_pair(self.alice, pair, second, "output", approved=False, confirmed=True, operation_key="reject")
        self.associations.submit_pair(self.alice, pair, second, "new output", operation_key="submit2")
        self.associations.complete_pair(self.alice, pair, second, "new output", confirmed=True, operation_key="complete")
        self.assertEqual(self.queue.get(self.alice, self.item)["status"], "completed")
        self.state.backup_to(self.directory / "resumed.sqlite3", confirmed=True)

    def test_ready_filters_managed_before_limit_and_preserves_other_owner(self):
        self.upgrade()
        self.associations.create_managed(self.alice, self.item, self.workflow, operation_key="managed")
        normal = self.queue.enqueue(self.alice, "unmanaged", operation_key="normal", priority=1)
        bob = self.queue.enqueue(self.bob, "other owner", operation_key="bob")
        self.assertEqual([row["id"] for row in self.queue.ready(self.alice, limit=1)], [normal])
        self.assertEqual([row["id"] for row in self.queue.ready(self.bob, limit=1)], [bob])
        self.assertTrue(self.queue.claim(self.alice, normal, operation_key="normal-claim", lease_seconds=10))

    def test_no_conversion_of_legacy_or_previously_used_queue(self):
        self.upgrade()
        lease = self.queue.claim(self.alice, self.item, operation_key="old-claim", lease_seconds=5)
        self.clock.now += 5
        self.queue.recover(self.alice, self.item, lease, operation_key="old-recover")
        with self.assertRaises(AssociationError):
            self.associations.create_managed(self.alice, self.item, self.workflow, operation_key="managed")
        self.assertEqual(self.workflows.get(self.alice, self.workflow)["status"], "planned")
        legacy = self.associations.create(self.alice, self.item, self.workflow, operation_key="legacy")
        with self.assertRaises(AssociationError):
            self.associations.create_managed(self.alice, self.item, self.workflow, operation_key="managed")
        self.assertEqual(self.associations.get(self.alice, legacy)["queue_id"], self.item)

    def test_deleted_marker_with_retained_operation_fails_closed(self):
        pair = self.managed()
        with self.state.transaction() as conn:
            conn.execute("DELETE FROM managed_task_pairs WHERE id=?", (pair,))
        with self.assertRaises(StateStorageError):
            self.queue.ready(self.alice)
        with self.assertRaises(StateStorageError):
            self.queue.claim(self.alice, self.item, operation_key="direct", lease_seconds=5)
        with self.assertRaises(StateStorageError):
            self.workflows.start(self.alice, self.workflow, operation_key="direct-start")
        with self.assertRaises(StateStorageError):
            self.associations.create_managed(self.alice, self.item, self.workflow, operation_key="managed")
        with self.assertRaises(StateStorageError):
            self.state.backup_to(self.directory / "missing-marker.sqlite3", confirmed=True)

    def test_deleted_marker_and_operation_with_retained_audit_fails_closed(self):
        pair = self.managed()
        with self.state.transaction() as conn:
            conn.execute("DELETE FROM managed_task_pairs WHERE id=?", (pair,))
            conn.execute("DELETE FROM state_operations WHERE resource_type='managed_task_pair' AND resource_id=?", (pair,))
        with self.assertRaises(StateStorageError):
            self.queue.ready(self.alice)
        with self.assertRaises(StateStorageError):
            self.workflows.start(self.alice, self.workflow, operation_key="direct-start")
        with self.assertRaises(StateStorageError):
            self.state.backup_to(self.directory / "missing-marker-and-op.sqlite3", confirmed=True)

    def test_coordinated_marker_and_history_deletion_is_not_tamper_evident(self):
        pair = self.managed()
        with self.state.transaction() as conn:
            conn.execute("DELETE FROM managed_task_pairs WHERE id=?", (pair,))
            conn.execute("DELETE FROM state_operations WHERE resource_type='managed_task_pair' AND resource_id=?", (pair,))
            conn.execute("DELETE FROM state_audit WHERE resource_type='managed_task_pair' AND resource_id=?", (pair,))
        # The remaining association is indistinguishable from a legitimate
        # legacy association; this is not an OS or tamper-evidence boundary.
        self.assertTrue(self.queue.claim(self.alice, self.item, operation_key="direct", lease_seconds=5))
        self.state.backup_to(self.directory / "erased-marker.sqlite3", confirmed=True)

    def test_corrupt_managed_creation_history_refused_on_checked_copy(self):
        pair = self.managed()
        with self.state.transaction() as conn:
            conn.execute("DELETE FROM state_audit WHERE resource_type='managed_task_pair' AND resource_id=?", (pair,))
        with self.assertRaises(StateStorageError):
            self.state.backup_to(self.directory / "bad-history.sqlite3", confirmed=True)
        self.assertEqual(self.queue.get(self.alice, self.item)["status"], "queued")

    def test_reserved_v12_operation_type_collision_rolls_back_migration(self):
        with self.state.transaction() as conn:
            conn.execute("INSERT INTO state_operations VALUES (?,?,?,?,?)", ("a"*32, "collision", b"x"*32, "managed_task_pair", "f"*32))
        target = self.directory / "before-v12.sqlite3"
        with self.assertRaises(StateStorageError):
            self.state.migrate_to_v12(target, confirmed=True)
        self.assertTrue(target.exists())
        with self.state.transaction() as conn:
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 11)
            self.assertEqual(conn.execute("SELECT count(*) FROM state_operations WHERE operation_key='collision'").fetchone()[0], 1)

    def test_earlier_services_work_on_v12(self):
        from ai_earn.opportunity_drafts import OpportunityDrafts
        from ai_earn.task_plans import TaskPlans
        from ai_earn.order_drafts import OrderDrafts
        from ai_earn.invoice_drafts import InvoiceDrafts
        from ai_earn.payment_observations import PaymentObservations
        self.upgrade()
        OpportunityDrafts(self.auth, self.state).create(self.alice, "Opportunity", operation_key="opp")
        TaskPlans(self.auth, self.state).create(self.alice, "Plan", operation_key="plan")
        order = OrderDrafts(self.auth, self.state).create(self.alice, "Estimate", 100, operation_key="order")
        invoice = InvoiceDrafts(self.auth, self.state).create(self.alice, order, operation_key="invoice")
        PaymentObservations(self.auth, self.state).create(self.alice, invoice, operation_key="placeholder")
        self.state.backup_to(self.directory / "earlier-v12.sqlite3", confirmed=True)

    def test_managed_creation_and_direct_claim_compete_atomically(self):
        self.upgrade()
        def manage():
            try:
                self.associations.create_managed(self.alice, self.item, self.workflow, operation_key="managed")
                return "managed"
            except AssociationError:
                return "denied"
        def claim():
            try:
                self.queue.claim(self.alice, self.item, operation_key="direct", lease_seconds=20)
                return "claimed"
            except QueueError:
                return "denied"
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(manage), pool.submit(claim)]
            result = [future.result(timeout=15) for future in futures]
        self.assertIn(result, (["managed", "denied"], ["denied", "claimed"]))
        self.state.backup_to(self.directory / "race.sqlite3", confirmed=True)

    def test_v12_migration_crash_retains_checked_pre_migration_backup(self):
        target = self.directory / "before-v12.sqlite3"
        code = r"""
import os, sys
from pathlib import Path
from ai_earn import state_storage as s
original = s._apply_v12
def crash(conn):
    original(conn)
    os._exit(73)
s._apply_v12 = crash
s.StateStorage(Path(sys.argv[1])).migrate_to_v12(Path(sys.argv[2]), confirmed=True)
"""
        result = subprocess.run([sys.executable, "-c", code, str(self.state.path), str(target)], capture_output=True, timeout=30)
        self.assertEqual(result.returncode, 73)
        self.assertTrue(target.exists())
        for store in (self.state, StateStorage(target)):
            with store.transaction() as conn:
                self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 11)
                self.assertIsNone(conn.execute("SELECT 1 FROM sqlite_master WHERE name='managed_task_pairs'").fetchone())

    def test_ambiguous_auth_exit_can_retry_creation_exactly_once(self):
        self.upgrade()
        original = self.auth.storage.transaction
        @contextmanager
        def fail_after_commit():
            with original() as conn:
                yield conn
                raise RuntimeError("injected auth exit")
        with patch.object(self.auth.storage, "transaction", fail_after_commit):
            with self.assertRaises(RuntimeError):
                self.associations.create_managed(self.alice, self.item, self.workflow, operation_key="managed")
        pair = self.associations.create_managed(self.alice, self.item, self.workflow, operation_key="managed")
        self.assertEqual(self.associations.get(self.alice, pair)["id"], pair)
        with self.state.transaction() as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM managed_task_pairs").fetchone()[0], 1)
        self.state.backup_to(self.directory / "ambiguous.sqlite3", confirmed=True)

    def test_controlled_creation_crashes_before_and_after_state_commit(self):
        self.upgrade()
        code = r"""
from contextlib import contextmanager
from pathlib import Path
import json, os, sys
import ai_earn.task_associations as module
from ai_earn.auth import AuthService
from ai_earn.auth_storage import AuthStorage
from ai_earn.state_storage import StateStorage
p = json.load(sys.stdin)
auth = AuthService(AuthStorage(Path(p['auth']), create=False), lambda: 1800000000)
service = module.TaskAssociations(auth, StateStorage(Path(p['state'])))
if p['point'] == 'before':
    original = module._managed_audit
    def crash(*args):
        original(*args)
        os._exit(73)
    module._managed_audit = crash
else:
    original = auth.storage.transaction
    @contextmanager
    def crash_after_state_commit():
        with original() as conn:
            yield conn
            os._exit(74)
    auth.storage.transaction = crash_after_state_commit
service.create_managed(p['token'], p['queue'], p['workflow'], operation_key='managed')
"""
        def run(point):
            data = dict(auth=str(self.auth.storage.path), state=str(self.state.path), token=self.alice,
                        queue=self.item, workflow=self.workflow, point=point)
            return subprocess.run([sys.executable, "-c", code], input=json.dumps(data), text=True, capture_output=True, timeout=30)
        self.assertEqual(run("before").returncode, 73)
        with self.state.transaction() as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM managed_task_pairs").fetchone()[0], 0)
            self.assertEqual(conn.execute("SELECT count(*) FROM task_associations").fetchone()[0], 0)
        self.assertEqual(run("after").returncode, 74)
        pair = self.associations.create_managed(self.alice, self.item, self.workflow, operation_key="managed")
        with self.assertRaises(QueueError):
            self.queue.claim(self.alice, self.item, operation_key="direct", lease_seconds=5)
        self.assertEqual(self.associations.inspect(self.alice, pair)["workflow_status"], "planned")
        self.state.backup_to(self.directory / "crash.sqlite3", confirmed=True)

    def test_inconsistent_managed_parent_states_fail_closed_and_backup_refuses(self):
        pair = self.managed()
        # Simulate an unsupported local database mutation that leaves a queued
        # managed pair with a cancelled workflow. No automatic repair occurs.
        with self.state.transaction() as conn:
            conn.execute("UPDATE task_workflows SET status='cancelled' WHERE id=?", (self.workflow,))
        with self.assertRaises(StateStorageError):
            self.queue.get(self.alice, self.item)
        with self.assertRaises(StateStorageError):
            self.workflows.get(self.alice, self.workflow)
        with self.assertRaises(StateStorageError):
            self.state.backup_to(self.directory / "bad-managed.sqlite3", confirmed=True)
        with self.state.transaction() as conn:
            self.assertEqual(conn.execute("SELECT status FROM task_queue WHERE id=?", (self.item,)).fetchone()[0], "queued")
            self.assertEqual(conn.execute("SELECT count(*) FROM managed_task_pairs WHERE id=?", (pair,)).fetchone()[0], 1)

    def test_migration_failure_preserves_source_and_backup(self):
        target = self.directory / "before-v12.sqlite3"
        from ai_earn.state_storage import _apply_v12
        def fail(conn):
            _apply_v12(conn)
            raise RuntimeError("injected")
        with patch("ai_earn.state_storage._apply_v12", side_effect=fail):
            with self.assertRaises(RuntimeError):
                self.state.migrate_to_v12(target, confirmed=True)
        for store in (self.state, StateStorage(target)):
            with store.transaction() as conn:
                self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 11)
                self.assertIsNone(conn.execute("SELECT 1 FROM sqlite_master WHERE name='managed_task_pairs'").fetchone())

if __name__ == "__main__":
    unittest.main()

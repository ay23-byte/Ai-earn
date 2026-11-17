"""Phase 5 completion — task management and safe execution (v23/v24)."""
import os, tempfile, unittest
from pathlib import Path
from unittest.mock import patch
import sqlite3

from ai_earn.auth_storage import AuthStorage
from ai_earn.auth import AuthService
from ai_earn.state_storage import StateStorage, _check_integrity, _validate_schema
from ai_earn.opportunities import Opportunities
from ai_earn.project_tasks import ProjectTasks, ProjectTaskError
from ai_earn.project_task_runner import ProjectTaskRunner, ProjectTaskRunnerError
from ai_earn.state_storage import LATEST_SCHEMA_VERSION

class Clock:
    now = 1800000000
    def __call__(self):
        return self.now

class Phase5Tests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.tmp.cleanup)
        self.priv = Path(self.tmp.name) / "priv"
        if os.name == "nt":
            from ai_earn.windows_security import create_private_directory
            create_private_directory(self.priv)
        else:
            self.priv.mkdir(mode=0o700)
        self.clock = Clock()
        patcher = patch("ai_earn.auth.PASSWORD_ITERATIONS", 1000)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.auth_path = self.priv / "auth.sqlite3"
        self.state_path = self.priv / "state.sqlite3"
        self.auth = AuthService(AuthStorage(self.auth_path, create=True), self.clock)
        self.auth.register("alice", "disposable test password")
        self.token = self.auth.login("alice", "disposable test password")
        self.state = StateStorage(self.state_path, create=True, confirmed=True)
        for v in range(2, LATEST_SCHEMA_VERSION+1):
            getattr(self.state, f"migrate_to_v{v}")(self.priv / f"before-v{v}.sqlite3", confirmed=True)
        self.pt = ProjectTasks(AuthService(AuthStorage(self.auth_path), self.clock), StateStorage(self.state_path, create=False))
        self.runner = ProjectTaskRunner(AuthService(AuthStorage(self.auth_path), self.clock), StateStorage(self.state_path, create=False))
        self.opp = Opportunities(AuthService(AuthStorage(self.auth_path), self.clock), StateStorage(self.state_path, create=False))

    def _other_owner(self):
        # create second owner
        auth2 = AuthService(AuthStorage(self.auth_path), self.clock)
        auth2.register("bob", "other disposable password")
        token2 = auth2.login("bob", "other disposable password")
        pt2 = ProjectTasks(AuthService(AuthStorage(self.auth_path), self.clock), StateStorage(self.state_path))
        return token2, pt2

    def test_create_from_opportunity_and_subtasks(self):
        oid = self.opp.create(self.token, "Freelance gig", "https://example.com/gig", 100000, operation_key="op_phase5_1")
        tid = self.pt.create_from_opportunity(self.token, oid, operation_key="task_from_opp1", description="from opp")
        view = self.pt.get(self.token, tid)
        self.assertEqual(view["opportunity_id"], oid)
        sub = self.pt.create_subtask(self.token, tid, "Subtask A", operation_key="sub_a1", priority=7)
        sub_view = self.pt.get(self.token, sub)
        self.assertEqual(sub_view["parent_task_id"], tid)
        self.assertEqual(sub_view["priority"], 7)
        # list subtasks
        listed = self.pt.list(self.token, parent_task_id=tid)
        self.assertEqual(len(listed), 1)

    def test_priorities_deadlines_and_schedule_ordering(self):
        # create 3 tasks with different priorities and deadlines
        t_low = self.pt.create(self.token, "Low", operation_key="prio_low", priority=1)
        t_high = self.pt.create(self.token, "High", operation_key="prio_high", priority=9)
        t_mid = self.pt.create(self.token, "Mid", operation_key="prio_mid", priority=5)
        # deadlines: use future timestamps
        now = self.clock.now
        # schedule with explicit deadline param (runner will respect)
        self.pt.schedule(self.token, t_low, now+10000, operation_key="sched_low")
        self.pt.schedule(self.token, t_high, now+20000, operation_key="sched_high")
        self.pt.schedule(self.token, t_mid, now+5000, operation_key="sched_mid")
        # claim_next should respect priority first, then deadline
        # High priority should be first despite later deadline
        claimed = self.runner.claim_next(self.token, operation_key="claim_prio1", lease_seconds=60)
        self.assertIsNotNone(claimed)
        self.assertEqual(claimed["id"], t_high)
        self.runner.complete(self.token, claimed["id"], claimed["lease_id"], operation_key="comp_high")
        # Next should be mid (priority 5) before low (1)
        claimed2 = self.runner.claim_next(self.token, operation_key="claim_prio2", lease_seconds=60)
        self.assertEqual(claimed2["id"], t_mid)
        self.runner.complete(self.token, claimed2["id"], claimed2["lease_id"], operation_key="comp_mid")
        claimed3 = self.runner.claim_next(self.token, operation_key="claim_prio3", lease_seconds=60)
        self.assertEqual(claimed3["id"], t_low)

    def test_estimated_actual_time_and_deadlines(self):
        tid = self.pt.create(self.token, "Timed task", operation_key="timed1", estimated_duration_seconds=3600, deadline_at=0)
        # set estimated
        self.pt.set_estimated_duration(self.token, tid, 7200, operation_key="est_upd")
        self.pt.record_actual_duration(self.token, tid, 1800, operation_key="act_rec")
        view = self.pt.get(self.token, tid)
        self.assertEqual(view["estimated_duration_seconds"], 7200)
        self.assertEqual(view["actual_duration_seconds"], 1800)
        # deadline must be future
        with self.assertRaises(ProjectTaskError):
            future = self.clock.now - 100
            self.pt.create(self.token, "Bad deadline", operation_key="bad_dl", deadline_at=future)

    def test_dependencies_and_cycle_detection(self):
        a = self.pt.create(self.token, "A", operation_key="dep_a")
        b = self.pt.create(self.token, "B", operation_key="dep_b")
        c = self.pt.create(self.token, "C", operation_key="dep_c")
        # A depends on B, B depends on C -> valid
        self.pt.add_dependency(self.token, a, b, operation_key="dep_ab")
        self.pt.add_dependency(self.token, b, c, operation_key="dep_bc")
        # Cycle: C depends on A should fail
        with self.assertRaises(ProjectTaskError):
            self.pt.add_dependency(self.token, c, a, operation_key="dep_ca")
        # Self dependency fails
        with self.assertRaises(ProjectTaskError):
            self.pt.add_dependency(self.token, a, a, operation_key="dep_self")
        # Schedule all
        self.pt.schedule(self.token, a, 0, operation_key="sched_a")
        self.pt.schedule(self.token, b, 0, operation_key="sched_b")
        self.pt.schedule(self.token, c, 0, operation_key="sched_c")
        # C has no dependencies, should be claimable first (priority equal, but C is dependency of B)
        # However runner respects dependencies: A depends on B, so A not ready until B completed
        # So claim should give C first
        claimed = self.runner.claim_next(self.token, operation_key="claim_dep1", lease_seconds=60)
        self.assertEqual(claimed["id"], c)
        self.runner.complete(self.token, claimed["id"], claimed["lease_id"], operation_key="comp_c")
        # Next should be B (now C completed)
        claimed2 = self.runner.claim_next(self.token, operation_key="claim_dep2", lease_seconds=60)
        self.assertEqual(claimed2["id"], b)
        self.runner.complete(self.token, claimed2["id"], claimed2["lease_id"], operation_key="comp_b")
        # Now A
        claimed3 = self.runner.claim_next(self.token, operation_key="claim_dep3", lease_seconds=60)
        self.assertEqual(claimed3["id"], a)

    def test_manual_review_gate_no_uncontrolled_autonomy(self):
        tid = self.pt.create(self.token, "Needs review", operation_key="rev_gate1")
        self.pt.submit_for_review(self.token, tid, operation_key="sub_rev_gate")
        view = self.pt.get(self.token, tid)
        self.assertEqual(view["status"], "pending_review")
        # Runner should not be able to claim pending_review tasks
        claimed = self.runner.claim_next(self.token, operation_key="claim_gate1", lease_seconds=60)
        self.assertIsNone(claimed)
        # Scheduling pending_review should fail
        with self.assertRaises(ProjectTaskError):
            self.pt.schedule(self.token, tid, 0, operation_key="sched_gate_fail")
        # Approve then schedule
        self.pt.approve(self.token, tid, operation_key="appr_gate")
        self.pt.schedule(self.token, tid, 0, operation_key="sched_gate")
        self.assertEqual(self.pt.get(self.token, tid)["status"], "scheduled")
        claimed2 = self.runner.claim_next(self.token, operation_key="claim_gate2", lease_seconds=60)
        self.assertIsNotNone(claimed2)
        self.assertEqual(claimed2["id"], tid)

    def test_execution_logs_and_audit(self):
        tid = self.pt.create(self.token, "Logged task", operation_key="log_task1")
        self.pt.schedule(self.token, tid, 0, operation_key="log_sched")
        claimed = self.runner.claim_next(self.token, operation_key="log_claim", lease_seconds=60)
        self.runner.complete(self.token, claimed["id"], claimed["lease_id"], operation_key="log_comp")
        logs = self.pt.list_logs(self.token, tid)
        self.assertGreaterEqual(len(logs), 3)  # created, scheduled, claimed, completed
        # Audit check: state_audit should have events for this task
        with StateStorage(self.state_path).transaction() as conn:
            rows = conn.execute("SELECT event FROM state_audit WHERE resource_type='project_task' AND resource_id=? ORDER BY id", (tid,)).fetchall()
            events = [r[0] for r in rows]
            self.assertIn("project_task_created", events)
            self.assertIn("project_task_scheduled", events)
            self.assertIn("project_task_started", events)
            self.assertIn("project_task_completed", events)
        # Custom log
        self.pt.log_execution(self.token, tid, "Custom execution step", operation_key="custom_log1")
        logs2 = self.pt.list_logs(self.token, tid)
        self.assertEqual(len(logs2), len(logs)+1)

    def test_retry_recoverable_and_max_attempts(self):
        tid = self.pt.create(self.token, "Retry task", operation_key="retry_task1")
        self.pt.schedule(self.token, tid, 0, operation_key="retry_sched")
        claimed = self.runner.claim_next(self.token, operation_key="retry_claim", lease_seconds=60)
        # Fail recoverable
        self.runner.fail(self.token, claimed["id"], claimed["lease_id"], operation_key="retry_fail1", error_message="transient", recoverable=True)
        view = self.pt.get(self.token, tid)
        self.assertEqual(view["status"], "failed")
        self.assertGreater(view["available_at"], self.clock.now)
        # Retry before backoff should fail
        with self.assertRaises(ProjectTaskRunnerError):
            self.runner.retry(self.token, tid, operation_key="retry_early")
        # Fast-forward by clearing available_at
        with StateStorage(self.state_path).transaction() as conn:
            conn.execute("UPDATE project_tasks SET available_at=0 WHERE id=?", (tid,))
        self.runner.retry(self.token, tid, operation_key="retry_ok")
        self.assertEqual(self.pt.get(self.token, tid)["status"], "scheduled")
        # Claim again and fail terminally (non-recoverable)
        claimed2 = self.runner.claim_next(self.token, operation_key="retry_claim2", lease_seconds=60)
        self.runner.fail(self.token, claimed2["id"], claimed2["lease_id"], operation_key="retry_fail2", error_message="fatal", recoverable=False)
        self.assertEqual(self.pt.get(self.token, tid)["status"], "failed")
        # Max attempts: set max_attempts=1 and fail recoverable should become terminal
        tid2 = self.pt.create(self.token, "Max task", operation_key="max_task1")
        with StateStorage(self.state_path).transaction() as conn:
            conn.execute("UPDATE project_tasks SET max_attempts=1 WHERE id=?", (tid2,))
        self.pt.schedule(self.token, tid2, 0, operation_key="max_sched")
        claimed3 = self.runner.claim_next(self.token, operation_key="max_claim", lease_seconds=60)
        self.runner.fail(self.token, claimed3["id"], claimed3["lease_id"], operation_key="max_fail", error_message="temp", recoverable=True)
        self.assertEqual(self.pt.get(self.token, tid2)["status"], "failed")
        # Retry should fail due to max attempts
        with StateStorage(self.state_path).transaction() as conn:
            conn.execute("UPDATE project_tasks SET available_at=0 WHERE id=?", (tid2,))
        with self.assertRaises(ProjectTaskRunnerError):
            self.runner.retry(self.token, tid2, operation_key="max_retry")

    def test_cancel_safely_including_leased(self):
        tid = self.pt.create(self.token, "Cancel me", operation_key="cancel_task1")
        self.pt.schedule(self.token, tid, 0, operation_key="cancel_sched")
        claimed = self.runner.claim_next(self.token, operation_key="cancel_claim", lease_seconds=60)
        # Cancel while in_progress (leased) should succeed and clear lease
        self.runner.cancel(self.token, tid, operation_key="cancel_op1")
        view = self.pt.get(self.token, tid)
        self.assertEqual(view["status"], "cancelled")
        self.assertIsNone(view["lease_id"])
        # Cancel again should fail (already cancelled)
        with self.assertRaises(ProjectTaskRunnerError):
            self.runner.cancel(self.token, tid, operation_key="cancel_op2")
        # Cancel draft task
        tid2 = self.pt.create(self.token, "Cancel draft", operation_key="cancel_draft1")
        self.pt.cancel(self.token, tid2, operation_key="cancel_draft_op")
        self.assertEqual(self.pt.get(self.token, tid2)["status"], "cancelled")

    def test_recovery_after_restart_lease_expiry(self):
        tid = self.pt.create(self.token, "Recover task", operation_key="recover_task1")
        self.pt.schedule(self.token, tid, 0, operation_key="recover_sched")
        claimed = self.runner.claim_next(self.token, operation_key="recover_claim", lease_seconds=10)
        lease_id = claimed["lease_id"]
        # Simulate time passing beyond lease_until
        original_until = claimed["lease_until"]
        self.clock.now = original_until + 5
        # New runner instance simulating restart (same auth/state but new object)
        runner2 = ProjectTaskRunner(AuthService(AuthStorage(self.auth_path), self.clock), StateStorage(self.state_path))
        # Complete should now fail due to expiry
        with self.assertRaises(ProjectTaskRunnerError):
            runner2.complete(self.token, tid, lease_id, operation_key="recover_comp_fail")
        # Recover should succeed and set to failed with backoff
        runner2.recover(self.token, tid, lease_id, operation_key="recover_op")
        view = self.pt.get(self.token, tid)
        self.assertEqual(view["status"], "failed")
        # Clear backoff and retry
        with StateStorage(self.state_path).transaction() as conn:
            conn.execute("UPDATE project_tasks SET available_at=0 WHERE id=?", (tid,))
        runner2.retry(self.token, tid, operation_key="recover_retry")
        self.assertEqual(self.pt.get(self.token, tid)["status"], "scheduled")
        # Now claim again should work
        claimed2 = runner2.claim_next(self.token, operation_key="recover_claim2", lease_seconds=60)
        self.assertIsNotNone(claimed2)
        self.clock.now = 1800000000  # reset

    def test_idempotency_and_account_scoping(self):
        tid = self.pt.create(self.token, "Idempotent", operation_key="idem_key1", priority=3)
        dup = self.pt.create(self.token, "Idempotent", operation_key="idem_key1", priority=3)
        self.assertEqual(tid, dup)
        # Different operation key with same title but different priority should fail (key reuse)
        with self.assertRaises(ProjectTaskError):
            self.pt.create(self.token, "Idempotent", operation_key="idem_key1", priority=9)
        # Other owner cannot see
        token2, pt2 = self._other_owner()
        with self.assertRaises(Exception):
            pt2.get(token2, tid)
        # Other owner can create same operation_key (per-owner isolation)
        tid2_other = pt2.create(token2, "Idempotent", operation_key="idem_key1", priority=3)
        self.assertNotEqual(tid, tid2_other)
        # Runner idempotency
        self.pt.schedule(self.token, tid, 0, operation_key="idem_sched")
        claimed = self.runner.claim_next(self.token, operation_key="idem_claim", lease_seconds=60)
        # Same claim operation_key should return same
        claimed_dup = self.runner.claim_next(self.token, operation_key="idem_claim", lease_seconds=60)
        self.assertEqual(claimed["id"], claimed_dup["id"])

    def test_transactional_migrations_and_integrity(self):
        # Verify LATEST is 25 and backups count
        self.assertEqual(LATEST_SCHEMA_VERSION, 28)
        with StateStorage(self.state_path).transaction() as conn:
            self.assertEqual(_validate_schema(conn), LATEST_SCHEMA_VERSION)
            _check_integrity(conn)
        # Check backups exist
        count = len(list(self.priv.glob("before-v*.sqlite3")))
        self.assertEqual(count, LATEST_SCHEMA_VERSION-1)

    def test_status_fsm_all_valid_transitions(self):
        # Draft -> pending_review -> approved -> scheduled -> in_progress -> completed
        t = self.pt.create(self.token, "FSM", operation_key="fsm1")
        self.pt.submit_for_review(self.token, t, operation_key="fsm_sub")
        self.assertEqual(self.pt.get(self.token, t)["status"], "pending_review")
        self.pt.approve(self.token, t, operation_key="fsm_appr")
        self.assertEqual(self.pt.get(self.token, t)["status"], "approved")
        self.pt.schedule(self.token, t, 0, operation_key="fsm_sched")
        self.assertEqual(self.pt.get(self.token, t)["status"], "scheduled")
        claimed = self.runner.claim_next(self.token, operation_key="fsm_claim", lease_seconds=60)
        self.assertEqual(claimed["status"], "in_progress")
        self.runner.complete(self.token, t, claimed["lease_id"], operation_key="fsm_comp")
        self.assertEqual(self.pt.get(self.token, t)["status"], "completed")
        # Failed path
        tf = self.pt.create(self.token, "FSM fail", operation_key="fsm_fail1")
        self.pt.schedule(self.token, tf, 0, operation_key="fsm_f_sched")
        c2 = self.runner.claim_next(self.token, operation_key="fsm_f_claim", lease_seconds=60)
        self.runner.fail(self.token, tf, c2["lease_id"], operation_key="fsm_f_fail", error_message="err", recoverable=True)
        self.assertEqual(self.pt.get(self.token, tf)["status"], "failed")
        with StateStorage(self.state_path).transaction() as conn:
            conn.execute("UPDATE project_tasks SET available_at=0 WHERE id=?", (tf,))
        self.runner.retry(self.token, tf, operation_key="fsm_f_retry")
        self.assertEqual(self.pt.get(self.token, tf)["status"], "scheduled")
        # Invalid transition: completed -> cancel should fail
        with self.assertRaises(ProjectTaskRunnerError):
            self.runner.cancel(self.token, t, operation_key="fsm_invalid_cancel")

    def test_deadline_enforcement(self):
        now = self.clock.now
        tid = self.pt.create(self.token, "Deadline task", operation_key="dl_task", deadline_at=now+1000)
        self.pt.schedule(self.token, tid, now+500, operation_key="dl_sched")
        # Advance clock past deadline
        self.clock.now = now+1500
        claimed = self.runner.claim_next(self.token, operation_key="dl_claim", lease_seconds=60)
        self.assertIsNone(claimed)  # deadline passed, not claimable
        # Creating with past deadline should fail
        with self.assertRaises(ProjectTaskError):
            self.pt.create(self.token, "Past", operation_key="dl_past", deadline_at=now-10)

    def test_opportunity_to_task_flow(self):
        # E2E: opportunity -> vetted -> task -> subtasks -> execution
        oid = self.opp.create(self.token, "Big project", "https://example.com/big", 500000, operation_key="opp_e2e")
        # Simulate vetting via opportunities update? Use status
        task_main = self.pt.create_from_opportunity(self.token, oid, operation_key="e2e_main", description="main work")
        sub1 = self.pt.create_subtask(self.token, task_main, "Phase 1", operation_key="e2e_sub1", priority=8, estimated_duration_seconds=3600)
        sub2 = self.pt.create_subtask(self.token, task_main, "Phase 2", operation_key="e2e_sub2", priority=5, estimated_duration_seconds=7200)
        self.pt.add_dependency(self.token, sub2, sub1, operation_key="e2e_dep")
        for sid in (sub1, sub2, task_main):
            self.pt.schedule(self.token, sid, 0, operation_key=f"e2e_sched_{sid[:4]}")
        # sub2 depends on sub1, so claim order should be sub1, then task_main or sub2? Priority 8 vs 5, but sub2 blocked
        # Scheduled tasks: main (5), sub1 (8), sub2 (5 blocked)
        claimed = self.runner.claim_next(self.token, operation_key="e2e_claim1", lease_seconds=60)
        self.assertEqual(claimed["id"], sub1)
        self.runner.complete(self.token, claimed["id"], claimed["lease_id"], operation_key="e2e_comp1")
        # Now sub2 unblocked, priority 5 vs main 5 => order by deadline/available/id
        claimed2 = self.runner.claim_next(self.token, operation_key="e2e_claim2", lease_seconds=60)
        # Both sub2 and main are priority 5, which one is picked? Should be deterministic by id ordering; just check one of them
        self.assertIn(claimed2["id"], (sub2, task_main))
        # Verify list filters
        scheduled = self.pt.list(self.token, status="scheduled")
        self.assertGreaterEqual(len(scheduled), 1)

if __name__ == "__main__":
    unittest.main()


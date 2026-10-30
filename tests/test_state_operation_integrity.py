"""Disposable backup checks for recognized idempotency result ownership."""
from contextlib import closing
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from ai_earn.auth import AuthService
from ai_earn.auth_storage import AuthStorage
from ai_earn.invoice_drafts import InvoiceDrafts
from ai_earn.opportunity_drafts import OpportunityDrafts
from ai_earn.order_drafts import OrderDrafts
from ai_earn.payment_observations import PaymentObservations
from ai_earn.state_storage import StateStorage, StateStorageError, _copy_database
from ai_earn.task_plans import TaskPlans
from ai_earn.task_queue import TaskQueue
from ai_earn.task_workflows import TaskWorkflows


@unittest.skipUnless(os.name in {"posix", "nt"}, "requires a supported private filesystem")
class OperationIntegrityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        directory = Path(self.tmp.name) / "private"
        if os.name == "nt":
            from ai_earn.windows_security import create_private_directory
            create_private_directory(directory)
        else:
            directory.mkdir(mode=0o700)
        self.directory = directory
        patcher = patch("ai_earn.auth.PASSWORD_ITERATIONS", 1000)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.auth = AuthService(AuthStorage(directory / "auth.sqlite3"), lambda: 1_800_000_000)
        for username in ("alice", "bobby"):
            self.auth.register(username, "disposable test password")
        self.alice = self.auth.login("alice", "disposable test password")
        self.bob = self.auth.login("bobby", "disposable test password")
        self.state = StateStorage(directory / "state.sqlite3", create=True, confirmed=True)
        for version in range(2, 10):
            getattr(self.state, f"migrate_to_v{version}")(directory / f"v{version - 1}.sqlite3", confirmed=True)
        self.ids = {}
        for token, prefix in ((self.alice, "a"), (self.bob, "b")):
            opportunity = OpportunityDrafts(self.auth, self.state).create(token, "Synthetic", operation_key=prefix + "-opp")
            task = TaskPlans(self.auth, self.state).create(token, "Synthetic", operation_key=prefix + "-plan")
            orders = OrderDrafts(self.auth, self.state)
            order = orders.create(token, "Synthetic", 100, operation_key=prefix + "-order")
            invoice = InvoiceDrafts(self.auth, self.state).create(token, order, operation_key=prefix + "-invoice")
            queue = TaskQueue(self.auth, self.state)
            item = queue.enqueue(token, "Synthetic", operation_key=prefix + "-queue")
            lease = queue.claim(token, item, operation_key=prefix + "-lease", lease_seconds=60)
            workflow = TaskWorkflows(self.auth, self.state).create(token, "Synthetic", operation_key=prefix + "-workflow")
            observation = PaymentObservations(self.auth, self.state).create(token, invoice, operation_key=prefix + "-observation")
            self.ids[prefix] = dict(opp=opportunity, plan=task, order=order, invoice=invoice,
                                    queue=item, lease=lease, workflow=workflow, observation=observation)

    def _change_result(self, key, result):
        with self.state.transaction() as conn:
            conn.execute("UPDATE state_operations SET resource_id=? WHERE operation_key=?", (result, key))

    def test_missing_and_cross_owner_results_refused_for_each_recognized_type(self):
        for kind in self.ids["a"]:
            for label, target in (("missing", "f" * 32), ("other-owner", self.ids["b"][kind])):
                with self.subTest(kind=kind, corruption=label):
                    self._change_result("a-" + kind, target)
                    backup = self.directory / (kind + "-" + label + ".sqlite3")
                    try:
                        with self.assertRaises(StateStorageError):
                            self.state.backup_to(backup, confirmed=True)
                        self.assertFalse(backup.exists())
                    finally:
                        self._change_result("a-" + kind, self.ids["a"][kind])

    def test_checked_backup_and_restore_keep_valid_results_and_retries(self):
        backup = self.directory / "checked.sqlite3"
        restored = self.directory / "restored.sqlite3"
        self.state.backup_to(backup, confirmed=True)
        StateStorage(backup).backup_to(restored, confirmed=True)
        orders = OrderDrafts(self.auth, StateStorage(restored))
        self.assertEqual(orders.create(self.alice, "Synthetic", 100, operation_key="a-order"), self.ids["a"]["order"])
        with StateStorage(restored).transaction() as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM state_operations").fetchone()[0], 16)

    def test_copy_corruption_is_detected_and_incomplete_copy_is_not_trusted(self):
        def corrupt_copy(source, destination):
            _copy_database(source, destination)
            destination.execute("UPDATE state_operations SET resource_id=? WHERE operation_key='a-order'", (self.ids["b"]["order"],))
            destination.commit()
        backup = self.directory / "bad-copy.sqlite3"
        with patch("ai_earn.state_storage._copy_database", side_effect=corrupt_copy):
            with self.assertRaises(StateStorageError):
                self.state.backup_to(backup, confirmed=True)
        self.assertTrue(backup.exists())  # A failed copy is intentionally retained, not accepted.
        with self.state.transaction() as conn:
            self.assertEqual(conn.execute("SELECT resource_id FROM state_operations WHERE operation_key='a-order'").fetchone()[0], self.ids["a"]["order"])
        with self.assertRaises(StateStorageError):
            StateStorage(backup).backup_to(self.directory / "bad-restore.sqlite3", confirmed=True)

    def test_v8_migration_refuses_bad_result_without_changing_source(self):
        source = StateStorage(self.directory / "v8.sqlite3")
        # v8 pre-migration snapshot was empty; add a synthetic v8-valid operation.
        with source.transaction() as conn:
            conn.execute("INSERT INTO state_operations VALUES (?,?,?,?,?)", ("a" * 32, "bad", b"x" * 32, "task_workflow", "f" * 32))
        dest = self.directory / "blocked-v8.sqlite3"
        with self.assertRaises(StateStorageError):
            source.migrate_to_v9(dest, confirmed=True)
        self.assertFalse(dest.exists())
        with closing(sqlite3.connect(source.path)) as conn:
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 8)
            self.assertEqual(conn.execute("SELECT count(*) FROM state_operations WHERE operation_key='bad'").fetchone()[0], 1)

    def test_each_earlier_schema_checks_its_available_types(self):
        types = {3: "opportunity_draft", 4: "task_plan", 5: "order_draft",
                 6: "invoice_draft", 7: "queue_lease", 8: "task_workflow"}
        for version, kind in types.items():
            with self.subTest(version=version, kind=kind):
                snapshot = StateStorage(self.directory / f"v{version}.sqlite3")
                with snapshot.transaction() as conn:
                    conn.execute("INSERT INTO state_operations VALUES (?,?,?,?,?)", ("a" * 32, "bad", b"x" * 32, kind, "f" * 32))
                target = self.directory / f"blocked-v{version}.sqlite3"
                with self.assertRaises(StateStorageError):
                    snapshot.backup_to(target, confirmed=True)
                self.assertFalse(target.exists())

    def test_valid_reference_does_not_verify_request_or_audit(self):
        # Swapping two owned results preserves counts but violates request semantics.
        other = OrderDrafts(self.auth, self.state).create(self.alice, "Other", 1, operation_key="other-order")
        self._change_result("a-order", other)
        self._change_result("other-order", self.ids["a"]["order"])
        self.state.backup_to(self.directory / "same-owner.sqlite3", confirmed=True)

    def test_missing_audit_or_operation_is_refused_for_recognized_types(self):
        kinds = {"opp": ("opportunity_draft", "draft_created"),
                 "plan": ("task_plan", "task_planned"),
                 "order": ("order_draft", "order_draft_created"),
                 "invoice": ("invoice_draft", "invoice_draft_created"),
                 "queue": ("task_queue", "queue_enqueued"),
                 "lease": ("task_queue", "queue_leased"),
                 "workflow": ("task_workflow", "workflow_created"),
                 "observation": ("payment_observation", "payment_observation_created")}
        for kind, (resource_type, event) in kinds.items():
            resource_id = self.ids["a"]["queue" if kind == "lease" else kind]
            for removed in ("operation", "audit"):
                with self.subTest(kind=kind, removed=removed):
                    with self.state.transaction() as conn:
                        if removed == "operation":
                            row = tuple(conn.execute("SELECT * FROM state_operations WHERE operation_key=?", ("a-" + kind,)).fetchone())
                            conn.execute("DELETE FROM state_operations WHERE operation_key=?", ("a-" + kind,))
                        else:
                            row = tuple(conn.execute("SELECT * FROM state_audit WHERE resource_type=? AND resource_id=? AND event=?", (resource_type, resource_id, event)).fetchone())
                            conn.execute("DELETE FROM state_audit WHERE id=?", (row[0],))
                    target = self.directory / f"balance-{kind}-{removed}.sqlite3"
                    try:
                        with self.assertRaises(StateStorageError):
                            self.state.backup_to(target, confirmed=True)
                        self.assertFalse(target.exists())
                    finally:
                        with self.state.transaction() as conn:
                            if removed == "operation":
                                conn.execute("INSERT INTO state_operations VALUES (?,?,?,?,?)", row)
                            else:
                                conn.execute("INSERT INTO state_audit VALUES (?,?,?,?,?,?)", row)

    def test_audit_copy_corruption_is_refused(self):
        def corrupt_copy(source, destination):
            _copy_database(source, destination)
            destination.execute("DELETE FROM state_audit WHERE event='queue_leased' AND resource_id=?", (self.ids["a"]["queue"],))
            destination.commit()
        dest = self.directory / "bad-audit-copy.sqlite3"
        with patch("ai_earn.state_storage._copy_database", side_effect=corrupt_copy):
            with self.assertRaises(StateStorageError):
                self.state.backup_to(dest, confirmed=True)
        self.assertTrue(dest.exists())
        with self.assertRaises(StateStorageError):
            StateStorage(dest).backup_to(self.directory / "bad-audit-restore.sqlite3", confirmed=True)

    def test_reserved_type_collision_rolls_back_each_upgrade(self):
        types = {3: "opportunity_draft", 4: "task_plan", 5: "order_draft",
                 6: "invoice_draft", 7: "queue_lease", 8: "task_workflow", 9: "payment_observation"}
        for target_version, kind in types.items():
            with self.subTest(version=target_version, kind=kind):
                version = target_version - 1
                source = StateStorage(self.directory / f"v{version}.sqlite3")
                with source.transaction() as conn:
                    conn.execute("INSERT INTO state_operations VALUES (?,?,?,?,?)", ("a" * 32, "collision", b"x" * 32, kind, "f" * 32))
                backup = self.directory / f"collision-v{version}.sqlite3"
                with self.assertRaises(StateStorageError):
                    getattr(source, f"migrate_to_v{target_version}")(backup, confirmed=True)
                self.assertTrue(backup.exists())
                with source.transaction() as conn:
                    self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], version)
                    self.assertEqual(conn.execute("SELECT count(*) FROM state_operations WHERE operation_key='collision'").fetchone()[0], 1)
                with StateStorage(backup).transaction() as conn:
                    self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], version)
                    self.assertEqual(conn.execute("SELECT count(*) FROM state_operations WHERE operation_key='collision'").fetchone()[0], 1)

    def test_valid_transition_histories_survive_backup_and_restore(self):
        token, ids = self.alice, self.ids["a"]
        OpportunityDrafts(self.auth, self.state).archive(token, ids["opp"], operation_key="archive")
        TaskPlans(self.auth, self.state).cancel(token, ids["plan"], operation_key="plan-cancel")
        queue = TaskQueue(self.auth, self.state)
        queue.retry(token, ids["queue"], ids["lease"], operation_key="queue-retry")
        queue.cancel(token, ids["queue"], operation_key="queue-cancel")
        flow = TaskWorkflows(self.auth, self.state)
        flow.start(token, ids["workflow"], operation_key="flow-start")
        flow.submit(token, ids["workflow"], "synthetic output", operation_key="flow-submit")
        flow.review(token, ids["workflow"], "synthetic output", approved=True, confirmed=True, operation_key="flow-review")
        observations = PaymentObservations(self.auth, self.state)
        observations.flag(token, ids["observation"], operation_key="flag")
        observations.unflag(token, ids["observation"], operation_key="unflag")
        observations.discard(token, ids["observation"], operation_key="discard")
        InvoiceDrafts(self.auth, self.state).cancel(token, ids["invoice"], operation_key="invoice-cancel")
        OrderDrafts(self.auth, self.state).cancel(token, ids["order"], operation_key="order-cancel")
        backup = self.directory / "transitions.sqlite3"
        restore = self.directory / "transitions-restored.sqlite3"
        self.state.backup_to(backup, confirmed=True)
        StateStorage(backup).backup_to(restore, confirmed=True)
        restored_queue = TaskQueue(self.auth, StateStorage(restore))
        self.assertEqual(restored_queue.claim(token, ids["queue"], operation_key="a-lease", lease_seconds=60), ids["lease"])
        self.assertFalse(restored_queue.get_lease(token, ids["lease"])["active"])

    def test_removing_transition_audit_and_operation_can_escape_checks(self):
        OpportunityDrafts(self.auth, self.state).archive(self.alice, self.ids["a"]["opp"], operation_key="archive")
        with self.state.transaction() as conn:
            conn.execute("DELETE FROM state_operations WHERE operation_key='archive'")
            conn.execute("DELETE FROM state_audit WHERE event='draft_archived' AND resource_id=?", (self.ids["a"]["opp"],))
        self.state.backup_to(self.directory / "missing-transition-pair.sqlite3", confirmed=True)

    def test_missing_creation_audit_and_operation_pair_is_rejected(self):
        events = {"opp": "draft_created", "plan": "task_planned", "order": "order_draft_created",
                  "invoice": "invoice_draft_created", "queue": "queue_enqueued", "lease": "queue_leased",
                  "workflow": "workflow_created", "observation": "payment_observation_created"}
        for kind, event in events.items():
            with self.subTest(kind=kind):
                resource = self.ids["a"]["queue" if kind == "lease" else kind]
                with self.state.transaction() as conn:
                    operation = tuple(conn.execute("SELECT * FROM state_operations WHERE operation_key=?", ("a-"+kind,)).fetchone())
                    audit = tuple(conn.execute("SELECT * FROM state_audit WHERE event=? AND resource_id=?", (event, resource)).fetchone())
                    conn.execute("DELETE FROM state_operations WHERE operation_key=?", ("a-"+kind,))
                    conn.execute("DELETE FROM state_audit WHERE id=?", (audit[0],))
                target = self.directory / f"missing-create-{kind}.sqlite3"
                try:
                    with self.assertRaises(StateStorageError):
                        self.state.backup_to(target, confirmed=True)
                    self.assertFalse(target.exists())
                finally:
                    with self.state.transaction() as conn:
                        conn.execute("INSERT INTO state_operations VALUES (?,?,?,?,?)", operation)
                        conn.execute("INSERT INTO state_audit VALUES (?,?,?,?,?,?)", audit)

    def test_duplicate_creation_event_and_operation_is_rejected(self):
        with self.state.transaction() as conn:
            audit = conn.execute("SELECT occurred_at, actor_id, event, resource_type, resource_id FROM state_audit WHERE event='order_draft_created' AND resource_id=?", (self.ids["a"]["order"],)).fetchone()
            operation = conn.execute("SELECT owner_id, request_hash, resource_type, resource_id FROM state_operations WHERE operation_key='a-order'").fetchone()
            conn.execute("INSERT INTO state_audit(occurred_at,actor_id,event,resource_type,resource_id) VALUES(?,?,?,?,?)", tuple(audit))
            conn.execute("INSERT INTO state_operations VALUES (?,?,?,?,?)", (operation[0], "duplicate", operation[1], operation[2], operation[3]))
        dest = self.directory / "duplicate-creation.sqlite3"
        with self.assertRaises(StateStorageError):
            self.state.backup_to(dest, confirmed=True)
        self.assertFalse(dest.exists())

    def test_creation_pair_corruption_in_copy_is_rejected(self):
        def corrupt(source, destination):
            _copy_database(source, destination)
            destination.execute("DELETE FROM state_operations WHERE operation_key='a-order'")
            destination.execute("DELETE FROM state_audit WHERE event='order_draft_created' AND resource_id=?", (self.ids["a"]["order"],))
            destination.commit()
        dest = self.directory / "bad-create-copy.sqlite3"
        with patch("ai_earn.state_storage._copy_database", side_effect=corrupt):
            with self.assertRaises(StateStorageError):
                self.state.backup_to(dest, confirmed=True)
        self.assertTrue(dest.exists())
        with self.assertRaises(StateStorageError):
            StateStorage(dest).backup_to(self.directory / "bad-create-restore.sqlite3", confirmed=True)
        with self.state.transaction() as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM state_operations WHERE operation_key='a-order'").fetchone()[0], 1)

    def test_creation_pair_checks_apply_in_v3_through_v9(self):
        for version in range(3, 10):
            with self.subTest(version=version):
                source = self.state if version == 9 else StateStorage(self.directory / f"v{version}.sqlite3")
                ident = OpportunityDrafts(self.auth, source).create(self.alice, "Version test", operation_key="version-test")
                with source.transaction() as conn:
                    conn.execute("DELETE FROM state_operations WHERE operation_key='version-test'")
                    conn.execute("DELETE FROM state_audit WHERE event='draft_created' AND resource_id=?", (ident,))
                target = self.directory / f"no-creation-v{version}.sqlite3"
                with self.assertRaises(StateStorageError):
                    source.backup_to(target, confirmed=True)
                self.assertFalse(target.exists())

    def test_missing_creation_pair_blocks_migration_before_backup(self):
        source = StateStorage(self.directory / "v8.sqlite3")
        ident = OpportunityDrafts(self.auth, source).create(self.alice, "Synthetic", operation_key="missing-create")
        with source.transaction() as conn:
            conn.execute("DELETE FROM state_operations WHERE operation_key='missing-create'")
            conn.execute("DELETE FROM state_audit WHERE event='draft_created' AND resource_id=?", (ident,))
        dest = self.directory / "blocked-missing-create.sqlite3"
        with self.assertRaises(StateStorageError):
            source.migrate_to_v9(dest, confirmed=True)
        self.assertFalse(dest.exists())
        with source.transaction() as conn:
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 8)
            self.assertEqual(conn.execute("SELECT count(*) FROM opportunity_drafts WHERE id=?", (ident,)).fetchone()[0], 1)

    def test_post_migration_missing_creation_audit_rolls_back(self):
        from ai_earn.state_storage import _apply_v3
        source = StateStorage(self.directory / "v2.sqlite3")
        def corrupt(conn):
            _apply_v3(conn)
            conn.execute("INSERT INTO opportunity_drafts VALUES (?,?,?,?,?,?)", ("f"*32, "a"*32, "Synthetic", "draft", 1, 1))
        dest = self.directory / "before-bad-upgrade.sqlite3"
        with patch("ai_earn.state_storage._apply_v3", side_effect=corrupt):
            with self.assertRaises(StateStorageError):
                source.migrate_to_v3(dest, confirmed=True)
        self.assertTrue(dest.exists())
        for checked in (source, StateStorage(dest)):
            with checked.transaction() as conn:
                self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 2)
                self.assertIsNone(conn.execute("SELECT name FROM sqlite_master WHERE name='opportunity_drafts'").fetchone())

    def test_missing_second_lease_pair_is_rejected(self):
        queue = TaskQueue(self.auth, self.state)
        ident = queue.enqueue(self.alice, "Two attempts", operation_key="new-queue", backoff_seconds=0)
        lease = queue.claim(self.alice, ident, operation_key="first-claim", lease_seconds=60)
        queue.retry(self.alice, ident, lease, operation_key="retry")
        queue.claim(self.alice, ident, operation_key="second-claim", lease_seconds=60)
        with self.state.transaction() as conn:
            event = conn.execute("SELECT id FROM state_audit WHERE event='queue_leased' AND resource_id=? ORDER BY id DESC LIMIT 1", (ident,)).fetchone()[0]
            conn.execute("DELETE FROM state_audit WHERE id=?", (event,))
            conn.execute("DELETE FROM state_operations WHERE operation_key='second-claim'")
        dest = self.directory / "missing-second-lease.sqlite3"
        with self.assertRaises(StateStorageError):
            self.state.backup_to(dest, confirmed=True)
        self.assertFalse(dest.exists())

    def test_reserved_audit_type_collision_rolls_back_upgrade(self):
        source = StateStorage(self.directory / "v6.sqlite3")
        with source.transaction() as conn:
            conn.execute("INSERT INTO state_audit(occurred_at, actor_id, event, resource_type, resource_id) VALUES(?,?,?,?,?)", (1, "a" * 32, "custom", "task_queue", "f" * 32))
        backup = self.directory / "audit-collision.sqlite3"
        with self.assertRaises(StateStorageError):
            source.migrate_to_v7(backup, confirmed=True)
        self.assertTrue(backup.exists())
        with source.transaction() as conn:
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 6)
            self.assertEqual(conn.execute("SELECT count(*) FROM state_audit WHERE event='custom'").fetchone()[0], 1)

    def test_unknown_operation_types_are_outside_check(self):
        with self.state.transaction() as conn:
            conn.execute("INSERT INTO state_operations VALUES (?,?,?,?,?)", ("a" * 32, "unknown", b"x" * 32, "future_type", "f" * 32))
        self.state.backup_to(self.directory / "unknown.sqlite3", confirmed=True)

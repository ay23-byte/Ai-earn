"""Disposable tests for the experimental local opportunity draft service."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from ai_earn.auth import AccessDenied, AuthService, SESSION_SECONDS
from ai_earn.auth_storage import AuthStorage
from ai_earn.opportunity_drafts import DraftError, OpportunityDrafts
from ai_earn.state_storage import StateStorage, StateStorageError


class Clock:
    now = 1_800_000_000
    def __call__(self):
        return self.now


@unittest.skipUnless(os.name in {"posix", "nt"}, "requires a supported private filesystem")
class OpportunityDraftTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.tmp.cleanup)
        directory = Path(self.tmp.name) / "private"
        if os.name == "nt":
            from ai_earn.windows_security import create_private_directory
            create_private_directory(directory)
        else:
            directory.mkdir(mode=0o700)
        self.directory = directory
        self.clock = Clock()
        cost_patch = patch("ai_earn.auth.PASSWORD_ITERATIONS", 1000)
        cost_patch.start()
        self.addCleanup(cost_patch.stop)
        self.auth = AuthService(AuthStorage(directory / "auth.sqlite3"), self.clock)
        for name in ("alice", "bobby"):
            self.auth.register(name, "disposable test password")
        self.alice = self.auth.login("alice", "disposable test password")
        self.bob = self.auth.login("bobby", "disposable test password")
        self.state = StateStorage(directory / "state.sqlite3", create=True, confirmed=True)
        self.state.migrate_to_v2(directory / "v1.sqlite3", confirmed=True)
        self.state.migrate_to_v3(directory / "v2.sqlite3", confirmed=True)
        self.service = OpportunityDrafts(self.auth, self.state)

    def test_create_read_archive_and_idempotent_retries(self):
        record_id = self.service.create(self.alice, "Example draft", operation_key="create-1")
        self.assertEqual(self.service.create(self.alice, "Example draft", operation_key="create-1"), record_id)
        record = self.service.get(self.alice, record_id)
        self.assertEqual((record["title"], record["status"]), ("Example draft", "draft"))
        self.assertEqual(self.service.archive(self.alice, record_id, operation_key="archive-1"), record_id)
        self.assertEqual(self.service.archive(self.alice, record_id, operation_key="archive-1"), record_id)
        self.assertEqual(self.service.create(self.alice, "Example draft", operation_key="create-1"), record_id)
        self.assertEqual(self.service.get(self.alice, record_id)["status"], "archived")
        with self.state.transaction() as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM opportunity_drafts").fetchone()[0], 1)
            self.assertEqual(conn.execute("SELECT count(*) FROM state_operations").fetchone()[0], 2)
            rows = conn.execute("SELECT event,actor_id,resource_id FROM state_audit ORDER BY id").fetchall()
            self.assertEqual([r[0] for r in rows], ["draft_created", "draft_archived"])
            self.assertTrue(all(row[2] == record_id for row in rows))
            self.assertNotIn(self.alice, str([tuple(r) for r in rows]))
            self.assertNotIn("Example draft", str([tuple(r) for r in rows]))

    def test_cross_account_access_is_denied_and_keys_are_owner_scoped(self):
        record_id = self.service.create(self.alice, "Alice draft", operation_key="same-key")
        bob_id = self.service.create(self.bob, "Bob draft", operation_key="same-key")
        self.assertNotEqual(record_id, bob_id)
        for op in (lambda: self.service.get(self.bob, record_id),
                   lambda: self.service.archive(self.bob, record_id, operation_key="attack"),
                   lambda: self.service.get(self.bob, "f" * 32)):
            with self.assertRaises(AccessDenied):
                op()
        self.assertEqual(self.service.get(self.alice, record_id)["status"], "draft")
        with self.state.transaction() as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM state_audit").fetchone()[0], 2)

    def test_reused_key_with_different_request_fails_without_mutation(self):
        record_id = self.service.create(self.alice, "Original", operation_key="same")
        with self.assertRaises(DraftError):
            self.service.create(self.alice, "Different", operation_key="same")
        with self.assertRaises(DraftError):
            self.service.archive(self.alice, record_id, operation_key="same")
        with self.state.transaction() as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM opportunity_drafts").fetchone()[0], 1)
            self.assertEqual(conn.execute("SELECT count(*) FROM state_audit").fetchone()[0], 1)
            self.assertEqual(conn.execute("SELECT count(*) FROM state_operations").fetchone()[0], 1)

    def test_invalid_transition_and_invalid_inputs(self):
        record_id = self.service.create(self.alice, "Draft", operation_key="create")
        self.service.archive(self.alice, record_id, operation_key="archive")
        with self.assertRaises(DraftError):
            self.service.archive(self.alice, record_id, operation_key="archive-again")
        for title in ("", "  ", "x" * 201, "hello\nworld", "\ud800", None):
            with self.subTest(title=repr(title)):
                with self.assertRaises(DraftError):
                    self.service.create(self.alice, title, operation_key="valid-key")
        for key in ("", "contains space", "a" * 129, None):
            with self.subTest(key=key):
                with self.assertRaises(DraftError):
                    self.service.create(self.alice, "Draft", operation_key=key)
        with self.assertRaises(DraftError):
            self.service.get(self.alice, "not-an-id")
        with self.state.transaction() as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM state_audit").fetchone()[0], 2)

    def test_revoked_expired_and_invalid_sessions_denied(self):
        self.auth.logout(self.bob)
        for token in (self.bob, "x" * 43, ""):
            with self.assertRaises(AccessDenied):
                self.service.create(token, "Draft", operation_key="key")
        self.clock.now += SESSION_SECONDS
        with self.assertRaises(AccessDenied):
            self.service.create(self.alice, "Draft", operation_key="key")
        with self.state.transaction() as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM opportunity_drafts").fetchone()[0], 0)

    def test_failure_after_record_write_rolls_back_all(self):
        with patch("ai_earn.opportunity_drafts._audit", side_effect=RuntimeError("failure")):
            with self.assertRaisesRegex(RuntimeError, "failure"):
                self.service.create(self.alice, "Draft", operation_key="create")
        with self.state.transaction() as conn:
            for table in ("opportunity_drafts", "state_audit", "state_operations"):
                self.assertEqual(conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0], 0)
        self.assertTrue(self.service.create(self.alice, "Draft", operation_key="create"))

    def test_failure_at_idempotency_write_rolls_back_record_and_audit(self):
        with patch("ai_earn.opportunity_drafts._remember", side_effect=RuntimeError("failure")):
            with self.assertRaisesRegex(RuntimeError, "failure"):
                self.service.create(self.alice, "Draft", operation_key="create")
        with self.state.transaction() as conn:
            for table in ("opportunity_drafts", "state_audit", "state_operations"):
                self.assertEqual(conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0], 0)
        self.assertTrue(self.service.create(self.alice, "Draft", operation_key="create"))

    def test_expiry_before_state_commit_rolls_back_everything(self):
        from ai_earn.opportunity_drafts import _audit

        def expire_after_audit(*args):
            _audit(*args)
            self.clock.now += SESSION_SECONDS

        with patch("ai_earn.opportunity_drafts._audit", side_effect=expire_after_audit):
            with self.assertRaises(AccessDenied):
                self.service.create(self.alice, "Draft", operation_key="create")
        with self.state.transaction() as conn:
            for table in ("opportunity_drafts", "state_audit", "state_operations"):
                self.assertEqual(conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0], 0)

    def test_ambiguous_auth_exit_retry_does_not_duplicate(self):
        original = self.auth.storage.transaction

        @contextmanager
        def fail_after_state_commit():
            with original() as conn:
                yield conn
                raise RuntimeError("simulated auth exit failure")

        with patch.object(self.auth.storage, "transaction", fail_after_state_commit):
            with self.assertRaisesRegex(RuntimeError, "auth exit failure"):
                self.service.create(self.alice, "Draft", operation_key="same")
        record_id = self.service.create(self.alice, "Draft", operation_key="same")
        self.assertEqual(self.service.get(self.alice, record_id)["title"], "Draft")
        with self.state.transaction() as conn:
            for table in ("opportunity_drafts", "state_audit", "state_operations"):
                self.assertEqual(conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0], 1)

    def test_archive_audit_failure_rolls_back_transition_and_key(self):
        record_id = self.service.create(self.alice, "Draft", operation_key="create")
        with patch("ai_earn.opportunity_drafts._audit", side_effect=RuntimeError("failure")):
            with self.assertRaisesRegex(RuntimeError, "failure"):
                self.service.archive(self.alice, record_id, operation_key="archive")
        self.assertEqual(self.service.get(self.alice, record_id)["status"], "draft")
        with self.state.transaction() as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM state_audit").fetchone()[0], 1)
            self.assertEqual(conn.execute("SELECT count(*) FROM state_operations").fetchone()[0], 1)
        self.assertEqual(self.service.archive(self.alice, record_id, operation_key="archive"), record_id)

    def test_migration_failure_rolls_back_schema_and_preserves_backup(self):
        prior = StateStorage(self.directory / "v2.sqlite3")
        backup = self.directory / "pre-failure.sqlite3"

        def fail(conn):
            conn.execute("CREATE TABLE opportunity_drafts (id TEXT)")
            raise RuntimeError("injected migration failure")

        with patch("ai_earn.state_storage._apply_v3", side_effect=fail):
            with self.assertRaisesRegex(RuntimeError, "migration failure"):
                prior.migrate_to_v3(backup, confirmed=True)
        for store in (prior, StateStorage(backup)):
            with store.transaction() as conn:
                self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 2)
                self.assertIsNone(conn.execute("SELECT name FROM sqlite_master WHERE name='opportunity_drafts'").fetchone())

    def test_concurrent_duplicate_create_has_one_effect(self):
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(self.service.create, self.alice, "Draft", operation_key="same") for _ in range(2)]
            ids = [f.result(timeout=10) for f in futures]
        self.assertEqual(ids[0], ids[1])
        with self.state.transaction() as conn:
            for table in ("opportunity_drafts", "state_audit", "state_operations"):
                self.assertEqual(conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0], 1)

    def test_schema_rejects_invalid_status_and_null_identifier(self):
        actor = self.auth.profile(self.alice)["id"]
        for identifier, status in (("a" * 32, "verified"), (None, "draft")):
            with self.subTest(identifier=identifier, status=status):
                with self.assertRaises(StateStorageError):
                    with self.state.transaction() as conn:
                        conn.execute("INSERT INTO opportunity_drafts VALUES (?, ?, ?, ?, ?, ?)",
                                     (identifier, actor, "Draft", status, self.clock.now, self.clock.now))
        with self.state.transaction() as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM opportunity_drafts").fetchone()[0], 0)

    def test_reopen_backup_restore_and_prior_version_refusal(self):
        record_id = self.service.create(self.alice, "Draft", operation_key="create")
        copy = self.directory / "state-copy.sqlite3"
        self.state.backup_to(copy, confirmed=True)
        restored = OpportunityDrafts(self.auth, StateStorage(copy))
        self.assertEqual(restored.get(self.alice, record_id)["title"], "Draft")
        prior = OpportunityDrafts(self.auth, StateStorage(self.directory / "v2.sqlite3"))
        with self.assertRaises(StateStorageError):
            prior.create(self.alice, "Draft", operation_key="key")

    def test_migration_is_explicit_non_overwriting_and_preserves_v2_backup(self):
        prior = StateStorage(self.directory / "v2.sqlite3")
        destination = self.directory / "another.sqlite3"
        with self.assertRaises(StateStorageError):
            prior.migrate_to_v3(destination)
        self.assertFalse(destination.exists())
        destination.write_bytes(b"keep")
        with self.assertRaises(StateStorageError):
            prior.migrate_to_v3(destination, confirmed=True)
        self.assertEqual(destination.read_bytes(), b"keep")
        with prior.transaction() as conn:
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 2)

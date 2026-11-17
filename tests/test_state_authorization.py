"""Disposable integration tests for the internal session-bound state unit of work."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from ai_earn.auth import AccessDenied, AuthService, SESSION_SECONDS
from ai_earn.auth_storage import AuthStorage
from ai_earn.state_authorization import _authorized_transaction
from ai_earn.state_storage import StateStorage, StateStorageError


class Clock:
    def __init__(self):
        self.now = 1_800_000_000

    def __call__(self):
        return self.now


@unittest.skipUnless(os.name in {"posix", "nt"}, "requires a supported private filesystem")
class StateAuthorizationTests(unittest.TestCase):
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
        self.auth_store = AuthStorage(directory / "auth.sqlite3")
        self.clock = Clock()
        # Test-only cost reduction: all other session and persistence logic is real.
        patcher = patch("ai_earn.auth.PASSWORD_ITERATIONS", 1000)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.auth = AuthService(self.auth_store, self.clock)
        self.password = "disposable test password"
        self.auth.register("alice", self.password)
        self.auth.register("bobby", self.password)
        self.alice = self.auth.login("alice", self.password)
        self.bob = self.auth.login("bobby", self.password)
        self.state = StateStorage(directory / "state.sqlite3", create=True, confirmed=True)
        self.state.migrate_to_v2(directory / "backup.sqlite3", confirmed=True)

    def test_session_actor_is_derived_from_authenticated_account(self):
        alice_id = self.auth.profile(self.alice)["id"]
        bob_id = self.auth.profile(self.bob)["id"]
        with _authorized_transaction(self.auth, self.state, self.alice) as (conn, actor, now):
            self.assertEqual(actor, alice_id)
            self.assertNotEqual(actor, bob_id)
            self.assertEqual(now, self.clock.now)
            conn.execute("INSERT INTO state_meta VALUES (?, ?)", ("test", "value"))
        with self.state.transaction() as conn:
            self.assertEqual(conn.execute("SELECT value FROM state_meta WHERE key='test'").fetchone()[0], "value")

    def test_invalid_expired_and_revoked_sessions_are_denied(self):
        for token in ("", "x" * 43, None):
            with self.subTest(token=token):
                with self.assertRaises(AccessDenied):
                    with _authorized_transaction(self.auth, self.state, token):
                        self.fail("Invalid session reached state")
        self.auth.logout(self.bob)
        with self.assertRaises(AccessDenied):
            with _authorized_transaction(self.auth, self.state, self.bob):
                self.fail("Revoked session reached state")
        self.clock.now += SESSION_SECONDS
        with self.assertRaises(AccessDenied):
            with _authorized_transaction(self.auth, self.state, self.alice):
                self.fail("Expired session reached state")
        with self.state.transaction() as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM state_meta").fetchone()[0], 0)

    def test_exception_rolls_back_state(self):
        with self.assertRaisesRegex(RuntimeError, "failed operation"):
            with _authorized_transaction(self.auth, self.state, self.alice) as (conn, actor, now):
                conn.execute("INSERT INTO state_meta VALUES ('test', 'value')")
                raise RuntimeError("failed operation")
        with self.state.transaction() as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM state_meta").fetchone()[0], 0)

    def test_auth_exit_failure_can_leave_committed_state(self):
        # Two databases are not one atomic transaction. Future writers must
        # use durable idempotency to handle an ambiguous reported outcome.
        store = self.auth_store

        class ExitFailure:
            path = store.path

            @contextmanager
            def transaction(self):
                with store.transaction() as conn:
                    yield conn
                    raise RuntimeError("simulated auth exit failure")

        service = AuthService(ExitFailure(), self.clock)
        with self.assertRaisesRegex(RuntimeError, "auth exit failure"):
            with _authorized_transaction(service, self.state, self.alice) as (conn, actor, now):
                conn.execute("INSERT INTO state_meta VALUES ('ambiguous', 'committed')")
        with self.state.transaction() as conn:
            self.assertEqual(conn.execute("SELECT value FROM state_meta WHERE key='ambiguous'").fetchone()[0], "committed")

    def test_expiry_during_operation_rolls_back_state(self):
        with self.assertRaises(AccessDenied):
            with _authorized_transaction(self.auth, self.state, self.alice) as (conn, actor, now):
                conn.execute("INSERT INTO state_meta VALUES ('test', 'value')")
                self.clock.now += SESSION_SECONDS
        with self.state.transaction() as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM state_meta").fetchone()[0], 0)

    def test_recovery_revokes_existing_session(self):
        recovery = self.auth.register("carol", self.password)
        token = self.auth.login("carol", self.password)
        self.auth.recover("carol", recovery, "new disposable password")
        with self.assertRaises(AccessDenied):
            with _authorized_transaction(self.auth, self.state, token):
                self.fail("Recovered account's old session was accepted")

    def test_missing_auth_file_is_not_recreated(self):
        self.auth_store.path.unlink()
        with self.assertRaises(StateStorageError):
            with _authorized_transaction(self.auth, self.state, self.alice):
                self.fail("Missing auth database was accepted")
        self.assertFalse(self.auth_store.path.exists())

    def test_schema_v1_is_refused_without_changes(self):
        v1 = StateStorage(self.directory / "v1.sqlite3", create=True, confirmed=True)
        with self.assertRaises(StateStorageError):
            with _authorized_transaction(self.auth, v1, self.alice):
                self.fail("Unsupported state schema was exposed")
        with v1.transaction() as conn:
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 1)
            self.assertEqual(conn.execute("SELECT count(*) FROM state_meta").fetchone()[0], 0)

    def test_same_database_is_refused(self):
        # AuthStorage accepts the version-1 state file, but this unit of work
        # must never attempt nested locks on a single file.
        v1 = StateStorage(self.directory / "v1.sqlite3", create=True, confirmed=True)
        counterfeit = AuthService(AuthStorage(v1.path, create=False), self.clock)
        with self.assertRaises(StateStorageError):
            with _authorized_transaction(counterfeit, v1, self.alice):
                self.fail("The same file was accepted twice")

    @unittest.skipUnless(os.name == "posix", "POSIX hard-link test")
    def test_same_database_hard_link_is_refused(self):
        v1 = StateStorage(self.directory / "v1.sqlite3", create=True, confirmed=True)
        alias = self.directory / "alias.sqlite3"
        os.link(v1.path, alias)
        counterfeit = AuthService(AuthStorage(alias, create=False), self.clock)
        with self.assertRaises(StateStorageError):
            with _authorized_transaction(counterfeit, v1, self.alice):
                self.fail("A hard link to the same file was accepted")

    def test_expiry_is_checked_after_waiting_for_state_lock(self):
        entered = threading.Event()
        original = self.state.transaction

        @contextmanager
        def notifying_transaction():
            entered.set()
            with original() as conn:
                yield conn

        def attempt():
            with _authorized_transaction(self.auth, self.state, self.alice):
                self.fail("Expired while waiting for state lock")

        with ThreadPoolExecutor(max_workers=1) as pool:
            with original():
                with patch.object(self.state, "transaction", notifying_transaction):
                    future = pool.submit(attempt)
                    self.assertTrue(entered.wait(5))
                    self.clock.now += SESSION_SECONDS
            with self.assertRaises(AccessDenied):
                future.result(timeout=10)

    def test_logout_waits_for_in_flight_state_transaction(self):
        started = threading.Event()
        done = threading.Event()

        def logout():
            started.set()
            self.auth.logout(self.alice)
            done.set()

        with ThreadPoolExecutor(max_workers=1) as pool:
            with _authorized_transaction(self.auth, self.state, self.alice) as (conn, actor, now):
                future = pool.submit(logout)
                self.assertTrue(started.wait(5))
                self.assertFalse(done.wait(0.1))
                conn.execute("INSERT INTO state_meta VALUES ('before_logout', 'value')")
            future.result(timeout=10)
        with self.assertRaises(AccessDenied):
            with _authorized_transaction(self.auth, self.state, self.alice):
                self.fail("Logged-out session reached state")
        with self.state.transaction() as conn:
            self.assertEqual(conn.execute("SELECT value FROM state_meta WHERE key='before_logout'").fetchone()[0], "value")

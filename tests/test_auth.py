import concurrent.futures
from contextlib import contextmanager
import hashlib
import os
from pathlib import Path
from unittest.mock import patch
import sqlite3
import tempfile
import threading
import unittest

from ai_earn.auth import AccessDenied, AuthError, AuthService, RateLimitError, SESSION_SECONDS, WINDOW_SECONDS
from ai_earn.auth_storage import AuthStorage, StorageError


class Clock:
    def __init__(self):
        self.now = 1_800_000_000

    def __call__(self):
        return self.now


@unittest.skipUnless(os.name == "posix", "account storage requires verified POSIX permissions")
class AuthenticationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "auth.sqlite3"
        self.clock = Clock()
        self.store = AuthStorage(self.path)
        self.service = AuthService(self.store, self.clock)
        self.password = "test password long enough"

    def test_register_login_logout_and_hash_storage(self):
        recovery = self.service.register("alice", self.password)
        token = self.service.login("alice", self.password)
        profile = self.service.profile(token)
        self.assertEqual(profile["username"], "alice")
        with self.store.transaction() as conn:
            account = conn.execute("SELECT * FROM accounts").fetchone()
            session = conn.execute("SELECT * FROM sessions").fetchone()
            self.assertNotEqual(bytes(account["password_hash"]), self.password.encode())
            self.assertGreaterEqual(account["password_iterations"], 600_000)
            self.assertEqual(session["token_hash"], hashlib.sha256(token.encode()).digest())
            self.assertEqual(account["recovery_hash"], hashlib.sha256(recovery.encode()).digest())
            audit = str([tuple(row) for row in conn.execute("SELECT * FROM auth_audit")])
            for secret in (self.password, recovery, token):
                self.assertNotIn(secret, audit)
        self.service.logout(token)
        with self.assertRaises(AccessDenied):
            self.service.profile(token)

    def test_password_salts_differ(self):
        self.service.register("alice", self.password)
        self.service.register("bobby", self.password)
        with self.store.transaction() as conn:
            rows = conn.execute("SELECT password_salt,password_hash FROM accounts ORDER BY username").fetchall()
        self.assertNotEqual(rows[0][0], rows[1][0])
        self.assertNotEqual(rows[0][1], rows[1][1])

    def test_unauthorized_cross_account_and_expiration(self):
        self.service.register("alice", self.password)
        self.service.register("bobby", self.password)
        alice = self.service.login("alice", self.password)
        bob = self.service.login("bobby", self.password)
        bob_id = self.service.profile(bob)["id"]
        with self.assertRaises(AccessDenied):
            self.service.profile(alice, bob_id)
        with self.assertRaises(AccessDenied):
            self.service.profile("")
        self.clock.now += SESSION_SECONDS
        with self.assertRaises(AccessDenied):
            self.service.profile(alice)

    def test_session_expiry_is_checked_after_transaction_lock(self):
        self.service.register("alice", self.password)
        tokens = [self.service.login("alice", self.password) for _ in range(3)]
        start = self.clock.now
        store, clock = self.store, self.clock

        class DelayedStorage:
            @contextmanager
            def transaction(self):
                with store.transaction() as conn:
                    clock.now += SESSION_SECONDS
                    yield conn

        delayed = AuthService(DelayedStorage(), clock)
        for operation in (
            lambda: delayed.profile(tokens[0]),
            lambda: delayed.logout(tokens[1]),
            lambda: delayed.replace_recovery_code(tokens[2], confirmed=True),
        ):
            with self.subTest(operation=operation):
                self.clock.now = start
                with self.assertRaises(AccessDenied):
                    operation()

    def test_session_expiry_after_actual_sqlite_lock_wait(self):
        self.service.register("alice", self.password)
        token = self.service.login("alice", self.password)
        started = threading.Event()
        store = self.store

        class ContendedStorage:
            @contextmanager
            def transaction(self):
                started.set()
                with store.transaction() as conn:
                    yield conn

        delayed = AuthService(ContendedStorage(), self.clock)
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            with self.store.transaction():
                future = pool.submit(delayed.profile, token)
                self.assertTrue(started.wait(timeout=3))
                self.clock.now += SESSION_SECONDS
            with self.assertRaises(AccessDenied):
                future.result(timeout=10)

    def test_login_session_expiry_uses_transaction_time(self):
        self.service.register("alice", self.password)
        store, clock = self.store, self.clock

        class DelayedStorage:
            @contextmanager
            def transaction(self):
                with store.transaction() as conn:
                    clock.now += SESSION_SECONDS
                    yield conn

        token = AuthService(DelayedStorage(), clock).login("alice", self.password)
        self.assertEqual(self.service.profile(token)["username"], "alice")

    def test_rate_limit_window_uses_transaction_time(self):
        self.service.register("alice", self.password)
        for _ in range(5):
            with self.assertRaises(AuthError):
                self.service.login("alice", "wrong password long")
        store, clock = self.store, self.clock

        class DelayedStorage:
            @contextmanager
            def transaction(self):
                with store.transaction() as conn:
                    clock.now += WINDOW_SECONDS
                    yield conn

        token = AuthService(DelayedStorage(), clock).login("alice", self.password)
        self.assertEqual(self.service.profile(token)["username"], "alice")

    def test_recovery_is_one_time_rotated_and_revokes_sessions(self):
        recovery = self.service.register("alice", self.password)
        token = self.service.login("alice", self.password)
        updated = self.service.recover("alice", recovery, "new password long enough")
        self.assertNotEqual(recovery, updated)
        with self.assertRaises(AccessDenied):
            self.service.profile(token)
        with self.assertRaises(AuthError):
            self.service.recover("alice", recovery, "another long password")
        with self.assertRaises(AuthError):
            self.service.login("alice", self.password)
        new_token = self.service.login("alice", "new password long enough")
        self.assertEqual(self.service.profile(new_token)["username"], "alice")

    def test_authenticated_recovery_replacement_is_owned_and_invalidates_old_code(self):
        old = self.service.register("alice", self.password)
        bob_old = self.service.register("bobby", self.password)
        alice = self.service.login("alice", self.password)
        replacement = self.service.replace_recovery_code(alice, confirmed=True)
        self.assertNotEqual(old, replacement)
        with self.assertRaises(AuthError):
            self.service.recover("alice", old, "another long password")
        self.assertTrue(self.service.recover("bobby", bob_old, "another long password"))
        self.assertTrue(self.service.recover("alice", replacement, "another long password"))
        with self.store.transaction() as conn:
            audit = str([tuple(row) for row in conn.execute("SELECT * FROM auth_audit")])
        self.assertIn("recovery_code_replaced", audit)
        self.assertNotIn(replacement, audit)
        self.assertNotIn(alice, audit)

    def test_recovery_replacement_requires_explicit_confirmation(self):
        old = self.service.register("alice", self.password)
        token = self.service.login("alice", self.password)
        with self.assertRaises(AuthError):
            self.service.replace_recovery_code(token)
        with self.assertRaises(AuthError):
            self.service.replace_recovery_code(token, confirmed=1)
        self.assertTrue(self.service.recover("alice", old, "another long password"))

    def test_recovery_replacement_requires_live_session(self):
        self.service.register("alice", self.password)
        with self.assertRaises(AccessDenied):
            self.service.replace_recovery_code("", confirmed=True)
        token = self.service.login("alice", self.password)
        self.service.logout(token)
        with self.assertRaises(AccessDenied):
            self.service.replace_recovery_code(token, confirmed=True)
        token = self.service.login("alice", self.password)
        self.clock.now += SESSION_SECONDS
        with self.assertRaises(AccessDenied):
            self.service.replace_recovery_code(token, confirmed=True)

    def test_lost_replacement_can_be_replaced_again_after_login(self):
        self.service.register("alice", self.password)
        token = self.service.login("alice", self.password)
        lost = self.service.replace_recovery_code(token, confirmed=True)  # Simulate failure before display.
        self.service.logout(token)
        token = self.service.login("alice", self.password)
        replacement = self.service.replace_recovery_code(token, confirmed=True)
        with self.assertRaises(AuthError):
            self.service.recover("alice", lost, "another long password")
        self.assertTrue(self.service.recover("alice", replacement, "another long password"))

    def test_failed_replacement_transaction_preserves_previous_code(self):
        original = self.service.register("alice", self.password)
        token = self.service.login("alice", self.password)
        with patch("ai_earn.auth._audit", side_effect=RuntimeError("simulated transaction failure")):
            with self.assertRaises(RuntimeError):
                self.service.replace_recovery_code(token, confirmed=True)
        self.assertTrue(self.service.recover("alice", original, "another long password"))

    def test_recovery_replacement_rate_limit_persists_and_resets(self):
        self.service.register("alice", self.password)
        token = self.service.login("alice", self.password)
        for _ in range(5):
            self.service.replace_recovery_code(token, confirmed=True)
        reopened = AuthService(AuthStorage(self.path), self.clock)
        with self.assertRaises(RateLimitError):
            reopened.replace_recovery_code(token, confirmed=True)
        self.clock.now += WINDOW_SECONDS
        self.assertTrue(reopened.replace_recovery_code(token, confirmed=True))

    def test_invalid_credentials_do_not_log_in_with_dummy_password(self):
        self.service.register("alice", "invalid credential")
        with self.assertRaises(AuthError):
            self.service.login("alice", "")
        with self.assertRaises(AuthError):
            self.service.login("nobody", "invalid credential")

    def test_invalid_input_and_sql_injection(self):
        for username in ("x", "ALICE", "x' OR 1=1--", "a" * 33):
            with self.subTest(username=username), self.assertRaises(AuthError):
                self.service.register(username, self.password)
        with self.assertRaises(AuthError):
            self.service.register("alice", "short")
        with self.assertRaises(AuthError):
            self.service.register("alice", "x" * 257)

    def test_persistent_rate_limit_and_time_window(self):
        self.service.register("alice", self.password)
        for _ in range(5):
            with self.assertRaises(AuthError):
                self.service.login("alice", "wrong password long")
        reopened = AuthService(AuthStorage(self.path), self.clock)
        with self.assertRaises(RateLimitError):
            reopened.login("alice", self.password)
        self.clock.now += WINDOW_SECONDS
        self.assertEqual(reopened.profile(reopened.login("alice", self.password))["username"], "alice")

    def test_recovery_rate_limit_and_bounded_rejection_audit(self):
        recovery = self.service.register("alice", self.password)
        for _ in range(5):
            with self.assertRaises(AuthError):
                self.service.recover("alice", "invalid-code", "another secure password")
        for _ in range(12):
            with self.assertRaises(RateLimitError):
                self.service.recover("alice", recovery, "another secure password")
        with self.store.transaction() as conn:
            count = conn.execute("SELECT count(*) FROM auth_audit WHERE event='rate_limited'").fetchone()[0]
        self.assertEqual(count, 1)
        self.clock.now += WINDOW_SECONDS
        self.assertTrue(self.service.recover("alice", recovery, "another secure password"))

    def test_registration_rate_limit(self):
        self.service.register("alice", self.password)
        for _ in range(9):
            with self.assertRaises(AuthError):
                self.service.register("alice", self.password)
        with self.assertRaises(RateLimitError):
            self.service.register("alice", self.password)

    def test_concurrent_attempts_cannot_bypass_limit(self):
        self.service.register("alice", self.password)
        for _ in range(4):
            with self.assertRaises(AuthError):
                self.service.login("alice", "wrong password long")
        def attempt():
            try:
                self.service.login("alice", "wrong password long")
            except RateLimitError:
                return "limited"
            except AuthError:
                return "invalid"
            return "unexpected"
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: attempt(), range(2)))
        self.assertCountEqual(results, ["invalid", "limited"])

    def test_sqlite_backup_to_separate_file_restores_auth(self):
        self.service.register("alice", self.password)
        original_token = self.service.login("alice", self.password)
        backup_path = Path(self.tmp.name) / "backup.sqlite3"
        source = sqlite3.connect(self.path)
        destination = sqlite3.connect(backup_path)
        try:
            source.backup(destination)
        finally:
            destination.close()
            source.close()
        if os.name == "posix":
            os.chmod(backup_path, 0o600)
        restored_store = AuthStorage(backup_path)
        restored = AuthService(restored_store, self.clock)
        self.assertEqual(restored.profile(original_token)["username"], "alice")  # A stale backup can revive a session.
        with restored_store.transaction() as conn:
            conn.execute("UPDATE sessions SET revoked_at=? WHERE revoked_at IS NULL", (self.clock.now,))
        with self.assertRaises(AccessDenied):
            restored.profile(original_token)
        self.assertEqual(restored.profile(restored.login("alice", self.password))["username"], "alice")

    def test_unknown_database_version_is_not_overwritten(self):
        other = Path(self.tmp.name) / "other.sqlite3"
        connection = sqlite3.connect(other)
        connection.execute("CREATE TABLE important (value TEXT)")
        connection.execute("INSERT INTO important VALUES ('preserve')")
        connection.commit()
        connection.close()
        if os.name == "posix":
            os.chmod(other, 0o600)
        with self.assertRaises(StorageError):
            AuthStorage(other)
        with sqlite3.connect(other) as check:
            self.assertEqual(check.execute("SELECT value FROM important").fetchone()[0], "preserve")

    @unittest.skipUnless(os.name == "posix", "POSIX permissions test")
    def test_unsafe_permissions_and_symlink_rejected(self):
        os.chmod(self.path, 0o644)
        with self.assertRaises(StorageError):
            AuthStorage(self.path)
        os.chmod(self.path, 0o600)
        link = Path(self.tmp.name) / "link.sqlite3"
        link.symlink_to(self.path)
        with self.assertRaises(StorageError):
            AuthStorage(link)

    @unittest.skipUnless(os.name == "posix", "POSIX permissions test")
    def test_public_parent_and_unsupported_platform_are_rejected(self):
        other = Path(self.tmp.name) / "unprotected"
        other.mkdir(mode=0o755)
        os.chmod(other, 0o755)
        with self.assertRaises(StorageError):
            AuthStorage(other / "auth.sqlite3")
        self.assertFalse((other / "auth.sqlite3").exists())
        with patch("ai_earn.auth_storage.os.name", "unsupported"):
            with self.assertRaises(StorageError):
                AuthStorage(self.path)

    def test_missing_database_during_use_fails_closed(self):
        self.path.unlink()
        with self.assertRaises(StorageError):
            self.service.profile("a" * 43)
        self.assertFalse(self.path.exists())

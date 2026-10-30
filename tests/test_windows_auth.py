"""Real Windows ACL integration using the unpatched development path."""
from contextlib import closing, contextmanager
import io
import os
import re
from pathlib import Path
import sqlite3
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from ai_earn import auth_storage
from ai_earn.auth import AccessDenied, AuthError, AuthService, RateLimitError, SESSION_SECONDS
from ai_earn.auth_cli import run
from ai_earn.auth_storage import AuthStorage, StorageError
from ai_earn.windows_security import WindowsSecurityError, create_private_directory, verify_private_path


class WindowsGateDefaultTests(unittest.TestCase):
    def test_windows_development_path_is_enabled(self):
        self.assertIs(auth_storage._windows_auth_verified(), True)


@unittest.skipUnless(os.name == "nt", "requires actual Windows Win32 APIs and a local ACL-capable drive")
class WindowsAuthenticationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.directory = Path(self.tmp.name) / "private"
        create_private_directory(self.directory)
        self.path = self.directory / "auth.sqlite3"
        # Use the application's normal Windows path, with no gate override.
        self.storage = AuthStorage(self.path)
        self.service = AuthService(self.storage)
        self.password = "example-password-for-test"

    def test_acl_and_auth_isolation_recovery_revocation(self):
        verify_private_path(self.directory, directory=True)
        verify_private_path(self.path, directory=False)
        recovery = self.service.register("alice", self.password)
        self.service.register("bobby", self.password)
        alice = self.service.login("alice", self.password)
        bob = self.service.login("bobby", self.password)
        bob_id = self.service.profile(bob)["id"]
        with self.assertRaises(AccessDenied):
            self.service.profile(alice, bob_id)
        with self.assertRaises(AccessDenied):
            self.service.profile("")
        replacement = self.service.recover("alice", recovery, "new-example-password")
        self.assertNotEqual(recovery, replacement)
        with self.assertRaises(AccessDenied):
            self.service.profile(alice)
        new_session = self.service.login("alice", "new-example-password")
        self.service.logout(new_session)
        with self.assertRaises(AccessDenied):
            self.service.profile(new_session)

    def test_session_expiry_and_persistent_login_rate_limit(self):
        now = [1_800_000_000]
        service = AuthService(self.storage, lambda: now[0])
        service.register("alice", self.password)
        token = service.login("alice", self.password)
        now[0] += SESSION_SECONDS
        with self.assertRaises(AccessDenied):
            service.profile(token)
        for _ in range(5):
            with self.assertRaises(AuthError):
                service.login("alice", "wrong password long")
        with self.assertRaises(RateLimitError):
            AuthService(AuthStorage(self.path), lambda: now[0]).login("alice", self.password)

    def test_session_expiry_uses_time_after_transaction_lock(self):
        now = [1_800_000_000]
        clock = lambda: now[0]
        service = AuthService(self.storage, clock)
        service.register("alice", self.password)
        tokens = [service.login("alice", self.password) for _ in range(3)]
        start = now[0]
        store = self.storage

        class DelayedStorage:
            @contextmanager
            def transaction(self):
                with store.transaction() as conn:
                    now[0] += SESSION_SECONDS
                    yield conn

        delayed = AuthService(DelayedStorage(), clock)
        for operation in (
            lambda: delayed.profile(tokens[0]),
            lambda: delayed.logout(tokens[1]),
            lambda: delayed.replace_recovery_code(tokens[2], confirmed=True),
        ):
            now[0] = start
            with self.assertRaises(AccessDenied):
                operation()

    def test_cli_registration_and_login_with_hidden_input_mocked(self):
        class Terminal(io.StringIO):
            def isatty(self):
                return True
        env = {"AI_EARN_AUTH_DB": str(self.path)}
        out, err = Terminal(), io.StringIO()
        with patch("ai_earn.auth_cli._secret", side_effect=[self.password, self.password]):
            self.assertEqual(run("register", env, Terminal("alice\n"), out, err), 0)
        self.assertIn("Account created", out.getvalue())
        self.assertNotIn(self.password, out.getvalue() + err.getvalue())
        out, err = Terminal(), io.StringIO()
        with patch("ai_earn.auth_cli._secret", return_value=self.password):
            self.assertEqual(run("login", env, Terminal("alice\n\n"), out, err), 0)
        self.assertIn("Signed out", out.getvalue())
        self.assertNotIn(self.password, out.getvalue() + err.getvalue())

    def test_recovery_code_replacement_requires_confirmation_and_live_session(self):
        original = self.service.register("alice", self.password)
        session = self.service.login("alice", self.password)
        with self.assertRaises(AuthError):
            self.service.replace_recovery_code(session)
        replacement = self.service.replace_recovery_code(session, confirmed=True)
        with self.assertRaises(AuthError):
            self.service.recover("alice", original, "new-example-password")
        self.assertTrue(self.service.recover("alice", replacement, "new-example-password"))
        with self.assertRaises(AccessDenied):
            self.service.replace_recovery_code(session, confirmed=True)

    def test_cli_recovery_replacement(self):
        class Terminal(io.StringIO):
            def isatty(self):
                return True
        old = self.service.register("alice", self.password)
        out, err = Terminal(), io.StringIO()
        with patch("ai_earn.auth_cli._secret", return_value=self.password):
            self.assertEqual(run("login", {"AI_EARN_AUTH_DB": str(self.path)}, Terminal("alice\nr\nYES\n"), out, err), 0)
        codes = re.findall(r"(?m)^([A-Za-z0-9_-]{43})$", out.getvalue())
        self.assertEqual(len(codes), 1)
        with self.assertRaises(AuthError):
            self.service.recover("alice", old, "new-example-password")
        self.assertTrue(self.service.recover("alice", codes[0], "new-example-password"))

    def test_sidecar_inherits_private_acl_and_backup_restores(self):
        self.service.register("alice", self.password)
        with self.storage.transaction() as conn:
            conn.execute("INSERT INTO auth_audit(occurred_at,event) VALUES(?,?)", (123, "test_event"))
            journal = self.path.with_name(self.path.name + "-journal")
            self.assertTrue(journal.is_file(), "SQLite rollback journal was not observed")
            verify_private_path(journal, directory=False)
        backup = self.directory / "backup.sqlite3"
        with closing(sqlite3.connect(self.path)) as src, closing(sqlite3.connect(backup)) as dst:
            src.backup(dst)
        verify_private_path(backup, directory=False)
        restored = AuthService(AuthStorage(backup))
        token = restored.login("alice", self.password)
        self.assertEqual(restored.profile(token)["username"], "alice")

    def test_unsafe_directory_acl_fails_closed(self):
        subprocess.run(["icacls.exe", str(self.directory), "/grant", "*S-1-1-0:(OI)(CI)F"], check=True, capture_output=True)
        with self.assertRaises(StorageError):
            AuthStorage(self.path)

    def test_unsafe_file_acl_fails_closed(self):
        subprocess.run(["icacls.exe", str(self.path), "/grant", "*S-1-1-0:F"], check=True, capture_output=True)
        with self.assertRaises(StorageError):
            AuthStorage(self.path)

    def test_existing_directory_is_never_replaced(self):
        marker = self.directory / "marker.txt"
        marker.write_text("preserve", encoding="utf-8")
        with self.assertRaises(WindowsSecurityError):
            create_private_directory(self.directory)
        self.assertEqual(marker.read_text(encoding="utf-8"), "preserve")

    def test_junction_is_rejected(self):
        junction = Path(self.tmp.name) / "junction"
        subprocess.run(["cmd.exe", "/d", "/c", "mklink", "/J", str(junction), str(self.directory)], check=True, capture_output=True)
        with self.assertRaises(WindowsSecurityError):
            verify_private_path(junction, directory=True)

    def test_unpatched_windows_auth_uses_private_storage(self):
        recovery = self.service.register("alice", self.password)
        token = AuthService(AuthStorage(self.path)).login("alice", self.password)
        self.assertEqual(self.service.profile(token)["username"], "alice")
        self.assertTrue(recovery)

    def test_windows_cli_rejects_unsafe_acl_before_creating_database(self):
        class Terminal(io.StringIO):
            def isatty(self):
                return True

        subprocess.run(["icacls.exe", str(self.directory), "/grant", "*S-1-1-0:(OI)(CI)F"], check=True, capture_output=True)
        path = self.directory / "unsafe.sqlite3"
        out, err = Terminal(), io.StringIO()
        with patch("ai_earn.auth_cli._secret") as secret:
            code = run("register", {"AI_EARN_AUTH_DB": str(path)}, Terminal(), out, err)
        self.assertEqual(code, 2)
        self.assertIn("not private", err.getvalue())
        self.assertFalse(path.exists())
        secret.assert_not_called()

    def test_windows_login_and_recovery_do_not_create_or_initialize_database(self):
        class Terminal(io.StringIO):
            def isatty(self):
                return True

        missing = self.directory / "missing.sqlite3"
        empty = self.directory / "empty.sqlite3"
        empty.touch()
        for action in ("login", "recover"):
            for path in (missing, empty):
                with self.subTest(action=action, path=path.name):
                    out, err = Terminal(), io.StringIO()
                    with patch("ai_earn.auth_cli._secret") as secret:
                        code = run(action, {"AI_EARN_AUTH_DB": str(path)}, Terminal(), out, err)
                    self.assertEqual(code, 2)
                    self.assertIn("database", err.getvalue().lower())
                    secret.assert_not_called()
                    self.assertFalse(missing.exists())
                    self.assertEqual(empty.stat().st_size, 0)

    def test_windows_file_removed_after_check_is_not_recreated(self):
        ensure = self.storage._ensure_file
        def remove_after_check(*args, **kwargs):
            ensure(*args, **kwargs)
            self.path.unlink()
        with patch.object(self.storage, "_ensure_file", side_effect=remove_after_check):
            with self.assertRaises(sqlite3.Error):
                self.service.profile("a" * 43)
        self.assertFalse(self.path.exists())

    def test_windows_database_uri_handles_special_characters(self):
        path = self.directory / "db # 100%.sqlite3"
        service = AuthService(AuthStorage(path))
        service.register("alice", self.password)
        existing = AuthService(AuthStorage(path, create=False))
        self.assertEqual(existing.profile(existing.login("alice", self.password))["username"], "alice")

    def test_windows_production_cli_rejects_before_creating_database(self):
        class Terminal(io.StringIO):
            def isatty(self):
                return True

        path = self.directory / "production.sqlite3"
        out, err = Terminal(), io.StringIO()
        with patch("ai_earn.auth_cli._secret") as secret:
            code = run("register", {"AI_EARN_AUTH_DB": str(path), "AI_EARN_ENV": "production"}, Terminal(), out, err)
        self.assertEqual(code, 2)
        self.assertIn("not enabled for production", err.getvalue())
        self.assertFalse(path.exists())
        secret.assert_not_called()

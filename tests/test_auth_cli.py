import io
import os
import re
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from ai_earn.auth import AuthError, AuthService
from ai_earn.auth_cli import run
from ai_earn.auth_storage import AuthStorage


class Terminal(io.StringIO):
    def isatty(self):
        return True


@unittest.skipUnless(os.name == "posix", "account storage requires verified POSIX permissions")
class AuthCliTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.env = {"AI_EARN_AUTH_DB": str(Path(self.temp.name) / "auth.sqlite3")}

    def test_non_terminal_is_rejected_before_database_is_created(self):
        out, err = io.StringIO(), io.StringIO()
        self.assertEqual(run("register", self.env, io.StringIO(), out, err), 2)
        self.assertIn("interactive terminal", err.getvalue())
        self.assertFalse(Path(self.env["AI_EARN_AUTH_DB"]).exists())

    def test_register_and_login_interactively(self):
        out, err = Terminal(), io.StringIO()
        with patch("ai_earn.auth_cli._secret", side_effect=["test password long enough"] * 2):
            self.assertEqual(run("register", self.env, Terminal("alice\n"), out, err), 0)
        self.assertIn("Account created", out.getvalue())
        self.assertNotIn("test password", out.getvalue() + err.getvalue())
        out, err = Terminal(), io.StringIO()
        with patch("ai_earn.auth_cli._secret", return_value="test password long enough"):
            self.assertEqual(run("login", self.env, Terminal("alice\n\n"), out, err), 0)
        self.assertIn("Signed in as alice", out.getvalue())
        self.assertIn("Signed out", out.getvalue())
        self.assertNotIn("test password", out.getvalue() + err.getvalue())

    def test_recovery_replacement_requires_confirmation(self):
        service = AuthService(AuthStorage(Path(self.env["AI_EARN_AUTH_DB"])))
        original = service.register("alice", "test password long enough")
        out, err = Terminal(), io.StringIO()
        with patch("ai_earn.auth_cli._secret", return_value="test password long enough"):
            self.assertEqual(run("login", self.env, Terminal("alice\nr\nNO\n"), out, err), 0)
        self.assertIn("Recovery code unchanged", out.getvalue())
        self.assertTrue(service.recover("alice", original, "new test password long"))

    def test_confirmed_recovery_replacement_is_visible_once(self):
        service = AuthService(AuthStorage(Path(self.env["AI_EARN_AUTH_DB"])))
        original = service.register("alice", "test password long enough")
        out, err = Terminal(), io.StringIO()
        with patch("ai_earn.auth_cli._secret", return_value="test password long enough"):
            self.assertEqual(run("login", self.env, Terminal("alice\nr\nYES\n"), out, err), 0)
        codes = re.findall(r"(?m)^([A-Za-z0-9_-]{43})$", out.getvalue())
        self.assertEqual(len(codes), 1)
        with self.assertRaises(AuthError):
            service.recover("alice", original, "new test password long")
        self.assertTrue(service.recover("alice", codes[0], "new test password long"))
        self.assertNotIn(codes[0], err.getvalue())

    def test_output_failure_after_replacement_can_be_retried(self):
        class FailingTerminal(Terminal):
            def write(self, text):
                if re.fullmatch(r"[A-Za-z0-9_-]{43}\n", text):
                    raise OSError("do not echo this diagnostic")
                return super().write(text)
        service = AuthService(AuthStorage(Path(self.env["AI_EARN_AUTH_DB"])))
        original = service.register("alice", "test password long enough")
        out, err = FailingTerminal(), io.StringIO()
        with patch("ai_earn.auth_cli._secret", return_value="test password long enough"):
            self.assertEqual(run("login", self.env, Terminal("alice\nr\nYES\n"), out, err), 1)
        self.assertIn("may have changed", err.getvalue())
        self.assertNotIn("do not echo this diagnostic", err.getvalue())
        token = service.login("alice", "test password long enough")
        replacement = service.replace_recovery_code(token, confirmed=True)
        with self.assertRaises(AuthError):
            service.recover("alice", original, "new test password long")
        self.assertTrue(service.recover("alice", replacement, "new test password long"))

    def test_interrupted_code_flush_can_be_retried(self):
        class InterruptedFlush(Terminal):
            def flush(self):
                if re.search(r"(?m)^[A-Za-z0-9_-]{43}$", self.getvalue()):
                    raise KeyboardInterrupt()
                return super().flush()
        service = AuthService(AuthStorage(Path(self.env["AI_EARN_AUTH_DB"])))
        service.register("alice", "test password long enough")
        err = io.StringIO()
        with patch("ai_earn.auth_cli._secret", return_value="test password long enough"):
            result = run("login", self.env, Terminal("alice\nr\nYES\n"), InterruptedFlush(), err)
        self.assertEqual(result, 2)
        self.assertIn("may have changed", err.getvalue())
        token = service.login("alice", "test password long enough")
        replacement = service.replace_recovery_code(token, confirmed=True)
        self.assertTrue(service.recover("alice", replacement, "another long password"))

    def test_registration_and_recovery_output_failures_can_be_repaired(self):
        class FailingTerminal(Terminal):
            def write(self, text):
                if re.fullmatch(r"[A-Za-z0-9_-]{43}\n", text):
                    raise OSError("simulated display error")
                return super().write(text)
        for action in ("register", "recover"):
            with self.subTest(action=action):
                path = Path(self.temp.name) / (action + ".sqlite3")
                env = {"AI_EARN_AUTH_DB": str(path)}
                service = AuthService(AuthStorage(path))
                if action == "register":
                    secrets = ["test password long enough"] * 2
                    login_password = "test password long enough"
                else:
                    old = service.register("alice", "test password long enough")
                    secrets = [old, "new test password long", "new test password long"]
                    login_password = "new test password long"
                out, err = FailingTerminal(), io.StringIO()
                with patch("ai_earn.auth_cli._secret", side_effect=secrets):
                    self.assertEqual(run(action, env, Terminal("alice\n"), out, err), 1)
                self.assertIn("may have changed", err.getvalue())
                token = service.login("alice", login_password)
                replacement = service.replace_recovery_code(token, confirmed=True)
                self.assertTrue(service.recover("alice", replacement, "another long password"))

    def test_login_and_recovery_never_create_a_missing_database(self):
        path = Path(self.env["AI_EARN_AUTH_DB"])
        for action in ("login", "recover"):
            with self.subTest(action=action):
                out, err = Terminal(), io.StringIO()
                with patch("ai_earn.auth_cli._secret") as secret:
                    self.assertEqual(run(action, self.env, Terminal(), out, err), 2)
                self.assertFalse(path.exists())
                self.assertIn("database", err.getvalue().lower())
                secret.assert_not_called()

    def test_login_and_recovery_do_not_initialize_existing_empty_file(self):
        path = Path(self.env["AI_EARN_AUTH_DB"])
        path.touch(mode=0o600)
        for action in ("login", "recover"):
            with self.subTest(action=action):
                out, err = Terminal(), io.StringIO()
                with patch("ai_earn.auth_cli._secret") as secret:
                    self.assertEqual(run(action, self.env, Terminal(), out, err), 2)
                self.assertEqual(path.stat().st_size, 0)
                secret.assert_not_called()

    def test_mismatched_password_produces_no_account(self):
        out, err = Terminal(), io.StringIO()
        with patch("ai_earn.auth_cli._secret", side_effect=["test password long enough", "different password"]):
            self.assertEqual(run("register", self.env, Terminal("alice\n"), out, err), 2)
        self.assertNotIn("Account created", out.getvalue())
        self.assertIn("Passwords do not match", err.getvalue())

    def test_missing_path_and_production_fail_before_creating_db(self):
        for env in ({}, {**self.env, "AI_EARN_ENV": "production"}):
            out, err = Terminal(), io.StringIO()
            self.assertEqual(run("register", env, Terminal(), out, err), 2)
            self.assertFalse(Path(self.env["AI_EARN_AUTH_DB"]).exists())

    def test_getpass_warning_is_a_failure(self):
        import getpass
        import warnings
        from ai_earn.auth_cli import _secret
        def unsafe(*args, **kwargs):
            warnings.warn("Password will be echoed", getpass.GetPassWarning)
            return "secret"
        with patch("getpass.getpass", side_effect=unsafe):
            with self.assertRaises(getpass.GetPassWarning):
                _secret("Password: ", io.StringIO())

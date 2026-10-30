import io
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from ai_earn.auth_cli import run


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

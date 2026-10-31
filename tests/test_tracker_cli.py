"""Offline tracker tests with disposable accounts and temporary databases."""
import io
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from ai_earn.auth import AuthService
from ai_earn.auth_storage import AuthStorage
from ai_earn.financial_assertions import FinancialAssertions
from ai_earn.state_storage import StateStorage, StateStorageError
from ai_earn import tracker_cli


class Terminal(io.StringIO):
    def isatty(self):
        return True


@unittest.skipUnless(os.name in {"posix", "nt"}, "requires private filesystem")
class TrackerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.directory = Path(self.tmp.name) / "private"
        if os.name == "nt":
            from ai_earn.windows_security import create_private_directory
            create_private_directory(self.directory)
        else:
            self.directory.mkdir(mode=0o700)
        self.auth_path = self.directory / "auth.sqlite3"
        self.state_path = self.directory / "state.sqlite3"
        self.env = {"AI_EARN_AUTH_DB": str(self.auth_path), "AI_EARN_STATE_DB": str(self.state_path)}
        patcher = patch("ai_earn.auth.PASSWORD_ITERATIONS", 1000)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.auth = AuthService(AuthStorage(self.auth_path))
        self.auth.register("alice", "disposable test password")
        self.auth.register("bobby", "disposable test password")

    def run_cli(self, action, text, env=None):
        out, err = Terminal(), Terminal()
        with patch.object(tracker_cli, "_secret", return_value="disposable test password"):
            code = tracker_cli.run(action, self.env if env is None else env, Terminal(text), out, err)
        return code, out.getvalue(), err.getvalue()

    def test_initialize_open_create_list_and_owner_isolation(self):
        code, _, _ = self.run_cli("init", "YES\n")
        self.assertEqual(code, 0)
        self.assertTrue((self.directory / "state.sqlite3.before-v13.sqlite3").exists())
        code, out, err = self.run_cli("open", "alice\n1\nExample work\n123.45\n2\n0\n")
        self.assertEqual((code, err), (0, ""))
        self.assertIn("INR 123.45 | potential opportunity", out)
        self.assertIn("UNVERIFIED", out)
        self.assertNotIn("disposable test password", out)
        code, out, _ = self.run_cli("open", "bobby\n2\n0\n")
        self.assertEqual(code, 0)
        self.assertNotIn("Example work", out)
        self.assertIn("No entries", out)

    def test_advance_and_withdraw_require_confirmation(self):
        tracker_cli._initialize_state(self.state_path)
        auth = AuthService(AuthStorage(self.auth_path, create=False))
        token = auth.login("alice", "disposable test password")
        assertions = FinancialAssertions(auth, StateStorage(self.state_path))
        ident = assertions.create(token, "Work", 100, operation_key="create")
        auth.logout(token)
        code, out, _ = self.run_cli("open", f"alice\n3\n{ident}\nno\n3\n{ident}\nYES\n2\n4\n{ident}\nYES\n0\n")
        self.assertEqual(code, 0)
        self.assertIn("quote (unverified)", out)
        self.assertIn("No change", out)
        token = auth.login("alice", "disposable test password")
        self.assertEqual(assertions.get(token, ident)["stage"], "withdrawn")

    def test_init_refuses_existing_and_partial_backups(self):
        existing = self.directory / "state.sqlite3.before-v4.sqlite3"
        existing.write_bytes(b"keep")
        with self.assertRaises(StateStorageError):
            tracker_cli._initialize_state(self.state_path)
        self.assertEqual(existing.read_bytes(), b"keep")
        self.assertFalse(self.state_path.exists())

    def test_no_automatic_create_or_migrate_and_noninteractive_denial(self):
        code, _, _ = self.run_cli("open", "alice\n")
        self.assertNotEqual(code, 0)
        self.assertFalse(self.state_path.exists())
        out, err = io.StringIO(), io.StringIO()
        self.assertEqual(tracker_cli.run("init", self.env, io.StringIO("YES\n"), out, err), 2)
        self.assertFalse(self.state_path.exists())
        StateStorage(self.state_path, create=True, confirmed=True)
        code, _, _ = self.run_cli("open", "alice\n")
        self.assertNotEqual(code, 0)
        with StateStorage(self.state_path).transaction() as conn:
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 1)

    def test_amount_parsing(self):
        for value, expected in (("0", 0), ("1.2", 120), ("123.45", 12345), ("10000000000", 1000000000000)):
            self.assertEqual(tracker_cli._paise(value), expected)
        for value in ("-1", "1e2", "1.999", "01", "1.", "nan", "10000000000.01", "", "₹1"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                tracker_cli._paise(value)

    def test_init_declined_and_production_refused(self):
        self.assertEqual(self.run_cli("init", "no\n")[0], 0)
        self.assertFalse(self.state_path.exists())
        self.assertNotEqual(self.run_cli("init", "YES\n", {**self.env, "AI_EARN_ENV": "production"})[0], 0)
        self.assertFalse(self.state_path.exists())

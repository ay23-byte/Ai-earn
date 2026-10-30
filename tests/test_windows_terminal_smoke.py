"""Unit tests of the harness; real Windows input still needs manual evidence."""

import io
from pathlib import Path
import unittest
from unittest.mock import patch

from tools import windows_terminal_smoke as smoke
from ai_earn import auth_storage


class Terminal(io.StringIO):
    def isatty(self):
        return True


class WindowsTerminalHarnessTests(unittest.TestCase):
    def test_non_windows_and_non_terminal_fail_before_storage(self):
        with patch.object(smoke, "_is_windows", return_value=False), patch.object(smoke, "create_private_directory") as create:
            self.assertEqual(smoke.main(Terminal(), Terminal(), io.StringIO()), 2)
            create.assert_not_called()
        with patch.object(smoke, "_is_windows", return_value=True), patch.object(smoke, "create_private_directory") as create:
            self.assertEqual(smoke.main(io.StringIO(), Terminal(), io.StringIO()), 2)
            create.assert_not_called()

    def test_synthetic_username_and_real_terminal_enter(self):
        source = Terminal("\n")
        proxy = smoke._SyntheticInput(source)
        self.assertTrue(proxy.isatty())
        self.assertEqual(proxy.readline(), "phase2test\n")
        self.assertEqual(proxy.readline(), "\n")

    def test_only_synthetic_password_is_accepted_and_real_input_is_called(self):
        from ai_earn.auth import AuthError
        calls = []
        def fake_hidden_input(prompt, stream):
            calls.append(prompt)
            return "not-the-test-password"
        with self.assertRaises(AuthError):
            smoke._checked_test_secret(fake_hidden_input, "Password: ", io.StringIO())
        self.assertEqual(calls, ["Password: "])
        self.assertEqual(smoke._checked_test_secret(lambda prompt, stream: smoke.TEST_PASSWORD, "Password: ", io.StringIO()), smoke.TEST_PASSWORD)

    def test_success_is_test_scoped_and_storage_is_removed(self):
        paths = []
        actions = []
        initial_gate = auth_storage._windows_auth_verified()

        def create(directory):
            paths.append(directory)
            directory.mkdir()

        def fake_run(action, env, stdin, out, err):
            actions.append(action)
            self.assertTrue(auth_storage._windows_auth_verified())
            self.assertEqual(stdin.readline(), "phase2test\n")
            self.assertEqual(env["AI_EARN_ENV"], "test")
            self.assertEqual(Path(env["AI_EARN_AUTH_DB"]).parent, paths[0])
            self.assertTrue(paths[0].exists())
            return 0

        out = Terminal()
        with patch.object(smoke, "_is_windows", return_value=True), \
                patch.object(smoke, "create_private_directory", side_effect=create), \
                patch.object(smoke.auth_cli, "run", side_effect=fake_run):
            self.assertEqual(smoke.main(Terminal(), out, io.StringIO()), 0)
        self.assertEqual(actions, ["register", "login", "recover", "login"])
        self.assertFalse(paths[0].exists())
        self.assertEqual(auth_storage._windows_auth_verified(), initial_gate)
        self.assertIn("TEST COMPLETE", out.getvalue())

    def test_failed_action_stops_and_removes_temporary_files(self):
        paths = []
        def create(directory):
            paths.append(directory)
            directory.mkdir()
        out, err = Terminal(), io.StringIO()
        with patch.object(smoke, "_is_windows", return_value=True), \
                patch.object(smoke, "create_private_directory", side_effect=create), \
                patch.object(smoke.auth_cli, "run", side_effect=[0, 2]) as run:
            self.assertEqual(smoke.main(Terminal(), out, err), 2)
        self.assertEqual(run.call_count, 2)
        self.assertFalse(paths[0].exists())
        self.assertNotIn("TEST COMPLETE", out.getvalue())
        self.assertIn("no success claimed", err.getvalue())

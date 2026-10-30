"""Exercise the real interactive CLI on a POSIX pseudoterminal with test-only credentials."""
import os
import re
import select
import signal
import sys
import tempfile
import time
import unittest
from pathlib import Path


@unittest.skipUnless(os.name == "posix", "requires POSIX pseudoterminal")
class TerminalIntegrationTests(unittest.TestCase):
    def test_register_login_and_recovery_in_real_terminal(self):
        import pty

        with tempfile.TemporaryDirectory() as directory:
            db = str(Path(directory) / "auth.sqlite3")
            env = os.environ.copy()
            env.update({"AI_EARN_AUTH_DB": db, "AI_EARN_ENV": "test", "PYTHONPATH": str(Path.cwd())})
            env.pop("AI_EARN_LOG_LEVEL", None)
            password = "example-password-for-test"
            new_password = "different-password-for-test"

            def execute(action, answers):
                pid, master = pty.fork()
                if pid == 0:
                    os.execve(sys.executable, [sys.executable, "-m", "ai_earn", "auth", action], env)
                output = b""
                pending = list(answers)
                deadline = time.monotonic() + 20
                try:
                    while time.monotonic() < deadline:
                        ready, _, _ = select.select([master], [], [], 0.25)
                        if ready:
                            try:
                                chunk = os.read(master, 4096)
                            except OSError:
                                break
                            if not chunk:
                                break
                            output += chunk
                            if pending and pending[0][0].encode() in output:
                                _, answer = pending.pop(0)
                                os.write(master, (answer + "\n").encode())
                        if not ready and not pending:
                            done, status = os.waitpid(pid, os.WNOHANG)
                            if done:
                                return os.waitstatus_to_exitcode(status), output
                    # The PTY can close just before the child becomes waitable.
                    while time.monotonic() < deadline:
                        done, status = os.waitpid(pid, os.WNOHANG)
                        if done:
                            if pending:
                                self.fail("Terminal prompts were not completed")
                            return os.waitstatus_to_exitcode(status), output
                        select.select([], [], [], 0.01)
                    os.kill(pid, signal.SIGKILL)
                    os.waitpid(pid, 0)
                    self.fail("Interactive subprocess did not finish in time")
                finally:
                    os.close(master)

            code, output = execute("register", [("Username:", "alice"), ("New password:", password), ("Confirm password:", password)])
            self.assertEqual(code, 0)
            self.assertNotIn(password.encode(), output)
            matches = re.findall(rb"(?m)^([A-Za-z0-9_-]{43})\r?$", output)
            self.assertEqual(len(matches), 1)
            recovery = matches[0].decode()
            code, output = execute("login", [("Username:", "alice"), ("Password:", password), ("Press Enter", "")])
            self.assertEqual(code, 0)
            self.assertIn(b"Signed out.", output)
            self.assertNotIn(password.encode(), output)
            code, output = execute("recover", [("Username:", "alice"), ("Recovery code:", recovery), ("New password:", new_password), ("Confirm password:", new_password)])
            self.assertEqual(code, 0)
            self.assertNotIn(recovery.encode(), output)
            self.assertNotIn(new_password.encode(), output)
            code, output = execute("login", [("Username:", "alice"), ("Password:", new_password), ("Press Enter", "")])
            self.assertEqual(code, 0)
            self.assertIn(b"Signed out.", output)
            code, output = execute("login", [("Username:", "alice"), ("Password:", new_password), ("Press Enter", "r"), ("Type YES", "YES")])
            self.assertEqual(code, 0)
            replacements = re.findall(rb"(?m)^([A-Za-z0-9_-]{43})\r?$", output)
            self.assertEqual(len(replacements), 1)
            code, output = execute("recover", [("Username:", "alice"), ("Recovery code:", replacements[0].decode()), ("New password:", password), ("Confirm password:", password)])
            self.assertEqual(code, 0)
            self.assertNotIn(replacements[0], output)
            self.assertNotIn(password.encode(), output)

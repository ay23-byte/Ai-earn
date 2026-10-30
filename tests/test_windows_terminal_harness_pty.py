"""Exercise the manual harness on POSIX; this is not Windows verification."""

import os
from pathlib import Path
import re
import select
import signal
import sys
import time
import unittest


@unittest.skipUnless(os.name == "posix", "requires a POSIX pseudoterminal")
class TerminalHarnessIntegrationTests(unittest.TestCase):
    def test_real_hidden_input_and_disposable_auth_flow(self):
        import pty

        # The only substitutions are Windows detection and private-directory
        # creation. Authentication and terminal input remain real.
        child_code = (
            "from unittest.mock import patch\n"
            "from tools import windows_terminal_smoke as smoke\n"
            "with patch.object(smoke, '_is_windows', return_value=True), "
            "patch.object(smoke, 'create_private_directory', "
            "side_effect=lambda path: path.mkdir(mode=0o700)):\n"
            "    raise SystemExit(smoke.main())\n"
        )
        password = b"Test-Only-Password-123!"
        pending = [
            (b"New password:", password),
            (b"Confirm password:", password),
            (b"Password:", password),
            (b"Press Enter", b""),
            (b"Recovery code:", None),
            (b"New password:", password),
            (b"Confirm password:", password),
            (b"Password:", password),
            (b"Press Enter", b""),
        ]
        root = Path(__file__).resolve().parents[1]
        pid, master = pty.fork()
        if pid == 0:
            os.chdir(root)
            os.execv(sys.executable, [sys.executable, "-c", child_code])
        data = b""
        cursor = 0
        recovery = None
        reaped = False
        deadline = time.monotonic() + 30
        try:
            while time.monotonic() < deadline:
                ready, _, _ = select.select([master], [], [], 0.1)
                if ready:
                    try:
                        chunk = os.read(master, 4096)
                    except OSError:
                        chunk = b""
                    if chunk:
                        data += chunk
                        self.assertLess(len(data), 100_000, "Unexpectedly large terminal output")
                        while pending:
                            prompt, answer = pending[0]
                            index = data.find(prompt, cursor)
                            if index < 0:
                                break
                            cursor = index + len(prompt)
                            if answer is None:
                                codes = re.findall(rb"(?m)^([A-Za-z0-9_-]{43})\r?$", data)
                                self.assertTrue(codes, "Disposable recovery code was not displayed")
                                recovery = codes[0]
                                answer = recovery
                            os.write(master, answer + b"\n")
                            pending.pop(0)
                done, status = os.waitpid(pid, os.WNOHANG)
                if done:
                    reaped = True
                    # A child can exit while its final terminal output is still buffered.
                    while select.select([master], [], [], 0)[0]:
                        try:
                            tail = os.read(master, 4096)
                        except OSError:
                            break
                        if not tail:
                            break
                        data += tail
                    self.assertEqual(os.waitstatus_to_exitcode(status), 0, "Harness failed")
                    break
            self.assertTrue(reaped, "Harness timed out")
            self.assertFalse(pending, "Not all terminal prompts were completed")
            self.assertIn(b"TEST COMPLETE", data)
            # The test password is printed once in the instructions; input must
            # not echo it again. Recovery is displayed once, never echoed.
            self.assertEqual(data.count(password), 1, "Test password input was echoed")
            self.assertIsNotNone(recovery)
            self.assertEqual(data.count(recovery), 1, "Test recovery input was echoed")
        finally:
            os.close(master)
            if not reaped:
                try:
                    os.kill(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                os.waitpid(pid, 0)

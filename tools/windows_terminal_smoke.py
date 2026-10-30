"""Disposable manual Windows terminal check using the configured development path."""

from pathlib import Path
import sys
import tempfile
from unittest.mock import patch

from ai_earn import auth_cli
from ai_earn.auth import AuthError
from ai_earn.windows_security import WindowsSecurityError, create_private_directory


TEST_PASSWORD = "Test-Only-Password-123!"
TEST_USERNAME = "phase2test"


def _is_windows() -> bool:
    return sys.platform == "win32"


class _SyntheticInput:
    """Provide a synthetic username; keep real terminal input for Enter/getpass."""

    def __init__(self, terminal):
        self.terminal = terminal
        self.first = True

    def isatty(self):
        return self.terminal.isatty()

    def readline(self):
        if self.first:
            self.first = False
            return TEST_USERNAME + "\n"
        return self.terminal.readline()


def _checked_test_secret(real_secret, prompt, stream):
    value = real_secret(prompt, stream)
    if prompt != "Recovery code: " and value != TEST_PASSWORD:
        raise AuthError("Use only the synthetic test password shown in the instructions.")
    return value


def main(stdin=None, out=None, err=None) -> int:
    stdin = stdin if stdin is not None else sys.stdin
    out = out if out is not None else sys.stdout
    err = err if err is not None else sys.stderr
    if not _is_windows():
        err.write("This test requires an actual Windows terminal.\n")
        return 2
    if not stdin.isatty() or not out.isatty():
        err.write("This test requires an interactive terminal; do not pipe input or output.\n")
        return 2

    out.write("TEST ONLY: use no real password, account name, or recovery code.\n")
    out.write(f"For every password prompt, type exactly: {TEST_PASSWORD}\n")
    out.write("A disposable test recovery code will be displayed. Use it only in this test; do not share it.\n")
    out.write("Usernames are supplied automatically. Confirm that password/code input is not displayed.\n")
    out.flush()

    # This calls the real hidden-input function. The wrapper merely requires the
    # public synthetic password so this test cannot store a real password.
    real_secret = auth_cli._secret

    def synthetic_secret(prompt, stream):
        return _checked_test_secret(real_secret, prompt, stream)

    try:
        with tempfile.TemporaryDirectory(prefix="ai-earn-terminal-test-") as tmp:
            private_dir = Path(tmp) / "private"
            create_private_directory(private_dir)
            env = {"AI_EARN_ENV": "test", "AI_EARN_AUTH_DB": str(private_dir / "auth.sqlite3")}
            # The gate and storage path are not patched; the wrapper only
            # enforces the public synthetic password while calling real getpass.
            with patch("ai_earn.auth_cli._secret", side_effect=synthetic_secret):
                for index, (action, description) in enumerate((
                    ("register", "Register the disposable account; save its disposable recovery code."),
                    ("login", "Sign in, then press Enter to sign out."),
                    ("recover", "Enter the disposable recovery code and the same test password."),
                    ("login", "Sign in again, then press Enter to sign out."),
                ), start=1):
                    out.write(f"\nStep {index}/4: {description}\n")
                    out.flush()
                    code = auth_cli.run(action, env, _SyntheticInput(stdin), out, err)
                    if code != 0:
                        err.write(f"Terminal check stopped at step {index}; no success claimed.\n")
                        return code
        out.write("\nTEST COMPLETE: all four actions returned success and the temporary directory was removed.\n")
        out.write("Report the summary and whether password/recovery input was hidden; do not share any code.\n")
        return 0
    except (OSError, WindowsSecurityError):
        err.write("Terminal check could not create or clean up its private temporary directory.\n")
        return 1
    except KeyboardInterrupt:
        err.write("Terminal check interrupted; no success claimed.\n")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

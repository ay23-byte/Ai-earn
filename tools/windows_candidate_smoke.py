"""Disposable verification of a Windows candidate with its gate enabled.

This utility never changes or monkeypatches the storage gate. It must be run
only from an isolated, explicitly marked candidate copy, never with real data.
"""
import io
from pathlib import Path
import re
import sys
import tempfile
from unittest.mock import patch

from ai_earn import auth_storage
from ai_earn.auth import AuthError, AuthService
from ai_earn.auth_cli import run
from ai_earn.windows_security import create_private_directory


_PASSWORD = "synthetic-candidate-password"
_NEW_PASSWORD = "synthetic-candidate-new-password"
_USERNAME = "candidate_test"
_CODE_LINE = re.compile(r"(?m)^([A-Za-z0-9_-]{43})\r?$")


class _Terminal(io.StringIO):
    def isatty(self):
        return True


def _is_windows() -> bool:
    return sys.platform == "win32"


def _exercise(path: Path) -> None:
    """Exercise the ordinary storage and CLI path; only terminal input is mocked."""
    env = {"AI_EARN_ENV": "test", "AI_EARN_AUTH_DB": str(path)}

    def command(action: str, inputs: str, secrets: list[str]) -> str:
        out, err = _Terminal(), io.StringIO()
        with patch("ai_earn.auth_cli._secret", side_effect=secrets):
            result = run(action, env, _Terminal(inputs), out, err)
        if result != 0:
            raise RuntimeError("Candidate CLI operation failed.")
        return out.getvalue()

    output = command("register", _USERNAME + "\n", [_PASSWORD, _PASSWORD])
    codes = _CODE_LINE.findall(output)
    if len(codes) != 1:
        raise RuntimeError("Registration did not display a code.")
    old_code = codes[0]
    command("login", _USERNAME + "\n\n", [_PASSWORD])
    output = command("login", _USERNAME + "\nr\nYES\n", [_PASSWORD])
    codes = _CODE_LINE.findall(output)
    if len(codes) != 1 or codes[0] == old_code:
        raise RuntimeError("Replacement was not produced.")
    service = AuthService(auth_storage.AuthStorage(path))
    try:
        service.recover(_USERNAME, old_code, _NEW_PASSWORD)
    except AuthError:
        pass
    else:
        raise RuntimeError("Replaced code was accepted.")
    command("recover", _USERNAME + "\n", [codes[0], _NEW_PASSWORD, _NEW_PASSWORD])
    command("login", _USERNAME + "\n\n", [_NEW_PASSWORD])


def main(out=None, err=None) -> int:
    out = out if out is not None else sys.stdout
    err = err if err is not None else sys.stderr
    if not _is_windows():
        err.write("Candidate verification requires Windows.\n")
        return 2
    if auth_storage._windows_auth_verified() is not True:
        err.write("Windows auth is disabled in this copy; no candidate verification run.\n")
        return 2
    try:
        with tempfile.TemporaryDirectory(prefix="ai-earn-candidate-test-") as tmp:
            private_dir = Path(tmp) / "private"
            create_private_directory(private_dir)
            _exercise(private_dir / "auth.sqlite3")
    except Exception:
        # Do not print exceptions, paths or captured credentials.
        err.write("Candidate verification failed; no success claimed.\n")
        return 1
    out.write("CANDIDATE TEST COMPLETE: registration, login, code replacement, recovery and login passed; temporary data removed.\n")
    out.write("Only synthetic data was used. This is not production approval.\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

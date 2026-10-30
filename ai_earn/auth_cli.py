"""Interactive local auth. Secrets are never accepted as command arguments."""

import getpass
from pathlib import Path
import sqlite3
from typing import Mapping, TextIO
import warnings

from .auth import AuthError, AuthService
from .auth_storage import AuthStorage, StorageError
from .config import ConfigurationError, load_settings


def _secret(prompt: str, err: TextIO) -> str:
    # getpass can otherwise fall back to echoed input when a terminal is unavailable.
    with warnings.catch_warnings():
        warnings.simplefilter("error", getpass.GetPassWarning)
        return getpass.getpass(prompt, stream=err)


def run(action: str, environ: Mapping[str, str], stdin: TextIO, out: TextIO, err: TextIO) -> int:
    if action not in {"register", "login", "recover"}:
        out.write("Usage: python -m ai_earn auth {register|login|recover}\n")
        return 2
    if not stdin.isatty() or not out.isatty():
        err.write("Authentication requires an interactive terminal; secrets are not accepted from pipes.\n")
        return 2
    try:
        settings = load_settings(environ)
        if settings.environment == "production":
            raise AuthError("Authentication is not enabled for production use.")
        path = environ.get("AI_EARN_AUTH_DB", "")
        if not path:
            raise AuthError("Set AI_EARN_AUTH_DB to an absolute path in a private directory.")
        # Only registration may initialize storage. A missing login/recovery
        # database must not be silently replaced by an empty one.
        service = AuthService(AuthStorage(Path(path), create=(action == "register")))
        out.write("Username: ")
        out.flush()
        username = stdin.readline().rstrip("\r\n")
        if action == "register":
            password = _secret("New password: ", err)
            confirmation = _secret("Confirm password: ", err)
            if password != confirmation:
                raise AuthError("Passwords do not match.")
            recovery = service.register(username, password)
            out.write("Account created. Save this one-time recovery code privately; it cannot be shown again:\n")
            out.write(recovery + "\n")
            out.flush()
        elif action == "recover":
            recovery = _secret("Recovery code: ", err)
            password = _secret("New password: ", err)
            if password != _secret("Confirm password: ", err):
                raise AuthError("Passwords do not match.")
            new_recovery = service.recover(username, recovery, password)
            out.write("Password changed and sessions revoked. Save the new one-time recovery code privately:\n")
            out.write(new_recovery + "\n")
            out.flush()
        else:
            password = _secret("Password: ", err)
            token = service.login(username, password)
            try:
                profile = service.profile(token)
                out.write(f"Signed in as {profile['username']}. Press Enter to log out, or type R to replace your recovery code.\n")
                out.flush()
                choice = stdin.readline().strip()
                if choice.lower() == "r":
                    out.write("This invalidates the previous recovery code. Type YES to confirm: ")
                    out.flush()
                    if stdin.readline().strip() == "YES":
                        replacement = service.replace_recovery_code(token, confirmed=True)
                        out.write("Save this replacement recovery code privately; it cannot be shown again:\n")
                        out.write(replacement + "\n")
                        out.flush()
                    else:
                        out.write("Recovery code unchanged.\n")
                elif choice:
                    out.write("Unrecognized choice; no recovery code was changed.\n")
            finally:
                service.logout(token)
            out.write("Signed out.\n")
        return 0
    except (AuthError, StorageError, ConfigurationError) as exc:
        err.write(str(exc) + "\n")
        return 2
    except (EOFError, KeyboardInterrupt, getpass.GetPassWarning):
        err.write("Authentication cancelled or hidden input unavailable; an operation in progress may have changed state.\n")
        return 2
    except (OSError, sqlite3.Error):
        err.write("The operation could not finish; its state may have changed. If a recovery code was not displayed, sign in to replace it.\n")
        return 1
    except Exception:
        err.write("An unexpected authentication error occurred; the operation's state may have changed.\n")
        return 1

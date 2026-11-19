"""Interactive first-run onboarding checklist (read-only, local-only)."""

from pathlib import Path

from .auth import AuthError, AuthService
from .auth_cli import _secret
from .auth_storage import AuthStorage, StorageError
from .config import ConfigurationError, load_settings
from .onboarding import OnboardingChecklist
from .state_storage import StateStorage, StateStorageError


def run(environ, stdin, out, err) -> int:
    if not stdin.isatty() or not out.isatty():
        err.write("Onboarding requires an interactive terminal; credentials are never accepted from pipes.\n")
        return 2
    auth = None
    token = None
    try:
        settings = load_settings(environ)
        if settings.environment == "production":
            raise ConfigurationError("AI_EARN_ENV")
        auth_path = environ.get("AI_EARN_AUTH_DB", "")
        state_path = environ.get("AI_EARN_STATE_DB", "")
        if not auth_path or not state_path:
            raise StateStorageError("Set AI_EARN_AUTH_DB and AI_EARN_STATE_DB to absolute paths in a private directory.")
        state = StateStorage(Path(state_path), create=False)
        auth = AuthService(AuthStorage(Path(auth_path), create=False))
        out.write("Username: ")
        out.flush()
        username = stdin.readline().rstrip("\r\n")
        password = _secret("Password: ", err)
        token = auth.login(username, password)
        result = OnboardingChecklist(auth, state).get(token)
        auth.logout(token)
        token = None
        out.write(f"\nFirst-run progress: {result['completed']}/{result['total']} complete\n")
        for item in result["items"]:
            mark = "x" if item["done"] else " "
            out.write(f"[{mark}] {item['label']}\n")
        out.write(f"\nNext: {result['next_action']}\n")
        return 0
    except (AuthError, StorageError, StateStorageError, ConfigurationError):
        err.write("Onboarding could not be shown. Check your credentials and private database paths.\n")
        return 2
    except (EOFError, KeyboardInterrupt):
        err.write("Onboarding cancelled.\n")
        return 2
    finally:
        if auth is not None and token is not None:
            try:
                auth.logout(token)
            except Exception:
                pass

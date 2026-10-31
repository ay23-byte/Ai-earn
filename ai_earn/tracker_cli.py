"""Experimental offline, manually asserted opportunity tracker.

No payment is verified, received or processed. Use only disposable data.
"""
import getpass
import re
import secrets
import sqlite3
from pathlib import Path

from .auth import AuthError, AuthService
from .auth_cli import _secret
from .auth_storage import AuthStorage, StorageError
from .config import ConfigurationError, load_settings
from .financial_assertions import FinancialAssertions, FinancialAssertionError
from .state_storage import StateStorage, StateStorageError

_STAGES = {"potential": "potential opportunity", "quote_asserted": "quote (unverified)",
           "contract_asserted": "contract (unverified)", "invoice_asserted": "invoice (unverified)",
           "initiated_asserted": "payment initiated (unverified, not received)", "withdrawn": "withdrawn"}
_NEXT = {"potential": "quote_asserted", "quote_asserted": "contract_asserted",
         "contract_asserted": "invoice_asserted", "invoice_asserted": "initiated_asserted"}


def _read(stdin, out, prompt):
    out.write(prompt)
    out.flush()
    line = stdin.readline()
    if not line:
        raise EOFError
    return line.rstrip("\r\n")


def _paise(value):
    if re.fullmatch(r"(?:0|[1-9][0-9]{0,10})(?:\.[0-9]{1,2})?", value) is None:
        raise ValueError("Enter a non-negative INR amount, with at most two decimal places.")
    whole, _, fractional = value.partition(".")
    amount = int(whole) * 100 + int((fractional + "00")[:2])
    if amount > 1_000_000_000_000:
        raise ValueError("Amount exceeds the supported limit.")
    return amount


def _key():
    return "tracker_" + secrets.token_hex(24)


def _initialize_state(path):
    # Only initialize a new file; never silently upgrade an existing database.
    backups = [path.with_name(path.name + f".before-v{version}.sqlite3") for version in range(2, 14)]
    if path.exists() or path.is_symlink() or any(p.exists() or p.is_symlink() for p in backups):
        raise StateStorageError("State or migration backup already exists; no initialization was attempted.")
    state = StateStorage(path, create=True, confirmed=True)
    for version, backup in zip(range(2, 14), backups):
        getattr(state, f"migrate_to_v{version}")(backup, confirmed=True)
    return state


def _menu(service, token, stdin, out):
    out.write("Local tracker: all entries are UNVERIFIED. No payment processing or settlement.\n")
    while True:
        choice = _read(stdin, out, "\n1 Add opportunity  2 List entries  3 Advance stage  4 Withdraw  0 Sign out\nChoice: ").strip()
        try:
            if choice == "0":
                return
            if choice == "1":
                title = _read(stdin, out, "Opportunity title (no sensitive data): ")
                amount = _paise(_read(stdin, out, "Proposed INR amount: ").strip())
                ident = service.create(token, title, amount, operation_key=_key())
                out.write(f"Created unverified entry: {ident}\n")
            elif choice == "2":
                cursor = None
                while True:
                    entries = service.list(token, after_id=cursor, limit=100)
                    for item in entries:
                        amount = item["amount_paise"]
                        out.write(f"{item['id']} | {item['title']} | INR {amount // 100}.{amount % 100:02d} | {_STAGES[item['stage']]}\n")
                    if len(entries) < 100:
                        break
                    if _read(stdin, out, "More entries? Type YES: ").strip() != "YES":
                        break
                    cursor = entries[-1]["id"]
                if cursor is None and not entries:
                    out.write("No entries.\n")
            elif choice in {"3", "4"}:
                ident = _read(stdin, out, "Entry ID: ").strip()
                entry = service.get(token, ident)
                stage = _NEXT.get(entry["stage"]) if choice == "3" else "withdrawn"
                if stage is None or (choice == "4" and entry["stage"] == "withdrawn"):
                    out.write("No further change is available.\n")
                    continue
                out.write(f"Record {_STAGES[stage]}? This is only your unverified assertion.\n")
                if _read(stdin, out, "Type YES to confirm: ").strip() != "YES":
                    out.write("No change.\n")
                    continue
                if choice == "3":
                    service.advance(token, ident, stage, confirmed=True, operation_key=_key())
                else:
                    service.withdraw(token, ident, confirmed=True, operation_key=_key())
                out.write("Local entry updated; nothing was sent or paid.\n")
            else:
                out.write("Choose 0, 1, 2, 3 or 4.\n")
        except (FinancialAssertionError, ValueError) as exc:
            out.write(str(exc) + "\n")


def run(action, environ, stdin, out, err):
    if action not in {"init", "open"}:
        err.write("Usage: python -m ai_earn tracker {init|open}\n")
        return 2
    if not stdin.isatty() or not out.isatty():
        err.write("Tracker requires an interactive terminal.\n")
        return 2
    auth = token = None
    try:
        if load_settings(environ).environment == "production":
            raise StateStorageError("Tracker is not enabled for production use.")
        auth_path, state_path = environ.get("AI_EARN_AUTH_DB", ""), environ.get("AI_EARN_STATE_DB", "")
        if not auth_path or not state_path:
            raise StateStorageError("Set AI_EARN_AUTH_DB and AI_EARN_STATE_DB to absolute paths in a private directory.")
        path = Path(state_path)
        if action == "init":
            if _read(stdin, out, "Create a NEW tracker state file and migration backups? Type YES: ").strip() != "YES":
                out.write("No files created.\n")
                return 0
            _initialize_state(path)
            out.write("New tracker state initialized. Register an account with the auth command if needed.\n")
            return 0
        # Open only; missing files are never created during login.
        state = StateStorage(path)
        with state.transaction() as conn:
            if conn.execute("PRAGMA user_version").fetchone()[0] not in (13, 14):
                raise StateStorageError("Tracker requires an existing version 13 or 14 state file; no upgrade was performed.")
        auth = AuthService(AuthStorage(Path(auth_path), create=False))
        username = _read(stdin, out, "Username: ")
        password = _secret("Password: ", err)
        token = auth.login(username, password)
        _menu(FinancialAssertions(auth, state), token, stdin, out)
        return 0
    except (AuthError, StorageError, StateStorageError, ConfigurationError):
        err.write("Tracker could not open or complete the operation. Check your account, private paths and state version; no automatic replacement is performed.\n")
        return 2
    except (EOFError, KeyboardInterrupt, getpass.GetPassWarning):
        err.write("Tracker cancelled. An interrupted operation may already have committed; inspect entries before retrying.\n")
        return 2
    except (OSError, sqlite3.Error):
        err.write("Tracker storage error. An operation may already have committed; inspect before retrying.\n")
        return 1
    except Exception:
        err.write("Tracker encountered an unexpected error. An operation may already have committed; inspect before retrying.\n")
        return 1
    finally:
        if auth is not None and token is not None:
            try:
                auth.logout(token)
            except Exception:
                err.write("Sign-out could not be confirmed; the session will expire.\n")

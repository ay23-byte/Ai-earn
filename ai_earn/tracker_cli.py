"""Experimental offline, manually asserted opportunity tracker.

No payment is verified, received or processed. Use only disposable data.
Now includes v15 opportunity discovery and v16 service catalog/portfolio.
"""

import getpass
import re
import secrets
import sqlite3
from pathlib import Path

from .auth import AccessDenied, AuthError, AuthService
from .auth_cli import _secret
from .auth_storage import AuthStorage, StorageError
from .config import ConfigurationError, load_settings
from .financial_assertions import FinancialAssertions, FinancialAssertionError
from .opportunities import Opportunities, OpportunityError
from .services import Services, Portfolio, ServiceError
from .state_storage import StateStorage, StateStorageError

_STAGES = {"potential": "potential opportunity", "quote_asserted": "quote (unverified)",
           "contract_asserted": "contract (unverified)", "invoice_asserted": "invoice (unverified)",
           "initiated_asserted": "payment initiated (unverified, not received)", "withdrawn": "withdrawn"}
_NEXT = {"potential": "quote_asserted", "quote_asserted": "contract_asserted",
         "contract_asserted": "invoice_asserted", "invoice_asserted": "initiated_asserted"}

_OPP_STATUS_LABELS = {
    "discovered": "discovered (unverified)",
    "vetted": "vetted (unverified)",
    "pursuing": "pursuing (unverified)",
    "archived": "archived",
    "dismissed": "dismissed",
}
_OPP_TRANSITIONS = {
    "1": ("vetted", "opportunity_vetted"),
    "2": ("pursuing", "opportunity_pursuing"),
    "3": ("archived", "opportunity_archived"),
    "4": ("dismissed", "opportunity_dismissed"),
}


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
    backups = [path.with_name(path.name + f".before-v{version}.sqlite3") for version in range(2, 17)]
    if path.exists() or path.is_symlink() or any(p.exists() or p.is_symlink() for p in backups):
        raise StateStorageError("State or migration backup already exists; no initialization was attempted.")
    state = StateStorage(path, create=True, confirmed=True)
    for version, backup in zip(range(2, 17), backups):
        getattr(state, f"migrate_to_v{version}")(backup, confirmed=True)
    return state


def _menu(service, token, stdin, out, opportunities=None, services=None, portfolio=None):
    # service = FinancialAssertions, opportunities = Opportunities, services/portfolio = v16
    out.write("Local tracker: all entries are UNVERIFIED. No payment processing or settlement.\n")
    has_opp = opportunities is not None
    has_svc = services is not None and portfolio is not None
    if has_opp and has_svc:
        out.write("Financial (1-4), Opportunities v15 (5-8), Services v16 (9-12) available.\n")
    elif has_opp:
        out.write("Financial pipeline (1-4) and Opportunities v15 (5-8) are both available.\n")
    elif has_svc:
        out.write("Financial pipeline (1-4) and Services v16 (9-12) are available.\n")
    while True:
        if not has_opp and not has_svc:
            choice = _read(stdin, out, "\n1 Add opportunity  2 List entries  3 Advance stage  4 Withdraw  0 Sign out\nChoice: ").strip()
        elif has_opp and has_svc:
            choice = _read(stdin, out, "\n1 Add financial  2 List financial  3 Advance financial  4 Withdraw financial | 5 Add opportunity  6 List opps  7 Manage opp  8 Ranked | 9 Add service  10 List services  11 Manage service  12 Portfolio  0 Sign out\nChoice: ").strip()
        elif has_opp:
            choice = _read(stdin, out, "\n1 Add financial  2 List financial  3 Advance financial  4 Withdraw financial | 5 Add opportunity  6 List opportunities  7 Manage opportunity  8 Ranked opportunities  0 Sign out\nChoice: ").strip()
        else:
            choice = _read(stdin, out, "\n1 Add financial  2 List financial  3 Advance financial  4 Withdraw financial | 9 Add service  10 List services  11 Manage service  12 Portfolio  0 Sign out\nChoice: ").strip()
        try:
            if choice == "0":
                return
            if choice == "1":
                title = _read(stdin, out, "Opportunity title (no sensitive data): ")
                amount = _paise(_read(stdin, out, "Proposed INR amount: ").strip())
                ident = service.create(token, title, amount, operation_key=_key())
                out.write(f"Created unverified financial entry: {ident}\n")
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
            elif opportunities is not None and choice == "5":
                title = _read(stdin, out, "Opportunity title (1-200, no control chars): ")
                url = _read(stdin, out, "Source URL (http:// or https://): ").strip()
                budget_raw = _read(stdin, out, "Budget INR amount (e.g. 5000 or 5000.50): ").strip()
                budget = _paise(budget_raw)
                deadline_raw = _read(stdin, out, "Deadline unix timestamp (0 for none, or future seconds): ").strip()
                if deadline_raw == "":
                    deadline_raw = "0"
                try:
                    deadline = int(deadline_raw)
                except ValueError:
                    raise ValueError("Deadline must be an integer timestamp or 0.")
                risk = _read(stdin, out, "Risk level [low/medium/high/unknown] (default unknown): ").strip().lower()
                if risk == "":
                    risk = "unknown"
                tags = _read(stdin, out, "Skill tags (comma separated, 0-500 chars, empty for none): ")
                ident = opportunities.create(
                    token,
                    title,
                    url,
                    budget,
                    operation_key=_key(),
                    deadline_at=deadline,
                    risk_level=risk,
                    skill_tags=tags,
                    source_type="manual",
                    connector_id=None,
                )
                out.write(f"Created unverified opportunity (discovered): {ident}\n")
                out.write("Verification: unverified, budget_not_revenue. Use vet/pursue to advance.\n")
            elif opportunities is not None and choice == "6":
                # List opportunities with optional filters
                out.write("Listing opportunities (owner-scoped, id-ordered). Filters optional.\n")
                status_f = _read(stdin, out, "Filter status [discovered/vetted/pursuing/archived/dismissed or empty]: ").strip()
                if status_f == "":
                    status_f = None
                risk_f = _read(stdin, out, "Filter risk [low/medium/high/unknown or empty]: ").strip()
                if risk_f == "":
                    risk_f = None
                cursor = None
                while True:
                    entries = opportunities.list(token, after_id=cursor, limit=50, status=status_f, risk_level=risk_f)
                    for item in entries:
                        out.write(f"{item['id']} | {item['title']} | {item['source_url'][:60]} | INR {item['budget_paise'] // 100}.{item['budget_paise'] % 100:02d} | {_OPP_STATUS_LABELS.get(item['status'], item['status'])} | risk:{item['risk_level']} | skills:{item['skill_tags'][:40]}\n")
                    if len(entries) < 50:
                        break
                    if _read(stdin, out, "More? Type YES: ").strip() != "YES":
                        break
                    cursor = entries[-1]["id"]
                if cursor is None and not entries:
                    out.write("No opportunities.\n")
            elif opportunities is not None and choice == "7":
                ident = _read(stdin, out, "Opportunity ID: ").strip()
                item = opportunities.get(token, ident)
                out.write(f"Current: {item['title']} | {_OPP_STATUS_LABELS.get(item['status'], item['status'])} | risk:{item['risk_level']} | budget INR {item['budget_paise'] // 100}.{item['budget_paise'] % 100:02d}\n")
                out.write("Transitions: 1 vetted  2 pursuing  3 archived  4 dismissed\n")
                tchoice = _read(stdin, out, "Choose transition (1-4) or empty to cancel: ").strip()
                if tchoice == "":
                    out.write("No change.\n")
                    continue
                if tchoice not in _OPP_TRANSITIONS:
                    out.write("Invalid transition choice.\n")
                    continue
                target, _event = _OPP_TRANSITIONS[tchoice]
                out.write(f"Change to {_OPP_STATUS_LABELS.get(target, target)}? This is unverified.\n")
                if _read(stdin, out, "Type YES to confirm: ").strip() != "YES":
                    out.write("No change.\n")
                    continue
                if target == "vetted":
                    opportunities.vetted(token, ident, operation_key=_key())
                elif target == "pursuing":
                    opportunities.pursuing(token, ident, operation_key=_key())
                elif target == "archived":
                    opportunities.archived(token, ident, operation_key=_key())
                elif target == "dismissed":
                    opportunities.dismissed(token, ident, operation_key=_key())
                out.write(f"Opportunity updated to {target} (unverified).\n")
            elif opportunities is not None and choice == "8":
                limit_raw = _read(stdin, out, "How many ranked results (1-100, default 20): ").strip()
                if limit_raw == "":
                    limit = 20
                else:
                    try:
                        limit = int(limit_raw)
                    except ValueError:
                        raise ValueError("Invalid limit.")
                results = opportunities.ranked(token, limit=limit)
                if not results:
                    out.write("No opportunities to rank.\n")
                else:
                    out.write("Ranked (budget desc, risk low->high, deadline asc) — draft heuristic, unverified:\n")
                    for item in results:
                        out.write(f"{item['id']} | {item['title'][:40]} | INR {item['budget_paise'] // 100}.{item['budget_paise'] % 100:02d} | {item['risk_level']} | deadline:{item['deadline_at']} | {item['status']}\n")
            elif services is not None and choice == "9":
                title = _read(stdin, out, "Service title (1-200): ")
                scope = _read(stdin, out, "Scope (0-2000, empty for none): ")
                deliverable = _read(stdin, out, "Deliverable (1-500): ")
                price_raw = _read(stdin, out, "Price INR (e.g. 5000 or 2500.50): ").strip()
                price = _paise(price_raw)
                rev_raw = _read(stdin, out, "Revisions (0-100, default 1): ").strip()
                rev = int(rev_raw) if rev_raw != "" else 1
                ident = services.create(token, title, scope, deliverable, price, rev, operation_key=_key())
                out.write(f"Created service draft: {ident}\n")
            elif services is not None and choice == "10":
                status_f = _read(stdin, out, "Filter status [draft/published/archived or empty]: ").strip()
                if status_f == "":
                    status_f = None
                cursor = None
                while True:
                    entries = services.list(token, after_id=cursor, limit=50, status=status_f)
                    for item in entries:
                        out.write(f"{item['id']} | {item['title']} | INR {item['price_paise'] // 100}.{item['price_paise'] % 100:02d} | rev:{item['revisions']} | {item['status']} | scope:{item['scope'][:40]}\n")
                    if len(entries) < 50:
                        break
                    if _read(stdin, out, "More? Type YES: ").strip() != "YES":
                        break
                    cursor = entries[-1]["id"]
                if cursor is None and not entries:
                    out.write("No services.\n")
            elif services is not None and choice == "11":
                ident = _read(stdin, out, "Service ID: ").strip()
                item = services.get(token, ident)
                out.write(f"Current: {item['title']} | {item['status']} | INR {item['price_paise'] // 100}.{item['price_paise'] % 100:02d}\n")
                out.write("Transitions: 1 publish  2 archive\n")
                tchoice = _read(stdin, out, "Choose (1-2) or empty to cancel: ").strip()
                if tchoice == "":
                    out.write("No change.\n")
                    continue
                if tchoice == "1":
                    out.write("Publish service? This is unverified local draft.\n")
                    if _read(stdin, out, "Type YES to confirm: ").strip() != "YES":
                        out.write("No change.\n")
                        continue
                    services.publish(token, ident, operation_key=_key())
                    out.write("Service published (unverified).\n")
                elif tchoice == "2":
                    out.write("Archive service? This is unverified.\n")
                    if _read(stdin, out, "Type YES to confirm: ").strip() != "YES":
                        out.write("No change.\n")
                        continue
                    services.archive(token, ident, operation_key=_key())
                    out.write("Service archived.\n")
                else:
                    out.write("Invalid choice.\n")
            elif services is not None and portfolio is not None and choice == "12":
                out.write("Portfolio: 1 Add item  2 List items\n")
                sub = _read(stdin, out, "Choice (1-2): ").strip()
                if sub == "1":
                    sid = _read(stdin, out, "Service ID: ").strip()
                    title = _read(stdin, out, "Portfolio title (1-200): ")
                    desc = _read(stdin, out, "Description (0-2000): ")
                    pid = portfolio.create(token, sid, title, desc, operation_key=_key())
                    out.write(f"Created portfolio item: {pid}\n")
                elif sub == "2":
                    sid = _read(stdin, out, "Service ID (empty for all): ").strip()
                    if sid == "":
                        sid = None
                    cursor = None
                    while True:
                        entries = portfolio.list(token, service_id=sid, after_id=cursor, limit=50)
                        for item in entries:
                            out.write(f"{item['id']} | svc:{item['service_id'][:8]} | {item['title']} | {item['description'][:60]}\n")
                        if len(entries) < 50:
                            break
                        if _read(stdin, out, "More? Type YES: ").strip() != "YES":
                            break
                        cursor = entries[-1]["id"]
                    if cursor is None and not entries:
                        out.write("No portfolio items.\n")
                else:
                    out.write("Invalid portfolio choice.\n")
            else:
                has_opp = opportunities is not None
                has_svc = services is not None and portfolio is not None
                if not has_opp and not has_svc:
                    out.write("Choose 0, 1, 2, 3 or 4.\n")
                elif has_opp and has_svc:
                    out.write("Choose 0-4 (financial), 5-8 (opps), 9-12 (services/portfolio).\n")
                elif has_opp:
                    out.write("Choose 0-4 (financial) or 5-8 (opportunities).\n")
                else:
                    out.write("Choose 0-4 (financial) or 9-12 (services).\n")
        except (FinancialAssertionError, OpportunityError, ServiceError, ValueError, StateStorageError) as exc:
            out.write(str(exc) + "\n")
        except AccessDenied as exc:
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
            out.write("New tracker state initialized (v16, 15 backups). Register an account with the auth command if needed.\n")
            return 0
        # Open only; missing files are never created during login.
        state = StateStorage(path)
        with state.transaction() as conn:
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            if version not in (13, 14, 15, 16):
                raise StateStorageError("Tracker requires an existing version 13, 14, 15 or 16 state file; no upgrade was performed.")
        auth = AuthService(AuthStorage(Path(auth_path), create=False))
        username = _read(stdin, out, "Username: ")
        password = _secret("Password: ", err)
        token = auth.login(username, password)
        # Opportunities available on 15+, services/portfolio on 16+
        opp = Opportunities(auth, state) if version >= 15 else None
        svc = Services(auth, state) if version >= 16 else None
        port = Portfolio(auth, state) if version >= 16 else None
        _menu(FinancialAssertions(auth, state), token, stdin, out, opp, svc, port)
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

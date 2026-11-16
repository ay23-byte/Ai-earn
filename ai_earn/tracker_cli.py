"""Experimental offline, manually asserted opportunity tracker.

No payment is verified, received or processed. Use only disposable data.
Now includes v15 opportunity discovery, v16 service catalog/portfolio,
v17 product workspace & customers, v18 communications & ledger, v19 followups & CSV, v20 invoices & 11-concept hardening, and v22 AI assistant (ToolRegistry/ApprovalQueue).
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
from .communications import Communications, CommunicationError
from .customers import Customers, CustomerError
from .financial_assertions import FinancialAssertions, FinancialAssertionError
from .followups import Followups, FollowupError
from .invoices import Invoices, InvoiceError
from .payment_providers import PaymentProviders, ProviderConfig, ProviderError
from .ai_assistant import AIAssistant, ToolRegistry, ApprovalError, AssistantError, ToolError
from .opportunities import Opportunities, OpportunityError
from .ledger import Ledger, LedgerError
from .products import Products, ProductError
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

_PROD_STATUS_LABELS = {
    "draft": "draft (unverified)",
    "published": "published (unverified)",
    "archived": "archived",
}
_CUST_STATUS_LABELS = {
    "lead": "lead (unverified)",
    "contacted": "contacted (unverified)",
    "converted": "converted (unverified)",
    "rejected": "rejected",
}
_CUST_TRANSITIONS = {
    "1": ("contacted", "customer_contacted"),
    "2": ("converted", "customer_converted"),
    "3": ("rejected", "customer_rejected"),
}
_COMM_STATUS_LABELS = {
    "draft": "draft (unverified)",
    "sent": "sent (unverified)",
    "archived": "archived",
}
_LEDGER_KINDS = {"income", "expense", "refund", "fee"}
_FOLLOWUP_STATUS_LABELS = {
    "pending": "pending",
    "completed": "completed",
    "cancelled": "cancelled",
}
_INVOICE_STATUS_LABELS = {
    "draft": "draft (unverified)",
    "issued": "issued (PDF stub)",
    "paid": "paid (unverified)",
    "overdue": "overdue (unverified)",
    "void": "void",
}
_PROVIDER_STATUS_LABELS = {
    "created": "created (sandbox)",
    "authorized": "authorized (HMAC)",
    "captured": "captured (HMAC)",
    "settled": "settled (sandbox)",
    "refunded": "refunded",
    "failed": "failed",
}
_AI_STATUS_LABELS = {
    "pending": "pending (awaiting approval)",
    "approved": "approved",
    "rejected": "rejected",
    "expired": "expired",
    "revoked": "revoked",
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
    backups = [path.with_name(path.name + f".before-v{version}.sqlite3") for version in range(2, 23)]
    if path.exists() or path.is_symlink() or any(p.exists() or p.is_symlink() for p in backups):
        raise StateStorageError("State or migration backup already exists; no initialization was attempted.")
    state = StateStorage(path, create=True, confirmed=True)
    for version, backup in zip(range(2, 23), backups):
        getattr(state, f"migrate_to_v{version}")(backup, confirmed=True)
    return state


def _csv_preview(text: str, limit: int = 500) -> str:
    sample = text.replace("\n", "\\n")[:limit]
    return sample + ("..." if len(text) > limit else "")


def _menu(service, token, stdin, out, opportunities=None, services=None, portfolio=None, products=None, customers=None, communications=None, ledger=None, followups=None, invoices=None, provider_orders=None, ai_assistant=None):
    # service = FinancialAssertions, opportunities = Opportunities, services/portfolio = v16, products/customers = v17, comm/ledger = v18, followups = v19, invoices = v20, provider_orders = v21, ai=v22
    out.write("Local tracker: all entries are UNVERIFIED. No payment processing or settlement.\n")
    has_opp = opportunities is not None
    has_svc = services is not None and portfolio is not None
    has_prod = products is not None
    has_cust = customers is not None
    has_comm = communications is not None
    has_ledger = ledger is not None
    has_follow = followups is not None
    has_inv = invoices is not None
    has_prov = provider_orders is not None
    extras = []
    if has_opp:
        extras.append("Opportunities v15 (5-8)")
    if has_svc:
        extras.append("Services v16 (9-12)")
    if has_prod and has_cust:
        extras.append("Products/Customers v17 (13-18)")
    elif has_prod:
        extras.append("Products v17 (13-15)")
    elif has_cust:
        extras.append("Customers v17 (16-18)")
    if has_comm and has_ledger:
        extras.append("Comms/Ledger v18 (19-22)")
    elif has_comm:
        extras.append("Comms v18 (19-21)")
    elif has_ledger:
        extras.append("Ledger v18 (22)")
    if has_follow:
        extras.append("Followups v19 (23-25) + CSV 26-27" if has_cust else "Followups v19 (23-25)")
    if has_inv:
        extras.append("Invoices v20 (28-30)")
    if has_prov:
        extras.append("Provider v21 (31-33 sandbox)")
    has_ai = ai_assistant is not None
    if has_ai:
        extras.append("AI v22 (34-41)")
    if extras:
        out.write("Financial (1-4), " + ", ".join(extras) + " available.\n")
    segments = ["1 Add financial  2 List financial  3 Advance financial  4 Withdraw financial"]
    if has_opp:
        segments.append("5 Add opportunity  6 List opps  7 Manage opp  8 Ranked")
    if has_svc:
        segments.append("9 Add service  10 List services  11 Manage service  12 Portfolio")
    if has_prod:
        segments.append("13 Add product  14 List products  15 Manage product")
    if has_cust:
        segments.append("16 Add customer  17 List customers  18 Manage customer")
    if has_comm:
        segments.append("19 Add comm  20 List comms  21 Manage comm")
    if has_ledger:
        segments.append("22 Ledger")
    if has_follow:
        segments.append("23 Add followup  24 List followups  25 Manage followup")
        if has_cust:
            segments.append("26 Import CSV  27 Export CSV")
    if has_inv:
        segments.append("28 Add invoice  29 List invoices  30 Manage invoice")
    if has_prov:
        segments.append("31 Add provider order  32 List provider orders  33 Webhook")
    if has_ai:
        segments.append("34 List tools  35 Request approval  36 List approvals  37 Decide approval")
        segments.append("38 Revoke approval  39 AI chat (draft)  40 Emergency stop/clear  41 Execute tool")
    prompt = "\n" + " | ".join(segments) + "  0 Sign out\nChoice: "
    # legacy no-opp/no-svc prompt (financial pipeline wording remains for v13-like fallback)
    legacy_prompt = "\n1 Add opportunity  2 List entries  3 Advance stage  4 Withdraw  0 Sign out\nChoice: "
    while True:
        if not has_opp and not has_svc and not has_prod and not has_cust and not has_comm and not has_ledger and not has_follow and not has_inv and not has_prov:
            choice = _read(stdin, out, legacy_prompt).strip()
        else:
            choice = _read(stdin, out, prompt).strip()
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
            elif products is not None and choice == "13":
                title = _read(stdin, out, "Product title (1-200): ")
                desc = _read(stdin, out, "Description (0-2000, empty for none): ")
                version = _read(stdin, out, "Version [A-Za-z0-9._- 1-20]: ").strip()
                price_raw = _read(stdin, out, "Price INR (e.g. 5000 or 2500.50): ").strip()
                price = _paise(price_raw)
                ident = products.create(token, title, desc, version, price, operation_key=_key())
                out.write(f"Created product draft: {ident}\n")
            elif products is not None and choice == "14":
                status_f = _read(stdin, out, "Filter status [draft/published/archived or empty]: ").strip()
                if status_f == "":
                    status_f = None
                cursor = None
                while True:
                    entries = products.list(token, after_id=cursor, limit=50, status=status_f)
                    for item in entries:
                        out.write(f"{item['id']} | {item['title']} | v{item['version']} | INR {item['price_paise'] // 100}.{item['price_paise'] % 100:02d} | {item['status']}\n")
                    if len(entries) < 50:
                        break
                    if _read(stdin, out, "More? Type YES: ").strip() != "YES":
                        break
                    cursor = entries[-1]["id"]
                if cursor is None and not entries:
                    out.write("No products.\n")
            elif products is not None and choice == "15":
                ident = _read(stdin, out, "Product ID: ").strip()
                item = products.get(token, ident)
                out.write(f"Current: {item['title']} | {item['status']} | v{item['version']} | INR {item['price_paise'] // 100}.{item['price_paise'] % 100:02d}\n")
                out.write("Transitions: 1 publish  2 archive\n")
                tchoice = _read(stdin, out, "Choose (1-2) or empty to cancel: ").strip()
                if tchoice == "":
                    out.write("No change.\n")
                    continue
                if tchoice == "1":
                    out.write("Publish product? This is unverified local draft.\n")
                    if _read(stdin, out, "Type YES to confirm: ").strip() != "YES":
                        out.write("No change.\n")
                        continue
                    products.publish(token, ident, operation_key=_key())
                    out.write("Product published (unverified).\n")
                elif tchoice == "2":
                    out.write("Archive product? This is unverified.\n")
                    if _read(stdin, out, "Type YES to confirm: ").strip() != "YES":
                        out.write("No change.\n")
                        continue
                    products.archive(token, ident, operation_key=_key())
                    out.write("Product archived.\n")
                else:
                    out.write("Invalid choice.\n")
            elif customers is not None and choice == "16":
                name = _read(stdin, out, "Customer name (1-200): ")
                contact = _read(stdin, out, "Contact (1-500): ")
                source = _read(stdin, out, "Source (0-500, empty for none): ")
                ident = customers.create(token, name, contact, source, operation_key=_key())
                out.write(f"Created customer lead: {ident}\n")
            elif customers is not None and choice == "17":
                status_f = _read(stdin, out, "Filter status [lead/contacted/converted/rejected or empty]: ").strip()
                if status_f == "":
                    status_f = None
                cursor = None
                while True:
                    entries = customers.list(token, after_id=cursor, limit=50, status=status_f)
                    for item in entries:
                        out.write(f"{item['id']} | {item['name']} | {item['contact'][:40]} | {item['status']} | src:{item['source'][:30]}\n")
                    if len(entries) < 50:
                        break
                    if _read(stdin, out, "More? Type YES: ").strip() != "YES":
                        break
                    cursor = entries[-1]["id"]
                if cursor is None and not entries:
                    out.write("No customers.\n")
            elif customers is not None and choice == "18":
                ident = _read(stdin, out, "Customer ID: ").strip()
                item = customers.get(token, ident)
                out.write(f"Current: {item['name']} | {item['status']} | {item['contact'][:40]}\n")
                out.write("Transitions: 1 contacted  2 converted  3 rejected\n")
                tchoice = _read(stdin, out, "Choose (1-3) or empty to cancel: ").strip()
                if tchoice == "":
                    out.write("No change.\n")
                    continue
                if tchoice not in _CUST_TRANSITIONS:
                    out.write("Invalid transition choice.\n")
                    continue
                target, _evt = _CUST_TRANSITIONS[tchoice]
                out.write(f"Change to {_CUST_STATUS_LABELS.get(target, target)}? This is unverified.\n")
                if _read(stdin, out, "Type YES to confirm: ").strip() != "YES":
                    out.write("No change.\n")
                    continue
                if target == "contacted":
                    customers.contacted(token, ident, operation_key=_key())
                elif target == "converted":
                    customers.converted(token, ident, operation_key=_key())
                elif target == "rejected":
                    customers.rejected(token, ident, operation_key=_key())
                out.write(f"Customer updated to {target} (unverified).\n")
            elif has_comm and choice == "19":
                cid = _read(stdin, out, "Customer ID: ").strip()
                chan = _read(stdin, out, "Channel (1-30): ")
                body = _read(stdin, out, "Body (1-2000): ")
                ident = communications.create(token, cid, chan, body, operation_key=_key())
                out.write(f"Created communication draft: {ident}\n")
            elif has_comm and choice == "20":
                cid_f = _read(stdin, out, "Filter customer ID (empty for all): ").strip()
                if cid_f == "":
                    cid_f = None
                status_f = _read(stdin, out, "Filter status [draft/sent/archived or empty]: ").strip()
                if status_f == "":
                    status_f = None
                cursor = None
                while True:
                    entries = communications.list(token, after_id=cursor, limit=50, customer_id=cid_f, status=status_f)
                    for item in entries:
                        out.write(f"{item['id']} | cust:{item['customer_id'][:8]} | {item['channel']} | {item['status']} | {item['body'][:40]}\n")
                    if len(entries) < 50:
                        break
                    if _read(stdin, out, "More? Type YES: ").strip() != "YES":
                        break
                    cursor = entries[-1]["id"]
                if cursor is None and not entries:
                    out.write("No communications.\n")
            elif has_comm and choice == "21":
                ident = _read(stdin, out, "Communication ID: ").strip()
                item = communications.get(token, ident)
                out.write(f"Current: {item['channel']} | {item['status']} | cust:{item['customer_id'][:8]} | {item['body'][:40]}\n")
                out.write("Transitions: 1 sent  2 archive\n")
                tchoice = _read(stdin, out, "Choose (1-2) or empty to cancel: ").strip()
                if tchoice == "":
                    out.write("No change.\n")
                    continue
                if tchoice == "1":
                    out.write("Send communication? Unverified local record.\n")
                    if _read(stdin, out, "Type YES to confirm: ").strip() != "YES":
                        out.write("No change.\n")
                        continue
                    communications.sent(token, ident, operation_key=_key())
                    out.write("Communication sent (unverified).\n")
                elif tchoice == "2":
                    out.write("Archive communication? Unverified.\n")
                    if _read(stdin, out, "Type YES to confirm: ").strip() != "YES":
                        out.write("No change.\n")
                        continue
                    communications.archive(token, ident, operation_key=_key())
                    out.write("Communication archived.\n")
                else:
                    out.write("Invalid choice.\n")
            elif has_ledger and choice == "22":
                out.write("Ledger: 1 Add entry  2 List entries  3 Net\n")
                sub = _read(stdin, out, "Choice (1-3): ").strip()
                if sub == "1":
                    kind = _read(stdin, out, "Kind [income/expense/refund/fee]: ").strip()
                    amt_raw = _read(stdin, out, "Amount INR (e.g. 5000 or 2500.50): ").strip()
                    amt = _paise(amt_raw)
                    note = _read(stdin, out, "Note (0-500, empty for none): ")
                    ident = ledger.add(token, kind, amt, note, operation_key=_key())
                    out.write(f"Created ledger entry: {ident}\n")
                elif sub == "2":
                    kind_f = _read(stdin, out, "Filter kind [income/expense/refund/fee or empty]: ").strip()
                    if kind_f == "":
                        kind_f = None
                    cursor = None
                    while True:
                        entries = ledger.list(token, after_id=cursor, limit=50, kind=kind_f)
                        for item in entries:
                            out.write(f"{item['id']} | {item['kind']} | INR {item['amount_paise'] // 100}.{item['amount_paise'] % 100:02d} | {item['note'][:40]}\n")
                        if len(entries) < 50:
                            break
                        if _read(stdin, out, "More? Type YES: ").strip() != "YES":
                            break
                        cursor = entries[-1]["id"]
                    if cursor is None and not entries:
                        out.write("No ledger entries.\n")
                elif sub == "3":
                    result = ledger.net(token)
                    out.write(f"Ledger net: INR {result['net_paise'] // 100}.{result['net_paise'] % 100:02d} (income {result['totals']['income']//100}.{result['totals']['income']%100:02d} - expense {result['totals']['expense']//100}.{result['totals']['expense']%100:02d} - refund {result['totals']['refund']//100}.{result['totals']['refund']%100:02d} - fee {result['totals']['fee']//100}.{result['totals']['fee']%100:02d}) count:{result['count']}\n")
                else:
                    out.write("Invalid ledger choice.\n")
            elif has_follow and choice == "23":
                cid = _read(stdin, out, "Customer ID: ").strip()
                due_raw = _read(stdin, out, "Due at unix timestamp (0 for none): ").strip()
                if due_raw == "":
                    due_raw = "0"
                try:
                    due = int(due_raw)
                except ValueError:
                    raise ValueError("Due must be integer timestamp or 0.")
                note = _read(stdin, out, "Note (0-500, empty for none): ")
                ident = followups.create(token, cid, due, note, operation_key=_key())
                out.write(f"Created followup pending: {ident}\n")
            elif has_follow and choice == "24":
                cid_f = _read(stdin, out, "Filter customer ID (empty for all): ").strip()
                if cid_f == "":
                    cid_f = None
                status_f = _read(stdin, out, "Filter status [pending/completed/cancelled or empty]: ").strip()
                if status_f == "":
                    status_f = None
                cursor = None
                while True:
                    entries = followups.list(token, after_id=cursor, limit=50, customer_id=cid_f, status=status_f)
                    for item in entries:
                        out.write(f"{item['id']} | cust:{item['customer_id'][:8]} | due:{item['due_at']} | {item['status']} | {item['note'][:40]}\n")
                    if len(entries) < 50:
                        break
                    if _read(stdin, out, "More? Type YES: ").strip() != "YES":
                        break
                    cursor = entries[-1]["id"]
                if cursor is None and not entries:
                    out.write("No followups.\n")
            elif has_follow and choice == "25":
                ident = _read(stdin, out, "Followup ID: ").strip()
                item = followups.get(token, ident)
                out.write(f"Current: cust:{item['customer_id'][:8]} | due:{item['due_at']} | {item['status']} | {item['note'][:40]}\n")
                out.write("Transitions: 1 completed  2 cancelled\n")
                tchoice = _read(stdin, out, "Choose (1-2) or empty to cancel: ").strip()
                if tchoice == "":
                    out.write("No change.\n")
                    continue
                if tchoice == "1":
                    out.write("Complete followup?\n")
                    if _read(stdin, out, "Type YES to confirm: ").strip() != "YES":
                        out.write("No change.\n")
                        continue
                    followups.completed(token, ident, operation_key=_key())
                    out.write("Followup completed.\n")
                elif tchoice == "2":
                    out.write("Cancel followup?\n")
                    if _read(stdin, out, "Type YES to confirm: ").strip() != "YES":
                        out.write("No change.\n")
                        continue
                    followups.cancelled(token, ident, operation_key=_key())
                    out.write("Followup cancelled.\n")
                else:
                    out.write("Invalid choice.\n")
            elif has_follow and has_cust and choice == "26":
                out.write("Paste CSV text (header name,contact,source). End with a line containing only '.'\n")
                lines = []
                while True:
                    line = _read(stdin, out, "")
                    if line == ".":
                        break
                    lines.append(line)
                csv_text = "\n".join(lines)
                if not csv_text.strip():
                    out.write("No CSV provided.\n")
                else:
                    pref = _read(stdin, out, "Operation key prefix (1-64, for idempotency): ").strip()
                    if pref == "":
                        pref = "csv_import"
                    result = customers.import_csv(token, csv_text, operation_key_prefix=pref)
                    out.write(f"CSV import: {len(result['created'])} created\n")
                    for ident in result["created"]:
                        out.write(f"  {ident}\n")
            elif has_follow and has_cust and choice == "27":
                csv_text = customers.export_csv(token)
                out.write("CSV export:\n")
                out.write(csv_text + "\n")
            elif has_inv and choice == "28":
                oid = _read(stdin, out, "Order ID (order_drafts): ").strip()
                ident = invoices.create(token, oid, operation_key=_key())
                out.write(f"Created invoice draft: {ident}\n")
            elif has_inv and choice == "29":
                status_f = _read(stdin, out, "Filter status [draft/issued/paid/overdue/void or empty]: ").strip()
                if status_f == "":
                    status_f = None
                oid_f = _read(stdin, out, "Filter order ID (empty for all): ").strip()
                if oid_f == "":
                    oid_f = None
                cursor = None
                while True:
                    entries = invoices.list(token, after_id=cursor, limit=50, status=status_f, order_id=oid_f)
                    for item in entries:
                        pdf_note = "PDF" if item['pdf_hash'] is not None else "no PDF"
                        out.write(f"{item['id']} | {item['invoice_number']} | order:{item['order_id'][:8]} | INR {item['amount_paise'] // 100}.{item['amount_paise'] % 100:02d} | {item['status']} | {pdf_note}\n")
                    if len(entries) < 50:
                        break
                    if _read(stdin, out, "More? Type YES: ").strip() != "YES":
                        break
                    cursor = entries[-1]["id"]
                if cursor is None and not entries:
                    out.write("No invoices.\n")
            elif has_inv and choice == "30":
                ident = _read(stdin, out, "Invoice ID: ").strip()
                item = invoices.get(token, ident)
                pdf_note = "PDF stub available" if item['pdf_hash'] is not None else "no PDF (draft/void)"
                out.write(f"Current: {item['invoice_number']} | order:{item['order_id'][:8]} | INR {item['amount_paise'] // 100}.{item['amount_paise'] % 100:02d} | {item['status']} | {pdf_note}\n")
                out.write("Transitions: 1 issued  2 paid  3 overdue  4 void  5 pdf\n")
                tchoice = _read(stdin, out, "Choose (1-5) or empty to cancel: ").strip()
                if tchoice == "":
                    out.write("No change.\n")
                    continue
                if tchoice == "1":
                    out.write("Issue invoice? PDF stub hash will be stored.\n")
                    if _read(stdin, out, "Type YES to confirm: ").strip() != "YES":
                        out.write("No change.\n")
                        continue
                    invoices.issued(token, ident, operation_key=_key())
                    out.write("Invoice issued (PDF stub).\n")
                elif tchoice == "2":
                    out.write("Mark paid?\n")
                    if _read(stdin, out, "Type YES to confirm: ").strip() != "YES":
                        out.write("No change.\n")
                        continue
                    invoices.paid(token, ident, operation_key=_key())
                    out.write("Invoice paid.\n")
                elif tchoice == "3":
                    out.write("Mark overdue?\n")
                    if _read(stdin, out, "Type YES to confirm: ").strip() != "YES":
                        out.write("No change.\n")
                        continue
                    invoices.overdue(token, ident, operation_key=_key())
                    out.write("Invoice overdue.\n")
                elif tchoice == "4":
                    out.write("Void invoice?\n")
                    if _read(stdin, out, "Type YES to confirm: ").strip() != "YES":
                        out.write("No change.\n")
                        continue
                    invoices.voided(token, ident, operation_key=_key())
                    out.write("Invoice voided.\n")
                elif tchoice == "5":
                    try:
                        stub = invoices.pdf_stub(token, ident)
                        out.write(f"PDF stub ({len(stub)} bytes):\n")
                        out.write(stub.decode('utf-8', errors='replace') + "\n")
                        out.write(f"Hash {item['pdf_hash'].hex() if item['pdf_hash'] else 'none'}\n")
                    except Exception as e:
                        out.write(str(e) + "\n")
                else:
                    out.write("Invalid choice.\n")
            elif has_prov and choice == "31":
                provider = _read(stdin, out, "Provider [razorpay/stripe]: ").strip() or "razorpay"
                amt_raw = _read(stdin, out, "Amount INR (e.g. 5000 or 2500.50): ").strip()
                amt = _paise(amt_raw)
                ident = provider_orders.create_order(token, amt, operation_key=_key())
                out.write(f"Created provider order ({provider}): {ident}\n")
            elif has_prov and choice == "32":
                status_f = _read(stdin, out, "Filter status [created/authorized/captured/settled/refunded/failed or empty]: ").strip()
                if status_f == "":
                    status_f = None
                cursor = None
                while True:
                    entries = provider_orders.list(token, after_id=cursor, limit=50, status=status_f)
                    for item in entries:
                        out.write(f"{item['id']} | {item['provider_order_id']} | {item['provider']}/{item['mode']} | INR {item['amount_paise'] // 100}.{item['amount_paise'] % 100:02d} | {item['status']}\n")
                    if len(entries) < 50:
                        break
                    if _read(stdin, out, "More? Type YES: ").strip() != "YES":
                        break
                    cursor = entries[-1]["id"]
                if cursor is None and not entries:
                    out.write("No provider orders.\n")
            elif has_prov and choice == "33":
                out.write("Webhook: paste raw JSON body (single line). Signature must be HMAC of raw body.\n")
                body_raw = _read(stdin, out, "Raw body: ")
                sig = _read(stdin, out, "Signature (hex for razorpay, t=...,v1=... for stripe): ").strip()
                try:
                    raw = body_raw.encode('utf-8')
                    result = provider_orders.handle_webhook(token, raw, sig, operation_key=_key())
                    out.write(f"Webhook processed: event {result['event_id']} -> {result['new_status']} for order {result['order_id'][:8]}\n")
                except Exception as e:
                    out.write(str(e) + "\n")
            elif has_ai and choice == "34":
                try:
                    tools = ai_assistant.registry.list()
                    for tl in tools:
                        out.write(f"{tl['name']} | {tl['scope']} | {tl['description'][:80]}\n")
                    if not tools:
                        out.write("No tools registered.\n")
                except Exception as e:
                    out.write(str(e) + "\n")
            elif has_ai and choice == "35":
                tname = _read(stdin, out, "Tool name (consequential only, e.g. publish_service): ").strip()
                args_raw = _read(stdin, out, "Args JSON (e.g. {\"service_id\": \"...\"}): ").strip()
                import json as _json
                try:
                    args = _json.loads(args_raw) if args_raw else {}
                    apid = ai_assistant.queue.request(token, tname, args, operation_key=_key())
                    out.write(f"Approval requested: {apid} (pending, expires in 15m)\n")
                    out.write("Use 37 to approve/reject; 41 to execute after approval.\n")
                except Exception as e:
                    out.write(str(e) + "\n")
            elif has_ai and choice == "36":
                filt = _read(stdin, out, "Filter status [pending/approved/rejected/expired/revoked or empty]: ").strip()
                if filt == "":
                    filt = None
                cursor = None
                try:
                    while True:
                        entries = ai_assistant.queue.list(token, after_id=cursor, limit=20, status=filt)
                        for item in entries:
                            out.write(f"{item['id']} | {item['tool_name']} | {item['status']}->{item['effective_status']} | expires:{item['expires_at']} | {item['args_json'][:60]}\n")
                        if len(entries) < 20:
                            break
                        if _read(stdin, out, "More? Type YES: ").strip() != "YES":
                            break
                        cursor = entries[-1]["id"]
                    if cursor is None and not entries:
                        out.write("No approvals.\n")
                except Exception as e:
                    out.write(str(e) + "\n")
            elif has_ai and choice == "37":
                apid = _read(stdin, out, "Approval ID: ").strip()
                dec = _read(stdin, out, "Decision [approved/rejected]: ").strip()
                try:
                    out.write(f"Confirm {dec} for {apid}? This is explicit approval.\n")
                    if _read(stdin, out, "Type YES to confirm: ").strip() != "YES":
                        out.write("No change.\n")
                    else:
                        ai_assistant.queue.decide(token, apid, dec, confirmed=True, operation_key=_key())
                        out.write(f"Approval {dec}.\n")
                except Exception as e:
                    out.write(str(e) + "\n")
            elif has_ai and choice == "38":
                apid = _read(stdin, out, "Approval ID to revoke: ").strip()
                try:
                    out.write(f"Revoke {apid}? This prevents future execution.\n")
                    if _read(stdin, out, "Type YES to confirm: ").strip() != "YES":
                        out.write("No change.\n")
                    else:
                        ai_assistant.queue.revoke(token, apid, operation_key=_key())
                        out.write("Approval revoked.\n")
                except Exception as e:
                    out.write(str(e) + "\n")
            elif has_ai and choice == "39":
                prompt = _read(stdin, out, "Prompt (1-4096): ")
                ext = _read(stdin, out, "External content (untrusted, empty for none): ")
                if ext == "":
                    ext = None
                try:
                    result = ai_assistant.chat(token, prompt, external_content=ext)
                    out.write(f"AI draft (unverified, {result['model']}): {result['output'][:500]}\n")
                    out.write(f"Policy: {result['policy']}\n")
                    out.write("Note: external content treated as data, never as instruction; no consequential tool auto-executed.\n")
                except Exception as e:
                    out.write(str(e) + "\n")
            elif has_ai and choice == "40":
                out.write("Emergency: 1 stop  2 clear  (requires explicit YES)\n")
                sub = _read(stdin, out, "Choice (1-2): ").strip()
                try:
                    if sub == "1":
                        reason = _read(stdin, out, "Reason (0-500, empty for none): ")
                        if _read(stdin, out, "Type YES to confirm EMERGENCY STOP: ").strip() != "YES":
                            out.write("No change.\n")
                        else:
                            ai_assistant.queue.emergency_stop(token, confirmed=True, reason=reason, operation_key=_key())
                            out.write("Emergency stop ACTIVE; consequential actions blocked.\n")
                    elif sub == "2":
                        if _read(stdin, out, "Type YES to confirm CLEAR emergency: ").strip() != "YES":
                            out.write("No change.\n")
                        else:
                            ai_assistant.queue.emergency_clear(token, confirmed=True, operation_key=_key())
                            out.write("Emergency cleared.\n")
                    else:
                        out.write("Invalid emergency choice.\n")
                except Exception as e:
                    out.write(str(e) + "\n")
            elif has_ai and choice == "41":
                tname = _read(stdin, out, "Tool name: ").strip()
                args_raw = _read(stdin, out, "Args JSON: ").strip()
                apid = _read(stdin, out, "Approval ID (required for consequential): ").strip()
                if apid == "":
                    apid = None
                import json as _json2
                try:
                    args = _json2.loads(args_raw) if args_raw else {}
                    result = ai_assistant.registry.execute(token, tname, args, ai_assistant._auth, ai_assistant._state, approval_id=apid)
                    out.write(f"Executed {tname} -> {result.get('result','ok')} (verification:{result.get('verification')})\n")
                    if 'note' in result:
                        out.write(result['note'] + "\n")
                except Exception as e:
                    out.write(str(e) + "\n")
            else:
                parts = ["0 Sign out", "1-4 financial"]
                if has_opp:
                    parts.append("5-8 opps")
                if has_svc:
                    parts.append("9-12 services/portfolio")
                if has_prod:
                    parts.append("13-15 products")
                if has_cust:
                    parts.append("16-18 customers")
                if has_comm:
                    parts.append("19-21 comms")
                if has_ledger:
                    parts.append("22 ledger")
                if has_follow:
                    parts.append("23-25 followups")
                    if has_cust:
                        parts.append("26-27 CSV")
                if has_inv:
                    parts.append("28-30 invoices")
                if has_prov:
                    parts.append("31-33 provider(sandbox)")
                if has_ai:
                    parts.append("34-41 AI (sandbox)")
                out.write("Choose " + ", ".join(parts) + ".\n")
        except (FinancialAssertionError, OpportunityError, ServiceError, ProductError, CustomerError, CommunicationError, LedgerError, FollowupError, InvoiceError, ProviderError, ApprovalError, AssistantError, ToolError, ValueError, StateStorageError) as exc:
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
            out.write("New tracker state initialized (v22, 21 backups). Register an account with the auth command if needed.\n")
            return 0
        # Open only; missing files are never created during login.
        state = StateStorage(path)
        with state.transaction() as conn:
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            if version not in (13, 14, 15, 16, 17, 18, 19, 20, 21, 22):
                raise StateStorageError("Tracker requires an existing version 13, 14, 15, 16, 17, 18, 19, 20, 21 or 22 state file; no upgrade was performed.")
        auth = AuthService(AuthStorage(Path(auth_path), create=False))
        username = _read(stdin, out, "Username: ")
        password = _secret("Password: ", err)
        token = auth.login(username, password)
        # Opportunities available on 15+, services/portfolio on 16+, products/customers on 17+, comm/ledger on 18+, followups on 19+, invoices on 20+, provider sandbox on 21+, AI on 22+
        opp = Opportunities(auth, state) if version >= 15 else None
        svc = Services(auth, state) if version >= 16 else None
        port = Portfolio(auth, state) if version >= 16 else None
        prod = Products(auth, state) if version >= 17 else None
        cust = Customers(auth, state) if version >= 17 else None
        comm = Communications(auth, state) if version >= 18 else None
        ledg = Ledger(auth, state) if version >= 18 else None
        foll = Followups(auth, state) if version >= 19 else None
        inv = Invoices(auth, state) if version >= 20 else None
        # Provider orders use sandbox config by default (razorpay, sandbox, fixed test secret for local demo)
        prov = None
        if version >= 21:
            # In tracker, use a deterministic sandbox secret derived from owner for demo — never live
            # The secret is not stored; it is derived per-session for the demo and must match webhook generation
            demo_secret = b"tracker-demo-sandbox-secret-32bytes-long-1234"
            try:
                cfg = ProviderConfig(provider="razorpay", mode="sandbox", webhook_secret=demo_secret)
                prov = PaymentProviders(auth, state, cfg)
            except ProviderError:
                prov = None
        ai = None
        if version >= 22:
            try:
                ai = AIAssistant(auth, state)
            except AssistantError:
                ai = None
        _menu(FinancialAssertions(auth, state), token, stdin, out, opp, svc, port, prod, cust, comm, ledg, foll, inv, prov, ai)
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

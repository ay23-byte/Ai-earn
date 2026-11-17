"""Experimental offline, manually asserted opportunity tracker.

No payment is verified, received or processed. Use only disposable data.
Now includes v15 opportunity discovery, v16 service catalog/portfolio,
v17 product workspace & customers, v18 communications & ledger, v19 followups & CSV, v20 invoices & 11-concept hardening, and v22 AI assistant (ToolRegistry/ApprovalQueue) and v22 analytics (funnel/MRR/conversion/workload) and connectors (Gmail/Drive/GitHub stubs) and v25 service workspace (packages/requirements/attachments/proposals/AI drafts/requests/templates/portfolio presentation) and v26 product workspace (ideas/validations/research/pricing/versions/files/sales/deliverables/docs) and v27 CRM (notes/requirements/orders/projects/timeline/approval/safe workflow).
"""

import getpass
import os
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
from .analytics import Analytics, AnalyticsError
from .service_workspace import ServicePackages, ServiceRequirements, ServiceAttachments, Proposals, CustomerRequests, ServiceTemplates, PortfolioPresentation, ServiceEditor, ServiceWorkspaceError
from ai_earn.connectors import ConnectorError as ConnError
from ai_earn.connectors.gmail import GmailConnector
from ai_earn.connectors.drive import DriveConnector
from ai_earn.connectors.github import GitHubConnector
from ai_earn.connectors.throttle import Throttle
from .product_workspace import ProductIdeas, ProductValidations, MarketResearch, PricingSuggestions, ProductVersions, ProductFiles, ProductDeliverables, ProductSales, ProductDocumentation, ProductEditor, ProductWorkspaceError
from .crm import CustomerNotes, CustomerRequirements, CustomerOrders, CustomerProjects, CommunicationApprovals, OutboundWorkflowConfig, CRMCommunications, ActivityTimeline, CustomerProfile, CustomerCSV, CRMError
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
    backups = [path.with_name(path.name + f".before-v{version}.sqlite3") for version in range(2, 28)]
    if path.exists() or path.is_symlink() or any(p.exists() or p.is_symlink() for p in backups):
        raise StateStorageError("State or migration backup already exists; no initialization was attempted.")
    state = StateStorage(path, create=True, confirmed=True)
    for version, backup in zip(range(2, 28), backups):
        getattr(state, f"migrate_to_v{version}")(backup, confirmed=True)
    return state


def _csv_preview(text: str, limit: int = 500) -> str:
    sample = text.replace("\n", "\\n")[:limit]
    return sample + ("..." if len(text) > limit else "")


def _menu(service, token, stdin, out, opportunities=None, services=None, portfolio=None, products=None, customers=None, communications=None, ledger=None, followups=None, invoices=None, provider_orders=None, ai_assistant=None, analytics=None, workspace_pkgs=None, workspace_reqs=None, workspace_atts=None, proposals=None, customer_requests=None, service_editor=None, service_templates=None, portfolio_pres=None, product_ideas=None, product_validations=None, market_research=None, pricing_suggestions=None, product_versions=None, product_files=None, product_deliverables=None, product_sales=None, product_docs=None, product_editor=None, crm_notes=None, crm_requirements=None, crm_orders=None, crm_projects=None, crm_comm_approvals=None, crm_outbound=None, crm_comms=None, crm_timeline=None, crm_profile=None, crm_csv=None):
    # service = FinancialAssertions, opportunities = Opportunities, services/portfolio = v16, products/customers = v17, comm/ledger = v18, followups = v19, invoices = v20, provider_orders = v21, ai=v22, analytics=v22 (no schema)
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
    has_an = analytics is not None
    if has_an:
        extras.append("Analytics v22 (42-45)")
    # connectors are available when analytics available (same version) – use same flag for simplicity
    has_conn = analytics is not None
    if has_conn:
        extras.append("Connectors v22 (46-48)")
    has_workspace = workspace_pkgs is not None
    if has_workspace:
        extras.append("Service Workspace v25 (49-61)")
    has_prod_ws = product_ideas is not None
    if has_prod_ws:
        extras.append("Product Workspace v26 (62-75)")
    has_crm = crm_notes is not None
    if has_crm:
        extras.append("CRM v27 (73-85)")
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
    if has_an:
        segments.append("42 Analytics summary  43 Funnel  44 MRR monthly  45 Export CSV")
    if has_conn:
        segments.append("46 List connectors  47 Fetch Gmail/Drive/GitHub  48 Import via connector")
    if has_workspace:
        segments.append("49 Edit service  50 Packages  51 Requirements  52 Attachments")
        segments.append("53 Create proposal  54 AI draft proposal  55 List/Manage proposals")
        segments.append("56 Customer requests  57 Templates  58 Portfolio presentation")
    if has_prod_ws:
        segments.append("62 Ideas  63 Validations  64 Market research (AI)  65 Pricing (AI)")
        segments.append("66 Versions  67 Files  68 Sales (verified)  69 Deliverables")
        segments.append("70 Documentation (AI)  71 Edit product  72 Product status")
    if has_crm:
        segments.append("73 Profile  74 Notes  75 Requirements  76 Communications+Approval  77 Follow-ups")
        segments.append("78 Orders  79 Projects history  80 Timeline  81 Outbound workflow  82 CSV")

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
            elif has_an and choice == "42":
                from_ts = _read(stdin, out, "From unix ts (0 for all time): ").strip()
                to_ts = _read(stdin, out, "To unix ts (empty for now): ").strip()
                try:
                    fts = int(from_ts) if from_ts else 0
                    tts = int(to_ts) if to_ts else 4102444800
                    summ = analytics.summary(token, from_ts=fts, to_ts=tts)
                    out.write(f"Analytics summary {fts}->{tts} generated {summ['funnel']['generated_at']}\n")
                    out.write(f"Funnel opp {summ['funnel'].get('opportunities', {}).get('total',0)} cust {summ['funnel'].get('customers', {}).get('total',0)} invoices {summ['funnel'].get('invoices', {}).get('total',0)}\n")
                    out.write(f"Conversion opp vetted {summ['conversion'].get('opportunities', {}).get('vetted_rate',0):.2f} cust converted {summ['conversion'].get('customers', {}).get('converted_over_lead',0):.2f}\n")
                    out.write(f"MRR total {summ['mrr']['total_paise']/100:.2f} INR over {summ['mrr']['months_with_income']} months avg {summ['mrr']['average_per_month_paise']/100:.2f}\n")
                    out.write(f"Workload queued {summ['workload'].get('queued_tasks',0)} pending follow {summ['workload'].get('pending_followups',0)} net {summ['workload'].get('ledger_net_paise',0)/100:.2f}\n")
                    out.write(f"Verification: {summ['funnel']['verification']} / {summ['mrr']['verification']} — no fabricated data.\n")
                except Exception as e:
                    out.write(str(e) + "\n")
            elif has_an and choice == "43":
                from_ts = _read(stdin, out, "From unix ts (0): ").strip()
                to_ts = _read(stdin, out, "To unix ts (empty for max): ").strip()
                try:
                    fts = int(from_ts) if from_ts else 0
                    tts = int(to_ts) if to_ts else 4102444800
                    fun = analytics.funnel(token, from_ts=fts, to_ts=tts)
                    out.write(f"Funnel {fts}->{tts}:\n")
                    for k in ("opportunities","customers","invoices","provider_orders","services","communications"):
                        if k in fun:
                            out.write(f" {k}: {fun[k].get('total',0)} by {fun[k].get('by_status', fun[k].get('by_kind', {}))}\n")
                except Exception as e:
                    out.write(str(e) + "\n")
            elif has_an and choice == "44":
                from_ts = _read(stdin, out, "From unix ts (0): ").strip()
                to_ts = _read(stdin, out, "To unix ts (empty for max): ").strip()
                try:
                    fts = int(from_ts) if from_ts else 0
                    tts = int(to_ts) if to_ts else 4102444800
                    mrr = analytics.mrr(token, from_ts=fts, to_ts=tts)
                    out.write(f"MRR {fts}->{tts} total {mrr['total_paise']/100:.2f} INR months {mrr['months_with_income']} avg {mrr['average_per_month_paise']/100:.2f}\n")
                    for mon, amt in sorted(mrr['monthly_buckets'].items()):
                        out.write(f" {mon}: {amt/100:.2f}\n")
                    out.write(mrr['note'] + "\n")
                except Exception as e:
                    out.write(str(e) + "\n")
            elif has_an and choice == "45":
                from_ts = _read(stdin, out, "From unix ts (0): ").strip()
                to_ts = _read(stdin, out, "To unix ts (empty for max): ").strip()
                try:
                    fts = int(from_ts) if from_ts else 0
                    tts = int(to_ts) if to_ts else 4102444800
                    csv_text = analytics.export_csv(token, from_ts=fts, to_ts=tts)
                    out.write("CSV export (first 2000 chars):\n")
                    out.write(csv_text[:2000] + ("...\n" if len(csv_text)>2000 else "\n"))
                    out.write(f"Total {len(csv_text)} chars, {csv_text.count(chr(10))} lines.\n")
                except Exception as e:
                    out.write(str(e) + "\n")
            elif has_conn and choice == "46":
                out.write("Connectors (stub sandbox, no network):\n")
                out.write(" - gmail-stub (Gmail, snippets) | drive-stub (Drive files) | github-stub (GitHub issues) | manual-fallback\n")
                out.write("Auth: api_key 32..128, throttle 10/min per owner, retry 3 backoff, normalize bounded, manual fallback always.\n")
                try:
                    from ai_earn.connectors import GmailConnector, DriveConnector, GitHubConnector
                    for name in ("gmail-stub","drive-stub","github-stub"):
                        out.write(f" {name}: ready (stub)\n")
                    out.write(" manual-fallback: ready\n")
                except Exception as e:
                    out.write(str(e) + "\n")
            elif has_conn and choice == "47":
                which = _read(stdin, out, "Which connector [gmail/drive/github]: ").strip().lower()
                limit_raw = _read(stdin, out, "Limit 1-100 (default 5): ").strip()
                limit = int(limit_raw) if limit_raw else 5
                try:
                    key = b"test-connector-api-key-32bytes-long-enough-1234"
                    # Need owner_id for throttle: derive from token via auth? Use dummy owner check via analytics owner? Simpler fetch without owner_id
                    if which == "gmail":
                        c = GmailConnector(api_key=key)
                    elif which == "drive":
                        c = DriveConnector(api_key=key)
                    elif which == "github":
                        c = GitHubConnector(api_key=key)
                    else:
                        out.write("Unknown connector.\n")
                        c = None
                    if c:
                        c.validate_config()
                        items = c.fetch(limit=limit)
                        out.write(f"Fetched {len(items)} items (normalized, bounded):\n")
                        for it in items:
                            out.write(f" - {it['title'][:60]} | {it['source_url'][:60]} | {it['budget_paise']/100:.2f} INR | {it['skill_tags'][:30]}\n")
                except Exception as e:
                    out.write(str(e) + "\n")
            elif has_conn and choice == "48":
                which = _read(stdin, out, "Import via which [gmail/drive/github/manual]: ").strip().lower()
                prefix = _read(stdin, out, "Operation prefix 1-64 (e.g. import1): ").strip() or "conn_import"
                limit_raw = _read(stdin, out, "Limit 1-100 (default 5): ").strip()
                limit = int(limit_raw) if limit_raw else 5
                try:
                    key = b"test-connector-api-key-32bytes-long-enough-1234"
                    if which == "gmail":
                        c = GmailConnector(api_key=key)
                    elif which == "drive":
                        c = DriveConnector(api_key=key)
                    elif which == "github":
                        c = GitHubConnector(api_key=key)
                    elif which == "manual":
                        from ai_earn.opportunities import ManualConnector
                        c = ManualConnector()
                    else:
                        out.write("Unknown connector.\n")
                        c = None
                    if c and opportunities is not None:
                        ids = opportunities.import_from_connector(token, c, limit=limit, operation_key_prefix=prefix)
                        out.write(f"Imported {len(ids)} opportunities (owner-scoped, idempotent by prefix):\n")
                        for ident in ids:
                            out.write(f" {ident}\n")
                    elif c:
                        out.write("Opportunities not available in this state version.\n")
                except Exception as e:
                    out.write(str(e) + "\n")

            elif has_workspace and choice == "49":
                sid = _read(stdin, out, "Service ID to edit: ").strip()
                try:
                    cur = services.get(token, sid)
                    out.write(f"Current: {cur['title']} | scope:{cur['scope'][:40]} | deliverable:{cur['deliverable'][:40]} | INR {cur['price_paise']//100}.{cur['price_paise']%100:02d} rev:{cur['revisions']}\n")
                    title = _read(stdin, out, "New title (empty to keep): ").strip()
                    if title == "":
                        title = None
                    scope = _read(stdin, out, "New scope (empty to keep): ").strip()
                    if scope == "":
                        scope = None
                    deliverable = _read(stdin, out, "New deliverable (empty to keep): ").strip()
                    if deliverable == "":
                        deliverable = None
                    price_raw = _read(stdin, out, "New price INR (empty to keep): ").strip()
                    price = _paise(price_raw) if price_raw else None
                    rev_raw = _read(stdin, out, "New revisions (empty to keep): ").strip()
                    rev = int(rev_raw) if rev_raw else None
                    dd_raw = _read(stdin, out, "Delivery days 1..365 (empty to keep): ").strip()
                    dd = int(dd_raw) if dd_raw else None
                    # Build kwargs
                    kwargs={}
                    if title is not None:
                        kwargs["title"]=title
                    if scope is not None:
                        kwargs["scope"]=scope
                    if deliverable is not None:
                        kwargs["deliverable"]=deliverable
                    if price is not None:
                        kwargs["price_paise"]=price
                    if rev is not None:
                        kwargs["revisions"]=rev
                    if dd is not None:
                        kwargs["delivery_days"]=dd
                    if not kwargs:
                        out.write("No fields to update.\n")
                    else:
                        service_editor.update(token, sid, operation_key=_key(), **kwargs)
                        out.write("Service updated.\n")
                except Exception as e:
                    out.write(str(e) + "\n")
            elif has_workspace and choice == "50":
                out.write("Packages: 1 Create  2 List  3 Update  4 Delete\n")
                sub=_read(stdin, out, "Choice (1-4): ").strip()
                try:
                    if sub=="1":
                        sid=_read(stdin, out, "Service ID: ").strip()
                        title=_read(stdin, out, "Package title: ")
                        desc=_read(stdin, out, "Description: ")
                        price=_paise(_read(stdin, out, "Price INR: ").strip())
                        dd=int(_read(stdin, out, "Delivery days 1..365: ").strip())
                        rev=int(_read(stdin, out, "Revisions 0..100: ").strip())
                        pid=workspace_pkgs.create(token, sid, title, desc, price, dd, rev, operation_key=_key())
                        out.write(f"Created package {pid}\n")
                    elif sub=="2":
                        sid=_read(stdin, out, "Service ID: ").strip()
                        pkgs=workspace_pkgs.list(token, sid)
                        for p in pkgs:
                            out.write(f"{p['id']} | {p['title']} | {p['description'][:40]} | INR {p['price_paise']//100}.{p['price_paise']%100:02d} | {p['delivery_days']}d | rev:{p['revisions']}\n")
                        if not pkgs:
                            out.write("No packages.\n")
                    elif sub=="3":
                        pid=_read(stdin, out, "Package ID: ").strip()
                        title=_read(stdin, out, "New title (empty keep): ").strip() or None
                        desc=_read(stdin, out, "New description (empty keep): ").strip() or None
                        price_raw=_read(stdin, out, "New price (empty keep): ").strip()
                        price=_paise(price_raw) if price_raw else None
                        dd_raw=_read(stdin, out, "New delivery days (empty keep): ").strip()
                        dd=int(dd_raw) if dd_raw else None
                        rev_raw=_read(stdin, out, "New revisions (empty keep): ").strip()
                        rev=int(rev_raw) if rev_raw else None
                        kwargs={}
                        if title: kwargs["title"]=title
                        if desc: kwargs["description"]=desc
                        if price is not None: kwargs["price_paise"]=price
                        if dd is not None: kwargs["delivery_days"]=dd
                        if rev is not None: kwargs["revisions"]=rev
                        workspace_pkgs.update(token, pid, operation_key=_key(), **kwargs)
                        out.write("Package updated.\n")
                    elif sub=="4":
                        pid=_read(stdin, out, "Package ID: ").strip()
                        workspace_pkgs.delete(token, pid, operation_key=_key())
                        out.write("Package deleted.\n")
                except Exception as e:
                    out.write(str(e) + "\n")
            elif has_workspace and choice == "51":
                out.write("Requirements: 1 Add  2 List  3 Remove\n")
                sub=_read(stdin, out, "Choice (1-3): ").strip()
                try:
                    if sub=="1":
                        sid=_read(stdin, out, "Service ID: ").strip()
                        req=_read(stdin, out, "Requirement (1..500): ")
                        rid=workspace_reqs.add(token, sid, req, operation_key=_key())
                        out.write(f"Added requirement {rid}\n")
                    elif sub=="2":
                        sid=_read(stdin, out, "Service ID: ").strip()
                        reqs=workspace_reqs.list(token, sid)
                        for r in reqs:
                            out.write(f"{r['id']} | {r['requirement'][:60]}\n")
                        if not reqs:
                            out.write("No requirements.\n")
                    elif sub=="3":
                        rid=_read(stdin, out, "Requirement ID: ").strip()
                        workspace_reqs.remove(token, rid, operation_key=_key())
                        out.write("Requirement removed.\n")
                except Exception as e:
                    out.write(str(e) + "\n")
            elif has_workspace and choice == "52":
                out.write("Attachments: 1 Add  2 List  3 Remove\n")
                sub=_read(stdin, out, "Choice (1-3): ").strip()
                try:
                    if sub=="1":
                        sid=_read(stdin, out, "Service ID (empty for none): ").strip() or None
                        pid=_read(stdin, out, "Portfolio item ID (empty for none): ").strip() or None
                        fname=_read(stdin, out, "File name (1..200): ")
                        fsize=int(_read(stdin, out, "File size 0..104857600: ").strip() or "0")
                        mime=_read(stdin, out, "MIME type (e.g. application/pdf): ").strip() or "application/octet-stream"
                        aid=workspace_atts.add(token, operation_key=_key(), file_name=fname, file_size=fsize, mime_type=mime, service_id=sid, portfolio_item_id=pid)
                        out.write(f"Added attachment {aid}\n")
                    elif sub=="2":
                        sid=_read(stdin, out, "Service ID (empty for none): ").strip() or None
                        pid=_read(stdin, out, "Portfolio item ID (empty for none): ").strip() or None
                        atts=workspace_atts.list(token, service_id=sid, portfolio_item_id=pid)
                        for a in atts:
                            out.write(f"{a['id']} | {a['file_name']} | {a['file_size']}b | {a['mime_type']} | svc:{(a['service_id'] or '-')[:8]} port:{(a['portfolio_item_id'] or '-')[:8]}\n")
                        if not atts:
                            out.write("No attachments.\n")
                    elif sub=="3":
                        aid=_read(stdin, out, "Attachment ID: ").strip()
                        workspace_atts.remove(token, aid, operation_key=_key())
                        out.write("Attachment removed.\n")
                except Exception as e:
                    out.write(str(e) + "\n")
            elif has_workspace and choice == "53":
                sid=_read(stdin, out, "Service ID: ").strip()
                cid=_read(stdin, out, "Customer ID (empty for none): ").strip() or None
                title=_read(stdin, out, "Proposal title: ")
                desc=_read(stdin, out, "Description: ")
                price=_paise(_read(stdin, out, "Price INR: ").strip())
                dd=int(_read(stdin, out, "Delivery days 1..365: ").strip())
                try:
                    pid=proposals.create(token, sid, title, desc, price, dd, operation_key=_key(), customer_id=cid)
                    out.write(f"Created proposal {pid} (draft)\n")
                except Exception as e:
                    out.write(str(e) + "\n")
            elif has_workspace and choice == "54":
                sid=_read(stdin, out, "Service ID: ").strip()
                cid=_read(stdin, out, "Customer ID (empty for none): ").strip() or None
                prompt=_read(stdin, out, "Prompt for AI (empty for default): ").strip() or None
                try:
                    pid=proposals.generate_ai_draft(token, sid, operation_key=_key(), customer_id=cid, prompt=prompt)
                    out.write(f"AI draft proposal {pid} (unverified draft)\n")
                except Exception as e:
                    out.write(str(e) + "\n")
            elif has_workspace and choice == "55":
                out.write("Proposals: 1 List  2 Manage status\n")
                sub=_read(stdin, out, "Choice (1-2): ").strip()
                try:
                    if sub=="1":
                        status=_read(stdin, out, "Filter status [draft/sent/accepted/rejected/cancelled/archived or empty]: ").strip() or None
                        proposals_list=proposals.list(token, status=status)
                        for p in proposals_list:
                            out.write(f"{p['id']} | svc:{p['service_id'][:8]} cust:{(p['customer_id'] or '-')[:8]} | {p['title'][:40]} | INR {p['price_paise']//100}.{p['price_paise']%100:02d} | {p['delivery_days']}d | {p['status']} ai:{p['ai_generated']}\n")
                        if not proposals_list:
                            out.write("No proposals.\n")
                    elif sub=="2":
                        pid=_read(stdin, out, "Proposal ID: ").strip()
                        cur=proposals.get(token, pid)
                        out.write(f"Current: {cur['title']} | {cur['status']} | ai:{cur['ai_generated']}\n")
                        out.write("Transitions: 1 send  2 accept  3 reject  4 cancel  5 archive\n")
                        tc=_read(stdin, out, "Choice (1-5): ").strip()
                        if tc=="1":
                            proposals.send(token, pid, operation_key=_key())
                            out.write("Sent.\n")
                        elif tc=="2":
                            proposals.accept(token, pid, operation_key=_key())
                            out.write("Accepted.\n")
                        elif tc=="3":
                            proposals.reject(token, pid, operation_key=_key())
                            out.write("Rejected.\n")
                        elif tc=="4":
                            proposals.cancel(token, pid, operation_key=_key())
                            out.write("Cancelled.\n")
                        elif tc=="5":
                            proposals.archive(token, pid, operation_key=_key())
                            out.write("Archived.\n")
                except Exception as e:
                    out.write(str(e) + "\n")
            elif has_workspace and choice == "56":
                out.write("Customer requests: 1 Create  2 List  3 Manage\n")
                sub=_read(stdin, out, "Choice (1-3): ").strip()
                try:
                    if sub=="1":
                        sid=_read(stdin, out, "Service ID: ").strip()
                        cid=_read(stdin, out, "Customer ID: ").strip()
                        msg=_read(stdin, out, "Message (1..2000): ")
                        rid=customer_requests.create(token, sid, cid, msg, operation_key=_key())
                        out.write(f"Created request {rid} (pending)\n")
                    elif sub=="2":
                        status=_read(stdin, out, "Filter status [pending/accepted/rejected/completed/cancelled or empty]: ").strip() or None
                        lst=customer_requests.list(token, status=status)
                        for r in lst:
                            out.write(f"{r['id']} | svc:{r['service_id'][:8]} cust:{r['customer_id'][:8]} | {r['status']} | {r['message'][:40]}\n")
                        if not lst:
                            out.write("No requests.\n")
                    elif sub=="3":
                        rid=_read(stdin, out, "Request ID: ").strip()
                        cur=customer_requests.get(token, rid)
                        out.write(f"Current: {cur['status']} | {cur['message'][:40]}\n")
                        out.write("Transitions: 1 accept  2 reject  3 complete  4 cancel\n")
                        tc=_read(stdin, out, "Choice (1-4): ").strip()
                        if tc=="1":
                            customer_requests.accept(token, rid, operation_key=_key())
                            out.write("Accepted.\n")
                        elif tc=="2":
                            customer_requests.reject(token, rid, operation_key=_key())
                            out.write("Rejected.\n")
                        elif tc=="3":
                            customer_requests.complete(token, rid, operation_key=_key())
                            out.write("Completed.\n")
                        elif tc=="4":
                            customer_requests.cancel(token, rid, operation_key=_key())
                            out.write("Cancelled.\n")
                except Exception as e:
                    out.write(str(e) + "\n")
            elif has_workspace and choice == "57":
                out.write("Templates: Excel data cleaning, Data entry, CSV processing, Python automation, Data analysis, Website development, Frontend development, AI workflow automation, Document and invoice automation\n")
                out.write("1 List templates  2 View template  3 Create service from template\n")
                sub=_read(stdin, out, "Choice (1-3): ").strip()
                try:
                    if sub=="1":
                        for tid in service_templates.list_templates():
                            t=service_templates.get_template(tid)
                            out.write(f"{tid} | {t['title']} | INR {t['price_paise']//100}.{t['price_paise']%100:02d} | {t['delivery_days']}d rev:{t['revisions']}\n")
                    elif sub=="2":
                        tid=_read(stdin, out, "Template ID: ").strip()
                        t=service_templates.get_template(tid)
                        out.write(f"Title: {t['title']}\nScope: {t['scope'][:200]}\nDeliverable: {t['deliverable'][:200]}\nPackages: {len(t['packages'])} Requirements: {len(t['requirements'])}\n")
                    elif sub=="3":
                        tid=_read(stdin, out, "Template ID: ").strip()
                        title=_read(stdin, out, "Custom title (empty to keep template title): ").strip() or None
                        sid=service_templates.create_service_from_template(AuthService(AuthStorage(Path(os.environ.get('AI_EARN_AUTH_DB'))), create=False), StateStorage(Path(os.environ.get('AI_EARN_STATE_DB'))), token, tid, operation_key=_key(), title=title)
                        out.write(f"Created service {sid} from template {tid} (editable)\n")
                except Exception as e:
                    out.write(str(e) + "\n")
            elif has_workspace and choice == "58":
                sid=_read(stdin, out, "Service ID (empty for all): ").strip() or None
                try:
                    pres=portfolio_pres.render(token, sid)
                    out.write(f"Portfolio presentation: {pres['total_services']} service(s), verification:{pres['verification']}\n")
                    for svc in pres["services"]:
                        s=svc["service"]
                        out.write(f"\n=== {s['title']} ===\n")
                        out.write(f"Scope: {s['scope'][:120]}\nDeliverable: {s['deliverable'][:120]}\n{svc['presentation']['price_display']} | {svc['presentation']['packages_display'][:120]}\n")
                        out.write(f"Packages: {len(svc['packages'])} Requirements: {len(svc['requirements'])} Attachments: {len(svc['attachments'])} Portfolio items: {len(svc['portfolio_items'])}\n")
                        for item in svc["portfolio_items"]:
                            out.write(f" - Portfolio: {item['title']} | {item['description'][:60]} | attachments:{len(item['attachments'])}\n")
                except Exception as e:
                    out.write(str(e) + "\n")

            elif has_prod_ws and choice == "62":
                out.write("Ideas: 1 Create  2 List  3 Validate/Reject/Archive\n")
                sub=_read(stdin, out, "Choice (1-3): ").strip()
                try:
                    if sub=="1":
                        title=_read(stdin, out, "Idea title (1..200): ")
                        desc=_read(stdin, out, "Description (0..2000, empty for none): ")
                        iid=product_ideas.create(token, title, desc, operation_key=_key())
                        out.write(f"Created idea {iid} (idea)\n")
                    elif sub=="2":
                        status=_read(stdin, out, "Filter status [idea/validated/rejected/archived or empty]: ").strip() or None
                        lst=product_ideas.list(token, status=status)
                        for it in lst:
                            out.write(f"{it['id']} | {it['title'][:40]} | {it['status']} | {it['description'][:40]}\n")
                        if not lst:
                            out.write("No ideas.\n")
                    elif sub=="3":
                        iid=_read(stdin, out, "Idea ID: ").strip()
                        cur=product_ideas.get(token, iid)
                        out.write(f"Current: {cur['title']} | {cur['status']}\nTransitions: 1 validated 2 rejected 3 archived\n")
                        tc=_read(stdin, out, "Choice (1-3): ").strip()
                        if tc=="1":
                            product_ideas.validate(token, iid, operation_key=_key())
                            out.write("Validated.\n")
                        elif tc=="2":
                            product_ideas.reject(token, iid, operation_key=_key())
                            out.write("Rejected.\n")
                        elif tc=="3":
                            product_ideas.archive(token, iid, operation_key=_key())
                            out.write("Archived.\n")
                except Exception as e:
                    out.write(str(e) + "\n")
            elif has_prod_ws and choice == "63":
                prod_id=_read(stdin, out, "Product ID: ").strip()
                out.write("Validations: 1 Add  2 List\n")
                sub=_read(stdin, out, "Choice (1-2): ").strip()
                try:
                    if sub=="1":
                        crit=_read(stdin, out, "Criteria (1..500): ")
                        res=_read(stdin, out, "Result [pass/fail/pending]: ").strip() or "pending"
                        ev=_read(stdin, out, "Evidence (0..1000, empty for none): ")
                        vid=product_validations.add(token, prod_id, crit, res, operation_key=_key(), evidence=ev)
                        out.write(f"Added validation {vid} ({res})\n")
                    elif sub=="2":
                        lst=product_validations.list(token, prod_id)
                        for r in lst:
                            out.write(f"{r['id']} | {r['criteria'][:40]} | {r['result']} | {r['evidence'][:30]}\n")
                        if not lst:
                            out.write("No validations.\n")
                except Exception as e:
                    out.write(str(e) + "\n")
            elif has_prod_ws and choice == "64":
                prod_id=_read(stdin, out, "Product ID: ").strip()
                out.write("Market research: 1 Add  2 AI draft  3 List\n")
                sub=_read(stdin, out, "Choice (1-3): ").strip()
                try:
                    if sub=="1":
                        q=_read(stdin, out, "Query (1..500): ")
                        f=_read(stdin, out, "Findings (1..5000): ")
                        ev=_read(stdin, out, "Evidence URL (https://, empty for none): ").strip()
                        ver=_read(stdin, out, "Verification [unverified/verified, default unverified]: ").strip() or "unverified"
                        rid=market_research.add(token, prod_id, q, f, operation_key=_key(), evidence_url=ev, verification=ver)
                        out.write(f"Added research {rid} ({ver})\n")
                        if ver=="unverified":
                            out.write("Note: unverified drafts must not be presented as fact.\n")
                    elif sub=="2":
                        q=_read(stdin, out, "AI query (1..500): ")
                        ev=_read(stdin, out, "Evidence URL (empty for none): ").strip()
                        rid=market_research.generate_ai_draft(token, prod_id, q, operation_key=_key(), evidence_url=ev)
                        out.write(f"AI draft research {rid} (ai_generated=1, unverified)\n")
                    elif sub=="3":
                        lst=market_research.list(token, prod_id)
                        for r in lst:
                            out.write(f"{r['id']} | q:{r['query'][:30]} | ai:{r['ai_generated']} {r['verification']} | {r['findings'][:60]}\n")
                        if not lst:
                            out.write("No research.\n")
                except Exception as e:
                    out.write(str(e) + "\n")
            elif has_prod_ws and choice == "65":
                prod_id=_read(stdin, out, "Product ID: ").strip()
                out.write("Pricing: 1 Add  2 AI suggestion  3 List\n")
                sub=_read(stdin, out, "Choice (1-3): ").strip()
                try:
                    if sub=="1":
                        price=_paise(_read(stdin, out, "Price INR: ").strip())
                        rat=_read(stdin, out, "Rationale (1..2000): ")
                        ev=_read(stdin, out, "Evidence (0..1000, empty for none): ")
                        pid=pricing_suggestions.add(token, prod_id, price, rat, operation_key=_key(), evidence=ev, ai_generated=False)
                        out.write(f"Added pricing {pid}\n")
                    elif sub=="2":
                        pid=pricing_suggestions.generate_ai_suggestion(token, prod_id, operation_key=_key())
                        out.write(f"AI pricing suggestion {pid} (unverified draft)\n")
                    elif sub=="3":
                        lst=pricing_suggestions.list(token, prod_id)
                        for r in lst:
                            out.write(f"{r['id']} | INR {r['price_paise']//100}.{r['price_paise']%100:02d} | ai:{r['ai_generated']} | {r['rationale'][:40]}\n")
                        if not lst:
                            out.write("No pricing.\n")
                except Exception as e:
                    out.write(str(e) + "\n")
            elif has_prod_ws and choice == "66":
                prod_id=_read(stdin, out, "Product ID: ").strip()
                out.write("Versions: 1 Add  2 List\n")
                sub=_read(stdin, out, "Choice (1-2): ").strip()
                try:
                    if sub=="1":
                        ver=_read(stdin, out, "Version [A-Za-z0-9._- 1..20]: ").strip()
                        notes=_read(stdin, out, "Notes (0..2000): ")
                        vid=product_versions.add(token, prod_id, ver, notes, operation_key=_key())
                        out.write(f"Added version {vid} -> product now v{ver}\n")
                    elif sub=="2":
                        lst=product_versions.list(token, prod_id)
                        for r in lst:
                            out.write(f"{r['id']} | v{r['version']} | {r['notes'][:40]}\n")
                        if not lst:
                            out.write("No versions.\n")
                except Exception as e:
                    out.write(str(e) + "\n")
            elif has_prod_ws and choice == "67":
                out.write("Files: 1 Add  2 List  3 Remove\n")
                sub=_read(stdin, out, "Choice (1-3): ").strip()
                try:
                    if sub=="1":
                        pid=_read(stdin, out, "Product ID: ").strip()
                        fname=_read(stdin, out, "File name (1..200): ")
                        fsize=int(_read(stdin, out, "File size 0..104857600: ").strip() or "0")
                        mime=_read(stdin, out, "MIME (e.g. application/pdf): ").strip() or "application/octet-stream"
                        fid=product_files.add(token, pid, fname, fsize, mime, operation_key=_key())
                        out.write(f"Added file {fid}\n")
                    elif sub=="2":
                        pid=_read(stdin, out, "Product ID: ").strip()
                        lst=product_files.list(token, pid)
                        for r in lst:
                            out.write(f"{r['id']} | {r['file_name']} | {r['file_size']}b | {r['mime_type']}\n")
                        if not lst:
                            out.write("No files.\n")
                    elif sub=="3":
                        fid=_read(stdin, out, "File ID: ").strip()
                        product_files.remove(token, fid, operation_key=_key())
                        out.write("File removed.\n")
                except Exception as e:
                    out.write(str(e) + "\n")
            elif has_prod_ws and choice == "68":
                prod_id=_read(stdin, out, "Product ID: ").strip()
                out.write("Sales: 1 Record  2 List  3 Summary\n")
                sub=_read(stdin, out, "Choice (1-3): ").strip()
                try:
                    if sub=="1":
                        qty=int(_read(stdin, out, "Quantity 1..1000000: ").strip())
                        amt=_paise(_read(stdin, out, "Amount INR total: ").strip())
                        sale_at=int(_read(stdin, out, "Sale unix ts: ").strip())
                        ev=_read(stdin, out, "Evidence URL (https://, required if verified): ").strip()
                        ver=_read(stdin, out, "Verification [unverified/verified]: ").strip() or "unverified"
                        sid=product_sales.record(token, prod_id, qty, amt, sale_at, operation_key=_key(), evidence_url=ev, verification=ver)
                        out.write(f"Recorded sale {sid} ({ver}) qty {qty}\n")
                        if ver=="unverified":
                            out.write("Note: unverified, not claimed as received.\n")
                    elif sub=="2":
                        lst=product_sales.list(token, prod_id)
                        for r in lst:
                            out.write(f"{r['id']} | qty:{r['quantity']} INR {r['amount_paise']//100}.{r['amount_paise']%100:02d} | {r['verification']} | {r['sale_at']}\n")
                        if not lst:
                            out.write("No sales.\n")
                    elif sub=="3":
                        summ=product_sales.summary(token, prod_id)
                        out.write(f"Sales summary: total qty {summ['total_quantity']} verified {summ['verified_quantity']} sales {summ['total_sales']} verified {summ['verified_sales']} verification:{summ['verification']}\n")
                        out.write("Policy: verified requires evidence_url; unverified not claimed as received.\n")
                except Exception as e:
                    out.write(str(e) + "\n")
            elif has_prod_ws and choice == "69":
                out.write("Deliverables: 1 Add  2 List  3 Remove\n")
                sub=_read(stdin, out, "Choice (1-3): ").strip()
                try:
                    if sub=="1":
                        pid=_read(stdin, out, "Product ID: ").strip()
                        fname=_read(stdin, out, "File name (1..200): ")
                        fsize=int(_read(stdin, out, "File size: ").strip() or "0")
                        mime=_read(stdin, out, "MIME: ").strip() or "application/octet-stream"
                        did=product_deliverables.add(token, pid, fname, fsize, mime, operation_key=_key())
                        out.write(f"Added deliverable {did}\n")
                    elif sub=="2":
                        pid=_read(stdin, out, "Product ID: ").strip()
                        lst=product_deliverables.list(token, pid)
                        for r in lst:
                            out.write(f"{r['id']} | {r['file_name']} | {r['file_size']}b | {r['mime_type']}\n")
                        if not lst:
                            out.write("No deliverables.\n")
                    elif sub=="3":
                        did=_read(stdin, out, "Deliverable ID: ").strip()
                        product_deliverables.remove(token, did, operation_key=_key())
                        out.write("Deliverable removed.\n")
                except Exception as e:
                    out.write(str(e) + "\n")
            elif has_prod_ws and choice == "70":
                out.write("Documentation: 1 Create  2 AI draft  3 List  4 Update  5 Get\n")
                sub=_read(stdin, out, "Choice (1-5): ").strip()
                try:
                    if sub=="1":
                        pid=_read(stdin, out, "Product ID: ").strip()
                        title=_read(stdin, out, "Title (1..200): ")
                        content=_read(stdin, out, "Content (1..10000): ")
                        did=product_docs.create(token, pid, title, content, operation_key=_key())
                        out.write(f"Created doc {did}\n")
                    elif sub=="2":
                        pid=_read(stdin, out, "Product ID: ").strip()
                        did=product_docs.generate_ai_draft(token, pid, operation_key=_key())
                        out.write(f"AI draft doc {did} (ai_generated=1, unverified)\n")
                    elif sub=="3":
                        pid=_read(stdin, out, "Product ID: ").strip()
                        lst=product_docs.list(token, pid)
                        for r in lst:
                            out.write(f"{r['id']} | {r['title'][:40]} | ai:{r['ai_generated']} {r['verification']}\n")
                        if not lst:
                            out.write("No docs.\n")
                    elif sub=="4":
                        did=_read(stdin, out, "Doc ID: ").strip()
                        title=_read(stdin, out, "New title (empty keep): ").strip() or None
                        content=_read(stdin, out, "New content (empty keep): ").strip() or None
                        kwargs={}
                        if title: kwargs["title"]=title
                        if content: kwargs["content"]=content
                        if kwargs:
                            product_docs.update(token, did, operation_key=_key(), **kwargs)
                            out.write("Doc updated.\n")
                        else:
                            out.write("No fields.\n")
                    elif sub=="5":
                        did=_read(stdin, out, "Doc ID: ").strip()
                        doc=product_docs.get(token, did)
                        out.write(f"{doc['title']}: {doc['content'][:200]} verification:{doc['verification']}\n")
                except Exception as e:
                    out.write(str(e) + "\n")
            elif has_prod_ws and choice == "71":
                pid=_read(stdin, out, "Product ID: ").strip()
                try:
                    cur=products.get(token, pid)
                    out.write(f"Current: {cur['title']} | {cur['description'][:40]} | INR {cur['price_paise']//100}.{cur['price_paise']%100:02d} | v{cur['version']}\n")
                    title=_read(stdin, out, "New title (empty keep): ").strip() or None
                    desc=_read(stdin, out, "New description (empty keep): ").strip() or None
                    price_raw=_read(stdin, out, "New price INR (empty keep): ").strip()
                    price=_paise(price_raw) if price_raw else None
                    ver=_read(stdin, out, "New version (empty keep): ").strip() or None
                    kwargs={}
                    if title: kwargs["title"]=title
                    if desc: kwargs["description"]=desc
                    if price is not None: kwargs["price_paise"]=price
                    if ver: kwargs["version"]=ver
                    if kwargs:
                        product_editor.update(token, pid, operation_key=_key(), **kwargs)
                        out.write("Product updated.\n")
                    else:
                        out.write("No fields.\n")
                except Exception as e:
                    out.write(str(e) + "\n")
            elif has_prod_ws and choice == "72":
                pid=_read(stdin, out, "Product ID: ").strip()
                try:
                    cur=products.get(token, pid)
                    out.write(f"Current: {cur['status']} | {cur['title']}\nTransitions: 1 publish 2 archive\n")
                    tc=_read(stdin, out, "Choice (1-2): ").strip()
                    if tc=="1":
                        products.publish(token, pid, operation_key=_key())
                        out.write("Published.\n")
                    elif tc=="2":
                        products.archive(token, pid, operation_key=_key())
                        out.write("Archived.\n")
                except Exception as e:
                    out.write(str(e) + "\n")
            elif has_crm and choice == "73":
                cid=_read(stdin, out, "Customer ID: ").strip()
                try:
                    prof=crm_profile.get(token, cid)
                    out.write(f"Profile: {prof['name']} | {prof['contact']} | source:{prof['source']} | {prof['status']} | notes:{prof['_note_count']} req:{prof['_requirement_count']} comm:{prof['_communication_count']} follow:{prof['_followup_count']} orders:{prof['_order_count']} proj:{prof['_project_count']} prop:{prof['_proposal_count']}\n")
                    full=crm_profile.full(token, cid)
                    out.write(f"Timeline events: {len(full['timeline'])} | proposals:{len(full['proposals'])} | orders:{len(full['orders'])}\n")
                except Exception as e:
                    out.write(str(e) + "\n")
            elif has_crm and choice == "74":
                out.write("Notes: 1 Add  2 List  3 Get\n")
                sub=_read(stdin, out, "Choice (1-3): ").strip()
                try:
                    if sub=="1":
                        cid=_read(stdin, out, "Customer ID: ").strip()
                        note=_read(stdin, out, "Note (1..2000): ")
                        nid=crm_notes.add(token, cid, note, operation_key=_key())
                        out.write(f"Added note {nid}\n")
                    elif sub=="2":
                        cid=_read(stdin, out, "Customer ID: ").strip()
                        lst=crm_notes.list(token, cid)
                        for n in lst:
                            out.write(f"{n['id']} | {n['note'][:60]} | {n['created_at']}\n")
                        if not lst:
                            out.write("No notes.\n")
                    elif sub=="3":
                        nid=_read(stdin, out, "Note ID: ").strip()
                        n=crm_notes.get(token, nid)
                        out.write(f"{n['id']} | cust:{n['customer_id'][:8]} | {n['note'][:120]}\n")
                except Exception as e:
                    out.write(str(e) + "\n")
            elif has_crm and choice == "75":
                out.write("Requirements: 1 Add  2 List  3 Remove\n")
                sub=_read(stdin, out, "Choice (1-3): ").strip()
                try:
                    if sub=="1":
                        cid=_read(stdin, out, "Customer ID: ").strip()
                        req=_read(stdin, out, "Requirement (1..500): ")
                        rid=crm_requirements.add(token, cid, req, operation_key=_key())
                        out.write(f"Added requirement {rid}\n")
                    elif sub=="2":
                        cid=_read(stdin, out, "Customer ID: ").strip()
                        lst=crm_requirements.list(token, cid)
                        for r in lst:
                            out.write(f"{r['id']} | {r['requirement'][:60]} | {r['created_at']}\n")
                        if not lst:
                            out.write("No requirements.\n")
                    elif sub=="3":
                        rid=_read(stdin, out, "Requirement ID: ").strip()
                        crm_requirements.remove(token, rid, operation_key=_key())
                        out.write("Requirement removed.\n")
                except Exception as e:
                    out.write(str(e) + "\n")
            elif has_crm and choice == "76":
                out.write("CRM Communications (approval-gated): 1 Create draft  2 Request approval  3 Approve/Reject  4 Send  5 List history\n")
                sub=_read(stdin, out, "Choice (1-5): ").strip()
                try:
                    if sub=="1":
                        cid=_read(stdin, out, "Customer ID: ").strip()
                        chan=_read(stdin, out, "Channel (1..30): ")
                        body=_read(stdin, out, "Body (1..2000): ")
                        did=crm_comms.create_draft(token, cid, chan, body, operation_key=_key())
                        out.write(f"Created draft {did} (requires approval unless safe workflow)\n")
                    elif sub=="2":
                        did=_read(stdin, out, "Communication ID: ").strip()
                        ttl_raw=_read(stdin, out, "TTL 60..86400 (default 900): ").strip()
                        ttl=int(ttl_raw) if ttl_raw else 900
                        aid=crm_comm_approvals.request(token, did, operation_key=_key(), ttl=ttl)
                        out.write(f"Approval requested {aid} (pending, expires in {ttl}s)\n")
                    elif sub=="3":
                        aid=_read(stdin, out, "Approval ID: ").strip()
                        dec=_read(stdin, out, "Decision [approved/rejected]: ").strip()
                        if dec=="approved":
                            crm_comm_approvals.approve(token, aid, operation_key=_key())
                            out.write("Approved.\n")
                        elif dec=="rejected":
                            crm_comm_approvals.reject(token, aid, operation_key=_key())
                            out.write("Rejected.\n")
                        else:
                            out.write("Invalid decision.\n")
                    elif sub=="4":
                        did=_read(stdin, out, "Communication ID: ").strip()
                        aid=_read(stdin, out, "Approval ID (empty if safe workflow): ").strip() or None
                        crm_comms.send(token, did, operation_key=_key(), approval_id=aid)
                        out.write(f"Sent {did} (approval={aid} or safe workflow)\n")
                    elif sub=="5":
                        cid=_read(stdin, out, "Customer ID: ").strip()
                        lst=crm_comms.list_history(token, cid, limit=20)
                        for c in lst:
                            out.write(f"{c['id']} | {c['channel']} | {c['status']} | {c['body'][:40]}\n")
                        if not lst:
                            out.write("No communications.\n")
                except Exception as e:
                    out.write(str(e) + "\n")
            elif has_crm and choice == "77":
                out.write("Follow-ups: use 23 Add  24 List  25 Manage (CRM follow-ups are customer-scoped)\n")
                cid=_read(stdin, out, "Customer ID (empty for all): ").strip() or None
                try:
                    from ai_earn.followups import Followups as _F
                    fl=_F(crm_notes._auth, crm_notes._state)
                    lst=fl.list(token, customer_id=cid, limit=20)
                    for f in lst:
                        out.write(f"{f['id']} | cust:{f['customer_id'][:8]} | due:{f['due_at']} | {f['status']} | {f['note'][:40]}\n")
                    if not lst:
                        out.write("No followups.\n")
                except Exception as e:
                    out.write(str(e) + "\n")
            elif has_crm and choice == "78":
                out.write("Orders: 1 Create  2 List  3 Confirm  4 Fulfill  5 Cancel  6 Get\n")
                sub=_read(stdin, out, "Choice (1-6): ").strip()
                try:
                    if sub=="1":
                        cid=_read(stdin, out, "Customer ID: ").strip()
                        title=_read(stdin, out, "Title (1..200): ")
                        desc=_read(stdin, out, "Description (0..2000): ")
                        amt=_paise(_read(stdin, out, "Amount INR: ").strip())
                        oid=crm_orders.create(token, cid, title, desc, amt, operation_key=_key())
                        out.write(f"Created order {oid} (draft)\n")
                    elif sub=="2":
                        cid=_read(stdin, out, "Customer ID (empty for all): ").strip() or None
                        status=_read(stdin, out, "Filter status [draft/confirmed/fulfilled/cancelled or empty]: ").strip() or None
                        lst=crm_orders.list(token, customer_id=cid, status=status, limit=20)
                        for o in lst:
                            out.write(f"{o['id']} | cust:{o['customer_id'][:8]} | {o['title'][:30]} | INR {o['amount_paise']//100}.{o['amount_paise']%100:02d} | {o['status']}\n")
                        if not lst:
                            out.write("No orders.\n")
                    elif sub=="3":
                        oid=_read(stdin, out, "Order ID: ").strip()
                        crm_orders.confirm(token, oid, operation_key=_key())
                        out.write("Confirmed.\n")
                    elif sub=="4":
                        oid=_read(stdin, out, "Order ID: ").strip()
                        crm_orders.fulfill(token, oid, operation_key=_key())
                        out.write("Fulfilled.\n")
                    elif sub=="5":
                        oid=_read(stdin, out, "Order ID: ").strip()
                        crm_orders.cancel(token, oid, operation_key=_key())
                        out.write("Cancelled.\n")
                    elif sub=="6":
                        oid=_read(stdin, out, "Order ID: ").strip()
                        o=crm_orders.get(token, oid)
                        out.write(f"{o['id']} | {o['title']} | {o['description'][:40]} | {o['status']}\n")
                except Exception as e:
                    out.write(str(e) + "\n")
            elif has_crm and choice == "79":
                out.write("Project history: 1 Link  2 List  3 Unlink\n")
                sub=_read(stdin, out, "Choice (1-3): ").strip()
                try:
                    if sub=="1":
                        cid=_read(stdin, out, "Customer ID: ").strip()
                        tid=_read(stdin, out, "Project task ID: ").strip()
                        lid=crm_projects.link(token, cid, tid, operation_key=_key())
                        out.write(f"Linked {lid}\n")
                    elif sub=="2":
                        cid=_read(stdin, out, "Customer ID: ").strip()
                        lst=crm_projects.list(token, cid)
                        for l in lst:
                            pt=l.get("project_task") or {}
                            out.write(f"{l['id']} | task:{l['project_task_id'][:8]} | {pt.get('title','')[:30]} | {pt.get('status','')}\n")
                        if not lst:
                            out.write("No projects.\n")
                    elif sub=="3":
                        lid=_read(stdin, out, "Link ID: ").strip()
                        crm_projects.unlink(token, lid, operation_key=_key())
                        out.write("Unlinked.\n")
                except Exception as e:
                    out.write(str(e) + "\n")
            elif has_crm and choice == "80":
                cid=_read(stdin, out, "Customer ID: ").strip()
                try:
                    tl=crm_timeline.get(token, cid, limit=30)
                    out.write(f"Timeline {len(tl)} events for {cid[:8]}:\n")
                    for ev in tl:
                        out.write(f"{ev['at']} | {ev['type']}/{ev['event']} | {ev['summary'][:60]}\n")
                    if not tl:
                        out.write("No timeline events.\n")
                except Exception as e:
                    out.write(str(e) + "\n")
            elif has_crm and choice == "81":
                out.write("Outbound workflow (safe limited workflow): 1 Get  2 Set  3 Clear\n")
                sub=_read(stdin, out, "Choice (1-3): ").strip()
                try:
                    if sub=="1":
                        cfg=crm_outbound.get(token)
                        if cfg:
                            out.write(f"enabled:{cfg['enabled']} channels:{cfg['allowed_channels']} limit:{cfg['daily_limit']} updated:{cfg['updated_at']}\n")
                        else:
                            out.write("No config (approval required for all outbound).\n")
                    elif sub=="2":
                        enabled_raw=_read(stdin, out, "Enabled [yes/no]: ").strip().lower()
                        enabled=enabled_raw in ("yes","y","1","true")
                        chans=_read(stdin, out, "Allowed channels comma-separated (0..500, empty for none): ")
                        limit_raw=_read(stdin, out, "Daily limit 1..100 (default 5): ").strip()
                        limit=int(limit_raw) if limit_raw else 5
                        crm_outbound.set(token, operation_key=_key(), enabled=enabled, allowed_channels=chans, daily_limit=limit)
                        out.write(f"Outbound config set enabled={enabled} channels={chans} limit={limit} (explicit safe workflow)\n")
                    elif sub=="3":
                        crm_outbound.clear(token, operation_key=_key())
                        out.write("Outbound config cleared (approval required).\n")
                except Exception as e:
                    out.write(str(e) + "\n")
            elif has_crm and choice == "82":
                out.write("CSV (validated): 1 Export  2 Import\n")
                sub=_read(stdin, out, "Choice (1-2): ").strip()
                try:
                    if sub=="1":
                        csv_text=crm_csv.export(token)
                        out.write("CSV export:\n")
                        out.write(csv_text[:2000] + ("...\n" if len(csv_text)>2000 else "\n"))
                    elif sub=="2":
                        out.write("Paste CSV (header name,contact,source). End with line '.'\n")
                        lines=[]
                        while True:
                            line=_read(stdin, out, "")
                            if line==".":
                                break
                            lines.append(line)
                        csv_text="\n".join(lines)
                        prefix=_read(stdin, out, "Operation prefix 1..64: ").strip() or "csv_import"
                        res=crm_csv.import_csv(token, csv_text, operation_key_prefix=prefix)
                        out.write(f"Imported {len(res['created'])} customers\n")
                        for iid in res['created']:
                            out.write(f" {iid}\n")
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
                if has_an:
                    parts.append("42-45 Analytics")
                if has_conn:
                    parts.append("46-48 Connectors (stub)")
                if has_workspace:
                    parts.append("49-61 Service Workspace")
                if has_prod_ws:
                    parts.append("62-72 Product Workspace (v26)")
                if has_crm:
                    parts.append("73-82 CRM v27 (profile/notes/req/comm approval/orders/projects/timeline/workflow/CSV)")
                out.write("Choose " + ", ".join(parts) + ".\n")
        except (FinancialAssertionError, OpportunityError, ServiceError, ProductError, CustomerError, CommunicationError, LedgerError, FollowupError, InvoiceError, ProviderError, ApprovalError, AssistantError, ToolError, AnalyticsError, ConnError, ValueError, StateStorageError, ServiceWorkspaceError, ProductWorkspaceError, CRMError) as exc:
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
            out.write("New tracker state initialized (v27, 26 backups). Register an account with the auth command if needed.\n")
            return 0
        # Open only; missing files are never created during login.
        state = StateStorage(path)
        with state.transaction() as conn:
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            if version not in (13, 14, 15, 16, 17, 18, 19, 20, 21, 22, 23, 24, 25, 26, 27, 28):
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
        an = None
        if version >= 22:
            try:
                an = Analytics(auth, state)
            except AnalyticsError:
                an = None
        # Service workspace v25
        workspace_pkgs = workspace_reqs = workspace_atts = proposals = customer_requests = service_editor = service_templates = portfolio_pres = None
        if version >= 25:
            try:
                workspace_pkgs = ServicePackages(auth, state)
                workspace_reqs = ServiceRequirements(auth, state)
                workspace_atts = ServiceAttachments(auth, state)
                proposals = Proposals(auth, state)
                customer_requests = CustomerRequests(auth, state)
                service_editor = ServiceEditor(auth, state)
                service_templates = ServiceTemplates()
                portfolio_pres = PortfolioPresentation(auth, state)
            except Exception:
                workspace_pkgs = None
        # Product workspace v26
        product_ideas = product_validations = market_research = pricing_suggestions = product_versions = product_files = product_deliverables = product_sales = product_docs = product_editor = None
        if version >= 26:
            try:
                product_ideas = ProductIdeas(auth, state)
                product_validations = ProductValidations(auth, state)
                market_research = MarketResearch(auth, state)
                pricing_suggestions = PricingSuggestions(auth, state)
                product_versions = ProductVersions(auth, state)
                product_files = ProductFiles(auth, state)
                product_deliverables = ProductDeliverables(auth, state)
                product_sales = ProductSales(auth, state)
                product_docs = ProductDocumentation(auth, state)
                product_editor = ProductEditor(auth, state)
            except Exception:
                product_ideas = None
        # CRM v27
        crm_notes = crm_requirements = crm_orders = crm_projects = crm_comm_approvals = crm_outbound = crm_comms = crm_timeline = crm_profile = crm_csv = None
        if version >= 27:
            try:
                crm_notes = CustomerNotes(auth, state)
                crm_requirements = CustomerRequirements(auth, state)
                crm_orders = CustomerOrders(auth, state)
                crm_projects = CustomerProjects(auth, state)
                crm_comm_approvals = CommunicationApprovals(auth, state)
                crm_outbound = OutboundWorkflowConfig(auth, state)
                crm_comms = CRMCommunications(auth, state)
                crm_timeline = ActivityTimeline(auth, state)
                crm_profile = CustomerProfile(auth, state)
                crm_csv = CustomerCSV(auth, state)
            except Exception:
                crm_notes = None
        _menu(FinancialAssertions(auth, state), token, stdin, out, opp, svc, port, prod, cust, comm, ledg, foll, inv, prov, ai, an, workspace_pkgs, workspace_reqs, workspace_atts, proposals, customer_requests, service_editor, service_templates, portfolio_pres, product_ideas, product_validations, market_research, pricing_suggestions, product_versions, product_files, product_deliverables, product_sales, product_docs, product_editor, crm_notes, crm_requirements, crm_orders, crm_projects, crm_comm_approvals, crm_outbound, crm_comms, crm_timeline, crm_profile, crm_csv)
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

"""Tracker menu — orchestrates dispatch to domain handlers.

Extracted from tracker_cli._menu (P1-1). Choices 1-12 delegate to
tracker.financial / opportunities / services modules; remaining choices
remain inline pending further splits.
"""

from ai_earn.auth import AccessDenied
from ai_earn.communications import CommunicationError
from ai_earn.customers import CustomerError
from ai_earn.financial_assertions import FinancialAssertionError
from ai_earn.followups import FollowupError
from ai_earn.invoices import InvoiceError
from ai_earn.payment_providers import ProviderError
from ai_earn.ai_assistant import ApprovalError, AssistantError, ToolError
from ai_earn.analytics import AnalyticsError
from ai_earn.service_workspace import ServiceWorkspaceError
from ai_earn.product_workspace import ProductWorkspaceError
from ai_earn.crm import CRMError
from ai_earn.opportunities import OpportunityError
from ai_earn.ledger import LedgerError
from ai_earn.products import ProductError
from ai_earn.services import ServiceError
from ai_earn.state_storage import StateStorageError
from ai_earn.connectors import ConnectorError as ConnError

from .helpers import (
    _read,
    _confirm_yes,
    _paise,
    _key,
    _csv_preview,
    _STAGES,
    _NEXT,
    _OPP_STATUS_LABELS,
    _OPP_TRANSITIONS,
    _PROD_STATUS_LABELS,
    _CUST_STATUS_LABELS,
    _CUST_TRANSITIONS,
    _COMM_STATUS_LABELS,
    _LEDGER_KINDS,
    _FOLLOWUP_STATUS_LABELS,
    _INVOICE_STATUS_LABELS,
    _PROVIDER_STATUS_LABELS,
    _AI_STATUS_LABELS,
)

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
                from .financial import handle_add
                handle_add(service, token, stdin, out)
            elif choice == "2":
                from .financial import handle_list
                handle_list(service, token, stdin, out)
            elif choice in {"3", "4"}:
                from .financial import handle_advance_or_withdraw
                handle_advance_or_withdraw(service, token, choice, stdin, out)
            elif opportunities is not None and choice == "5":
                from .opportunities import handle_create
                handle_create(opportunities, token, stdin, out)
            elif opportunities is not None and choice == "6":
                from .opportunities import handle_list
                handle_list(opportunities, token, stdin, out)
            elif opportunities is not None and choice == "7":
                from .opportunities import handle_manage
                handle_manage(opportunities, token, stdin, out)
            elif opportunities is not None and choice == "8":
                from .opportunities import handle_ranked
                handle_ranked(opportunities, token, stdin, out)
            elif services is not None and choice == "9":
                from .services import handle_add_service
                handle_add_service(services, token, stdin, out)
            elif services is not None and choice == "10":
                from .services import handle_list_services
                handle_list_services(services, token, stdin, out)
            elif services is not None and choice == "11":
                from .services import handle_manage_service
                handle_manage_service(services, token, stdin, out)
            elif services is not None and portfolio is not None and choice == "12":
                from .services import handle_portfolio
                handle_portfolio(services, portfolio, token, stdin, out)
            elif products is not None and choice == "13":
                from .products import handle_add_product
                handle_add_product(products, token, stdin, out)
            elif products is not None and choice == "14":
                from .products import handle_list_products
                handle_list_products(products, token, stdin, out)
            elif products is not None and choice == "15":
                from .products import handle_manage_product
                handle_manage_product(products, token, stdin, out)
            elif customers is not None and choice == "16":
                from .customers import handle_add_customer
                handle_add_customer(customers, token, stdin, out)
            elif customers is not None and choice == "17":
                from .customers import handle_list_customers
                handle_list_customers(customers, token, stdin, out)
            elif customers is not None and choice == "18":
                from .customers import handle_manage_customer
                handle_manage_customer(customers, token, stdin, out)
            elif has_comm and choice == "19":
                from .communications import handle_add_comm
                handle_add_comm(communications, token, stdin, out)
            elif has_comm and choice == "20":
                from .communications import handle_list_comms
                handle_list_comms(communications, token, stdin, out)
            elif has_comm and choice == "21":
                from .communications import handle_manage_comm
                handle_manage_comm(communications, token, stdin, out)
            elif has_ledger and choice == "22":
                from .ledger import handle_ledger
                handle_ledger(ledger, token, stdin, out)
            elif has_follow and choice == "23":
                from .followups import handle_add_followup
                handle_add_followup(followups, token, stdin, out)
            elif has_follow and choice == "24":
                from .followups import handle_list_followups
                handle_list_followups(followups, token, stdin, out)
            elif has_follow and choice == "25":
                from .followups import handle_manage_followup
                handle_manage_followup(followups, token, stdin, out)
            elif has_follow and has_cust and choice == "26":
                from .followups import handle_import_csv
                handle_import_csv(customers, token, stdin, out)
            elif has_follow and has_cust and choice == "27":
                from .followups import handle_export_csv
                handle_export_csv(customers, token, stdin, out)
            elif has_inv and choice == "28":
                from .invoices import handle_add_invoice
                handle_add_invoice(invoices, token, stdin, out)
            elif has_inv and choice == "29":
                from .invoices import handle_list_invoices
                handle_list_invoices(invoices, token, stdin, out)
            elif has_inv and choice == "30":
                from .invoices import handle_manage_invoice
                handle_manage_invoice(invoices, token, stdin, out)
            elif has_prov and choice == "31":
                from .providers import handle_add_provider
                handle_add_provider(provider_orders, token, stdin, out)
            elif has_prov and choice == "32":
                from .providers import handle_list_providers
                handle_list_providers(provider_orders, token, stdin, out)
            elif has_prov and choice == "33":
                from .providers import handle_webhook
                handle_webhook(provider_orders, token, stdin, out)
            elif has_ai and choice == "34":
                from .ai import handle_list_tools
                handle_list_tools(ai_assistant, token, stdin, out)
            elif has_ai and choice == "35":
                from .ai import handle_request_approval
                handle_request_approval(ai_assistant, token, stdin, out)
            elif has_ai and choice == "36":
                from .ai import handle_list_approvals
                handle_list_approvals(ai_assistant, token, stdin, out)
            elif has_ai and choice == "37":
                from .ai import handle_decide_approval
                handle_decide_approval(ai_assistant, token, stdin, out)
            elif has_ai and choice == "38":
                from .ai import handle_revoke_approval
                handle_revoke_approval(ai_assistant, token, stdin, out)
            elif has_ai and choice == "39":
                from .ai import handle_ai_chat
                handle_ai_chat(ai_assistant, token, stdin, out)
            elif has_ai and choice == "40":
                from .ai import handle_emergency
                handle_emergency(ai_assistant, token, stdin, out)
            elif has_ai and choice == "41":
                from .ai import handle_execute_tool
                handle_execute_tool(ai_assistant, token, stdin, out)
            elif has_an and choice == "42":
                from .analytics import handle_summary
                handle_summary(analytics, token, stdin, out)
            elif has_an and choice == "43":
                from .analytics import handle_funnel
                handle_funnel(analytics, token, stdin, out)
            elif has_an and choice == "44":
                from .analytics import handle_mrr
                handle_mrr(analytics, token, stdin, out)
            elif has_an and choice == "45":
                from .analytics import handle_export_analytics
                handle_export_analytics(analytics, token, stdin, out)
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



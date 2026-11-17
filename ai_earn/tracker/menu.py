"""Tracker menu — orchestrates dispatch to domain handlers.

Extracted from tracker_cli._menu (P1-1). This module coordinates menu state
and delegates every choice to a focused tracker handler module.
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

from .helpers import _read

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
                from .connectors import handle_connectors
                handle_connectors(choice, token, stdin, out, opportunities=opportunities)
            elif has_conn and choice == "47":
                from .connectors import handle_connectors
                handle_connectors(choice, token, stdin, out, opportunities=opportunities)
            elif has_conn and choice == "48":
                from .connectors import handle_connectors
                handle_connectors(choice, token, stdin, out, opportunities=opportunities)
            elif has_workspace and choice == "49":
                from .workspace import handle_workspace
                handle_workspace(choice, token, stdin, out, services=services, workspace_pkgs=workspace_pkgs, workspace_reqs=workspace_reqs, workspace_atts=workspace_atts, proposals=proposals, customer_requests=customer_requests, service_editor=service_editor, service_templates=service_templates, portfolio_pres=portfolio_pres)
            elif has_workspace and choice == "50":
                from .workspace import handle_workspace
                handle_workspace(choice, token, stdin, out, services=services, workspace_pkgs=workspace_pkgs, workspace_reqs=workspace_reqs, workspace_atts=workspace_atts, proposals=proposals, customer_requests=customer_requests, service_editor=service_editor, service_templates=service_templates, portfolio_pres=portfolio_pres)
            elif has_workspace and choice == "51":
                from .workspace import handle_workspace
                handle_workspace(choice, token, stdin, out, services=services, workspace_pkgs=workspace_pkgs, workspace_reqs=workspace_reqs, workspace_atts=workspace_atts, proposals=proposals, customer_requests=customer_requests, service_editor=service_editor, service_templates=service_templates, portfolio_pres=portfolio_pres)
            elif has_workspace and choice == "52":
                from .workspace import handle_workspace
                handle_workspace(choice, token, stdin, out, services=services, workspace_pkgs=workspace_pkgs, workspace_reqs=workspace_reqs, workspace_atts=workspace_atts, proposals=proposals, customer_requests=customer_requests, service_editor=service_editor, service_templates=service_templates, portfolio_pres=portfolio_pres)
            elif has_workspace and choice == "53":
                from .workspace import handle_workspace
                handle_workspace(choice, token, stdin, out, services=services, workspace_pkgs=workspace_pkgs, workspace_reqs=workspace_reqs, workspace_atts=workspace_atts, proposals=proposals, customer_requests=customer_requests, service_editor=service_editor, service_templates=service_templates, portfolio_pres=portfolio_pres)
            elif has_workspace and choice == "54":
                from .workspace import handle_workspace
                handle_workspace(choice, token, stdin, out, services=services, workspace_pkgs=workspace_pkgs, workspace_reqs=workspace_reqs, workspace_atts=workspace_atts, proposals=proposals, customer_requests=customer_requests, service_editor=service_editor, service_templates=service_templates, portfolio_pres=portfolio_pres)
            elif has_workspace and choice == "55":
                from .workspace import handle_workspace
                handle_workspace(choice, token, stdin, out, services=services, workspace_pkgs=workspace_pkgs, workspace_reqs=workspace_reqs, workspace_atts=workspace_atts, proposals=proposals, customer_requests=customer_requests, service_editor=service_editor, service_templates=service_templates, portfolio_pres=portfolio_pres)
            elif has_workspace and choice == "56":
                from .workspace import handle_workspace
                handle_workspace(choice, token, stdin, out, services=services, workspace_pkgs=workspace_pkgs, workspace_reqs=workspace_reqs, workspace_atts=workspace_atts, proposals=proposals, customer_requests=customer_requests, service_editor=service_editor, service_templates=service_templates, portfolio_pres=portfolio_pres)
            elif has_workspace and choice == "57":
                from .workspace import handle_workspace
                handle_workspace(choice, token, stdin, out, services=services, workspace_pkgs=workspace_pkgs, workspace_reqs=workspace_reqs, workspace_atts=workspace_atts, proposals=proposals, customer_requests=customer_requests, service_editor=service_editor, service_templates=service_templates, portfolio_pres=portfolio_pres)
            elif has_workspace and choice == "58":
                from .workspace import handle_workspace
                handle_workspace(choice, token, stdin, out, services=services, workspace_pkgs=workspace_pkgs, workspace_reqs=workspace_reqs, workspace_atts=workspace_atts, proposals=proposals, customer_requests=customer_requests, service_editor=service_editor, service_templates=service_templates, portfolio_pres=portfolio_pres)
            elif has_prod_ws and choice == "62":
                from .product_workspace import handle_product_workspace
                handle_product_workspace(choice, token, stdin, out, products=products, product_ideas=product_ideas, product_validations=product_validations, market_research=market_research, pricing_suggestions=pricing_suggestions, product_versions=product_versions, product_files=product_files, product_deliverables=product_deliverables, product_sales=product_sales, product_docs=product_docs, product_editor=product_editor)
            elif has_prod_ws and choice == "63":
                from .product_workspace import handle_product_workspace
                handle_product_workspace(choice, token, stdin, out, products=products, product_ideas=product_ideas, product_validations=product_validations, market_research=market_research, pricing_suggestions=pricing_suggestions, product_versions=product_versions, product_files=product_files, product_deliverables=product_deliverables, product_sales=product_sales, product_docs=product_docs, product_editor=product_editor)
            elif has_prod_ws and choice == "64":
                from .product_workspace import handle_product_workspace
                handle_product_workspace(choice, token, stdin, out, products=products, product_ideas=product_ideas, product_validations=product_validations, market_research=market_research, pricing_suggestions=pricing_suggestions, product_versions=product_versions, product_files=product_files, product_deliverables=product_deliverables, product_sales=product_sales, product_docs=product_docs, product_editor=product_editor)
            elif has_prod_ws and choice == "65":
                from .product_workspace import handle_product_workspace
                handle_product_workspace(choice, token, stdin, out, products=products, product_ideas=product_ideas, product_validations=product_validations, market_research=market_research, pricing_suggestions=pricing_suggestions, product_versions=product_versions, product_files=product_files, product_deliverables=product_deliverables, product_sales=product_sales, product_docs=product_docs, product_editor=product_editor)
            elif has_prod_ws and choice == "66":
                from .product_workspace import handle_product_workspace
                handle_product_workspace(choice, token, stdin, out, products=products, product_ideas=product_ideas, product_validations=product_validations, market_research=market_research, pricing_suggestions=pricing_suggestions, product_versions=product_versions, product_files=product_files, product_deliverables=product_deliverables, product_sales=product_sales, product_docs=product_docs, product_editor=product_editor)
            elif has_prod_ws and choice == "67":
                from .product_workspace import handle_product_workspace
                handle_product_workspace(choice, token, stdin, out, products=products, product_ideas=product_ideas, product_validations=product_validations, market_research=market_research, pricing_suggestions=pricing_suggestions, product_versions=product_versions, product_files=product_files, product_deliverables=product_deliverables, product_sales=product_sales, product_docs=product_docs, product_editor=product_editor)
            elif has_prod_ws and choice == "68":
                from .product_workspace import handle_product_workspace
                handle_product_workspace(choice, token, stdin, out, products=products, product_ideas=product_ideas, product_validations=product_validations, market_research=market_research, pricing_suggestions=pricing_suggestions, product_versions=product_versions, product_files=product_files, product_deliverables=product_deliverables, product_sales=product_sales, product_docs=product_docs, product_editor=product_editor)
            elif has_prod_ws and choice == "69":
                from .product_workspace import handle_product_workspace
                handle_product_workspace(choice, token, stdin, out, products=products, product_ideas=product_ideas, product_validations=product_validations, market_research=market_research, pricing_suggestions=pricing_suggestions, product_versions=product_versions, product_files=product_files, product_deliverables=product_deliverables, product_sales=product_sales, product_docs=product_docs, product_editor=product_editor)
            elif has_prod_ws and choice == "70":
                from .product_workspace import handle_product_workspace
                handle_product_workspace(choice, token, stdin, out, products=products, product_ideas=product_ideas, product_validations=product_validations, market_research=market_research, pricing_suggestions=pricing_suggestions, product_versions=product_versions, product_files=product_files, product_deliverables=product_deliverables, product_sales=product_sales, product_docs=product_docs, product_editor=product_editor)
            elif has_prod_ws and choice == "71":
                from .product_workspace import handle_product_workspace
                handle_product_workspace(choice, token, stdin, out, products=products, product_ideas=product_ideas, product_validations=product_validations, market_research=market_research, pricing_suggestions=pricing_suggestions, product_versions=product_versions, product_files=product_files, product_deliverables=product_deliverables, product_sales=product_sales, product_docs=product_docs, product_editor=product_editor)
            elif has_prod_ws and choice == "72":
                from .product_workspace import handle_product_workspace
                handle_product_workspace(choice, token, stdin, out, products=products, product_ideas=product_ideas, product_validations=product_validations, market_research=market_research, pricing_suggestions=pricing_suggestions, product_versions=product_versions, product_files=product_files, product_deliverables=product_deliverables, product_sales=product_sales, product_docs=product_docs, product_editor=product_editor)
            elif has_crm and choice == "73":
                from .crm import handle_crm
                handle_crm(choice, token, stdin, out, crm_notes=crm_notes, crm_requirements=crm_requirements, crm_orders=crm_orders, crm_projects=crm_projects, crm_comm_approvals=crm_comm_approvals, crm_outbound=crm_outbound, crm_comms=crm_comms, crm_timeline=crm_timeline, crm_profile=crm_profile, crm_csv=crm_csv)
            elif has_crm and choice == "74":
                from .crm import handle_crm
                handle_crm(choice, token, stdin, out, crm_notes=crm_notes, crm_requirements=crm_requirements, crm_orders=crm_orders, crm_projects=crm_projects, crm_comm_approvals=crm_comm_approvals, crm_outbound=crm_outbound, crm_comms=crm_comms, crm_timeline=crm_timeline, crm_profile=crm_profile, crm_csv=crm_csv)
            elif has_crm and choice == "75":
                from .crm import handle_crm
                handle_crm(choice, token, stdin, out, crm_notes=crm_notes, crm_requirements=crm_requirements, crm_orders=crm_orders, crm_projects=crm_projects, crm_comm_approvals=crm_comm_approvals, crm_outbound=crm_outbound, crm_comms=crm_comms, crm_timeline=crm_timeline, crm_profile=crm_profile, crm_csv=crm_csv)
            elif has_crm and choice == "76":
                from .crm import handle_crm
                handle_crm(choice, token, stdin, out, crm_notes=crm_notes, crm_requirements=crm_requirements, crm_orders=crm_orders, crm_projects=crm_projects, crm_comm_approvals=crm_comm_approvals, crm_outbound=crm_outbound, crm_comms=crm_comms, crm_timeline=crm_timeline, crm_profile=crm_profile, crm_csv=crm_csv)
            elif has_crm and choice == "77":
                from .crm import handle_crm
                handle_crm(choice, token, stdin, out, crm_notes=crm_notes, crm_requirements=crm_requirements, crm_orders=crm_orders, crm_projects=crm_projects, crm_comm_approvals=crm_comm_approvals, crm_outbound=crm_outbound, crm_comms=crm_comms, crm_timeline=crm_timeline, crm_profile=crm_profile, crm_csv=crm_csv)
            elif has_crm and choice == "78":
                from .crm import handle_crm
                handle_crm(choice, token, stdin, out, crm_notes=crm_notes, crm_requirements=crm_requirements, crm_orders=crm_orders, crm_projects=crm_projects, crm_comm_approvals=crm_comm_approvals, crm_outbound=crm_outbound, crm_comms=crm_comms, crm_timeline=crm_timeline, crm_profile=crm_profile, crm_csv=crm_csv)
            elif has_crm and choice == "79":
                from .crm import handle_crm
                handle_crm(choice, token, stdin, out, crm_notes=crm_notes, crm_requirements=crm_requirements, crm_orders=crm_orders, crm_projects=crm_projects, crm_comm_approvals=crm_comm_approvals, crm_outbound=crm_outbound, crm_comms=crm_comms, crm_timeline=crm_timeline, crm_profile=crm_profile, crm_csv=crm_csv)
            elif has_crm and choice == "80":
                from .crm import handle_crm
                handle_crm(choice, token, stdin, out, crm_notes=crm_notes, crm_requirements=crm_requirements, crm_orders=crm_orders, crm_projects=crm_projects, crm_comm_approvals=crm_comm_approvals, crm_outbound=crm_outbound, crm_comms=crm_comms, crm_timeline=crm_timeline, crm_profile=crm_profile, crm_csv=crm_csv)
            elif has_crm and choice == "81":
                from .crm import handle_crm
                handle_crm(choice, token, stdin, out, crm_notes=crm_notes, crm_requirements=crm_requirements, crm_orders=crm_orders, crm_projects=crm_projects, crm_comm_approvals=crm_comm_approvals, crm_outbound=crm_outbound, crm_comms=crm_comms, crm_timeline=crm_timeline, crm_profile=crm_profile, crm_csv=crm_csv)
            elif has_crm and choice == "82":
                from .crm import handle_crm
                handle_crm(choice, token, stdin, out, crm_notes=crm_notes, crm_requirements=crm_requirements, crm_orders=crm_orders, crm_projects=crm_projects, crm_comm_approvals=crm_comm_approvals, crm_outbound=crm_outbound, crm_comms=crm_comms, crm_timeline=crm_timeline, crm_profile=crm_profile, crm_csv=crm_csv)
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



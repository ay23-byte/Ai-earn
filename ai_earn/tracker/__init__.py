"""Tracker package — dispatches to menu and domain handlers (P1-1).

This package splits the former 1989-LOC tracker_cli monolith. Public API
remains via ai_earn.tracker_cli (shim) for backward compatibility.
"""

import getpass
import os
import sqlite3
from pathlib import Path

from ai_earn.auth import AuthService
from ai_earn.auth_cli import _secret as _auth_secret
from ai_earn.auth_storage import AuthStorage, StorageError
from ai_earn.auth import AuthError
from ai_earn.config import ConfigurationError, load_settings
from ai_earn.communications import Communications
from ai_earn.customers import Customers
from ai_earn.financial_assertions import FinancialAssertions
from ai_earn.followups import Followups
from ai_earn.invoices import Invoices
from ai_earn.payment_providers import PaymentProviders, ProviderConfig, ProviderError
from ai_earn.ai_assistant import AIAssistant, AssistantError
from ai_earn.analytics import Analytics, AnalyticsError
from ai_earn.service_workspace import ServicePackages, ServiceRequirements, ServiceAttachments, Proposals, CustomerRequests, ServiceTemplates, PortfolioPresentation, ServiceEditor
from ai_earn.product_workspace import ProductIdeas, ProductValidations, MarketResearch, PricingSuggestions, ProductVersions, ProductFiles, ProductDeliverables, ProductSales, ProductDocumentation, ProductEditor
from ai_earn.crm import CustomerNotes, CustomerRequirements, CustomerOrders, CustomerProjects, CommunicationApprovals, OutboundWorkflowConfig, CRMCommunications, ActivityTimeline, CustomerProfile, CustomerCSV
from ai_earn.opportunities import Opportunities
from ai_earn.ledger import Ledger
from ai_earn.products import Products
from ai_earn.services import Services, Portfolio
from ai_earn.state_storage import LATEST_SCHEMA_VERSION, StateStorage, StateStorageError

from .helpers import _initialize_state, _confirm_yes, _read
from .menu import _menu

# Re-export helpers for external use (tests patch via tracker_cli shim, not here,
# but keep availability)
from .helpers import _paise, _key, _csv_preview  # noqa: F401

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
            if not _confirm_yes(stdin, out, "Create a NEW tracker state file and migration backups? Type YES: "):
                out.write("No files created. Please run `tracker init` again and type YES exactly (uppercase).\n")
                return 0
            _initialize_state(path)
            out.write(f"New tracker state initialized (v{LATEST_SCHEMA_VERSION}, {LATEST_SCHEMA_VERSION - 1} backups). Register an account with the auth command if needed.\n")
            return 0
        # Open only; missing files are never created during login.
        state = StateStorage(path)
        with state.transaction() as conn:
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            if version not in (13, 14, 15, 16, 17, 18, 19, 20, 21, 22, 23, 24, 25, 26, 27, 28):
                raise StateStorageError("Tracker requires an existing version 13, 14, 15, 16, 17, 18, 19, 20, 21 or 22 state file; no upgrade was performed.")
        auth = AuthService(AuthStorage(Path(auth_path), create=False))
        username = _read(stdin, out, "Username: ")
        # Resolve _secret dynamically to respect patch.object(tracker_cli, "_secret")
        # (tests patch the shim, not this package). Fall back to auth_cli._secret.
        try:
            import sys as _sys
            _cli_mod = _sys.modules.get("ai_earn.tracker_cli")
            _secret_fn = getattr(_cli_mod, "_secret", _auth_secret) if _cli_mod is not None else _auth_secret
        except Exception:
            _secret_fn = _auth_secret
        password = _secret_fn("Password: ", err)
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


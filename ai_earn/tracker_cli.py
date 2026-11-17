"""Experimental offline, manually asserted opportunity tracker.

No payment is verified, received or processed. Use only disposable data.
Now includes v15 opportunity discovery, v16 service catalog/portfolio,
v17 product workspace & customers, v18 communications & ledger, v19 followups & CSV, v20 invoices & 11-concept hardening, and v22 AI assistant (ToolRegistry/ApprovalQueue) and v22 analytics (funnel/MRR/conversion/workload) and connectors (Gmail/Drive/GitHub stubs) and v25 service workspace (packages/requirements/attachments/proposals/AI drafts/requests/templates/portfolio presentation) and v26 product workspace (ideas/validations/research/pricing/versions/files/sales/deliverables/docs) and v27 CRM (notes/requirements/orders/projects/timeline/approval/safe workflow).
"""

# P1-1 shim: tracker_cli monolith split into ai_earn.tracker package.
# This module preserves the public import path `ai_earn.tracker_cli`
# for backward compatibility (tests patch here). All logic now lives in
# ai_earn.tracker.{helpers,menu,financial,opportunities,services,__init__}.

from ai_earn.auth_cli import _secret  # re-export for patch.object(tracker_cli, "_secret")
from ai_earn.tracker.helpers import (
    _read,
    _confirm_yes,
    _paise,
    _key,
    _initialize_state,
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
from ai_earn.tracker.menu import _menu
from ai_earn.tracker import run

__all__ = [
    "_read",
    "_confirm_yes",
    "_paise",
    "_key",
    "_initialize_state",
    "_csv_preview",
    "_menu",
    "run",
    "_secret",
    "_STAGES",
    "_NEXT",
    "_OPP_STATUS_LABELS",
    "_OPP_TRANSITIONS",
    "_PROD_STATUS_LABELS",
    "_CUST_STATUS_LABELS",
    "_CUST_TRANSITIONS",
    "_COMM_STATUS_LABELS",
    "_LEDGER_KINDS",
    "_FOLLOWUP_STATUS_LABELS",
    "_INVOICE_STATUS_LABELS",
    "_PROVIDER_STATUS_LABELS",
    "_AI_STATUS_LABELS",
]

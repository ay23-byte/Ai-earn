"""First-run onboarding checklist computed from actual owner data.

Read-only: every item is derived from records the authenticated owner
already has (services, customers, opportunities, proposals, tasks,
invoices, ledger, AI provider config). Nothing is fabricated, no demo
records are created, and no other owner's data is visible. Missing
tables on older schemas report as not-done rather than failing.
"""

from __future__ import annotations

import sqlite3

from .auth import AuthService
from .state_authorization import _authorized_transaction
from .state_storage import StateStorage

# (key, label, table, extra owner-scoped predicate, guidance when incomplete)
_ITEMS: tuple[tuple[str, str, str, str, str], ...] = (
    ("service_created", "Create your first service", "services", "",
     "Tracker menu 9 (Add service): define what you sell, its scope and price."),
    ("service_published", "Publish a service", "services", " AND status='published'",
     "Tracker menu 11 (Manage service): publish when the draft reads well."),
    ("customer_added", "Add your first customer or lead", "customers", "",
     "Tracker menu 16 (Add customer): even one real lead starts the pipeline."),
    ("opportunity_tracked", "Track an opportunity", "opportunities", "",
     "Tracker menu 5 (Add opportunity): paste a real posting URL; never invent one."),
    ("proposal_created", "Draft a proposal", "proposals", "",
     "Tracker menu 53 (Create proposal): AI drafts stay unverified until you review."),
    ("work_planned", "Plan the work (task or project)", "task_plans", "",
     "Tracker: create a task plan so delivery is tracked, not remembered."),
    ("invoice_created", "Create an invoice draft", "invoices", "",
     "Tracker menu 28 (Add invoice): drafts are not issued or paid until you act."),
    ("income_recorded", "Record income or an expense", "ledger_entries", "",
     "Tracker menu 1 (Add financial) or 22 (Ledger): only record real movements."),
    ("ai_configured", "Configure an AI provider (optional)", "ai_provider_configs", "",
     "Optional: configure a provider with an environment-supplied key; drafts work without it."),
)


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()
    return row is not None


class OnboardingChecklist:
    """Owner-scoped, read-only first-run progress."""

    def __init__(self, auth: AuthService, state: StateStorage):
        self._auth = auth
        self._state = state

    def get(self, token: str) -> dict:
        with _authorized_transaction(self._auth, self._state, token) as (conn, owner, _now):
            items = []
            for key, label, table, predicate, guidance in _ITEMS:
                done = False
                if _table_exists(conn, table):
                    row = conn.execute(
                        f"SELECT 1 FROM {table} WHERE owner_id=?{predicate} LIMIT 1",  # noqa: S608 - table/predicate are internal constants
                        (owner,),
                    ).fetchone()
                    done = row is not None
                items.append({"key": key, "label": label, "done": done, "guidance": guidance})
            completed = sum(1 for item in items if item["done"])
            next_item = next((item for item in items if not item["done"]), None)
            return {
                "items": items,
                "completed": completed,
                "total": len(items),
                "next_action": next_item["guidance"] if next_item else "All first-run steps complete.",
                "read_only": True,
            }

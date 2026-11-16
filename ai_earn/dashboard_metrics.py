"""Authenticated, read-only dashboard metrics for the local application.

This service deliberately reports assertions, drafts and payment observations
separately. No local record in the current schema is evidence of received money.
It is an internal application service, not an HTTP or public API.
"""
from .state_authorization import _authorized_transaction
from .state_storage import StateStorage
from .auth import AuthService


def _count(conn, table, owner):
    # Table names are fixed constants at call sites, never user input.
    return conn.execute(
        f"SELECT COUNT(*) FROM {table} WHERE owner_id=?", (owner,)
    ).fetchone()[0]


def _amount_summary(conn, table, owner, amount_column, *, status_column=None):
    # Identifiers are internal constants; values are always SQL parameters.
    if status_column is None:
        row = conn.execute(
            f"SELECT COUNT(*), COALESCE(SUM({amount_column}), 0) FROM {table} WHERE owner_id=?",
            (owner,),
        ).fetchone()
        return {"count": row[0], "amount_paise": row[1]}
    rows = conn.execute(
        f"SELECT {status_column}, COUNT(*), COALESCE(SUM({amount_column}), 0) "
        f"FROM {table} WHERE owner_id=? GROUP BY {status_column}",
        (owner,),
    ).fetchall()
    return {
        row[0]: {"count": row[1], "amount_paise": row[2]}
        for row in rows
    }


class DashboardMetrics:
    """Build a consistent, account-scoped snapshot without changing state."""

    def __init__(self, auth: AuthService, state: StateStorage):
        self._auth = auth
        self._state = state

    def get(self, token: str) -> dict:
        with _authorized_transaction(self._auth, self._state, token) as (conn, owner, now):
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            result = {
                "generated_at": now,
                "schema_version": version,
                "read_only": True,
                "work": {
                    "opportunity_drafts": 0,
                    "task_plans": 0,
                    "queued_tasks": 0,
                    "task_workflows": 0,
                },
                "commercial": {
                    "order_drafts": {"count": 0, "amount_paise": 0},
                    "invoice_drafts": {"count": 0, "amount_paise": 0},
                },
                "asserted_pipeline": {
                    "count": 0,
                    "amount_paise": 0,
                    "stages": {},
                    "verification": "unverified",
                    "amount_kind": "asserted_not_revenue",
                },
                "payment_observations": {
                    "count": 0,
                    "amount_paise": 0,
                    "statuses": {},
                    "verification": "unverified",
                },
                "sandbox_payments": {
                    "count": 0,
                    "amount_paise": 0,
                    "stages": {},
                    "verification": "sandbox_only",
                },
                "income": {
                    "verified_amount_paise": 0,
                    "currency": "INR",
                    "status": "not_available_no_verified_payment_ledger",
                    "note": "Drafts, assertions, observations and sandbox events are not verified income.",
                },
            }

            if version >= 3:
                result["work"]["opportunity_drafts"] = _count(conn, "opportunity_drafts", owner)
            if version >= 4:
                result["work"]["task_plans"] = _count(conn, "task_plans", owner)
            if version >= 7:
                result["work"]["queued_tasks"] = conn.execute(
                    "SELECT COUNT(*) FROM task_queue WHERE owner_id=? AND status IN ('queued','leased')",
                    (owner,),
                ).fetchone()[0]
            if version >= 8:
                result["work"]["task_workflows"] = _count(conn, "task_workflows", owner)
            if version >= 5:
                result["commercial"]["order_drafts"] = _amount_summary(
                    conn, "order_drafts", owner, "proposed_amount_paise"
                )
            if version >= 6:
                result["commercial"]["invoice_drafts"] = _amount_summary(
                    conn, "invoice_drafts", owner, "proposed_amount_paise"
                )
            if version >= 9:
                observations = _amount_summary(
                    conn, "payment_observations", owner, "proposed_amount_paise",
                    status_column="status",
                )
                result["payment_observations"] = {
                    "count": sum(item["count"] for item in observations.values()),
                    "amount_paise": sum(item["amount_paise"] for item in observations.values()),
                    "statuses": observations,
                    "verification": "unverified",
                }
            if version >= 13:
                assertions = _amount_summary(
                    conn, "financial_assertions", owner, "amount_paise",
                    status_column="stage",
                )
                result["asserted_pipeline"] = {
                    "count": sum(item["count"] for item in assertions.values()),
                    "amount_paise": sum(item["amount_paise"] for item in assertions.values()),
                    "stages": assertions,
                    "verification": "unverified",
                    "amount_kind": "asserted_not_revenue",
                }
            if version >= 14:
                sandbox = _amount_summary(
                    conn, "sandbox_payments", owner, "amount_paise",
                    status_column="stage",
                )
                result["sandbox_payments"] = {
                    "count": sum(item["count"] for item in sandbox.values()),
                    "amount_paise": sum(item["amount_paise"] for item in sandbox.values()),
                    "stages": sandbox,
                    "verification": "sandbox_only",
                }
            if version >= 15:
                opp = _amount_summary(
                    conn, "opportunities", owner, "budget_paise",
                    status_column="status",
                )
                result["opportunities"] = {
                    "count": sum(item["count"] for item in opp.values()),
                    "amount_paise": sum(item["amount_paise"] for item in opp.values()),
                    "statuses": opp,
                    "verification": "unverified",
                    "note": "Manual/connector discovered; not verified or contacted.",
                }
            if version >= 16:
                svc = _amount_summary(
                    conn, "services", owner, "price_paise",
                    status_column="status",
                )
                result["services"] = {
                    "count": sum(item["count"] for item in svc.values()),
                    "amount_paise": sum(item["amount_paise"] for item in svc.values()),
                    "statuses": svc,
                    "verification": "unverified",
                    "note": "Local service catalog; draft/published are not verified offerings.",
                }
                result["portfolio"] = {
                    "count": _count(conn, "portfolio_items", owner),
                    "verification": "unverified",
                }
            if version >= 17:
                prod = _amount_summary(
                    conn, "products", owner, "price_paise",
                    status_column="status",
                )
                result["products"] = {
                    "count": sum(item["count"] for item in prod.values()),
                    "amount_paise": sum(item["amount_paise"] for item in prod.values()),
                    "statuses": prod,
                    "verification": "unverified",
                    "note": "Local product workspace; draft/published are not verified.",
                }
                cust_rows = conn.execute(
                    "SELECT status, COUNT(*) FROM customers WHERE owner_id=? GROUP BY status",
                    (owner,),
                ).fetchall()
                cust_statuses = {row[0]: {"count": row[1]} for row in cust_rows}
                result["customers"] = {
                    "count": sum(item["count"] for item in cust_statuses.values()),
                    "statuses": cust_statuses,
                    "verification": "unverified",
                    "note": "Local CRM leads; not verified customers.",
                }
            if version >= 18:
                comm_rows = conn.execute(
                    "SELECT status, COUNT(*) FROM communications WHERE owner_id=? GROUP BY status",
                    (owner,),
                ).fetchall()
                comm_statuses = {row[0]: {"count": row[1]} for row in comm_rows}
                result["communications"] = {
                    "count": sum(item["count"] for item in comm_statuses.values()),
                    "statuses": comm_statuses,
                    "verification": "unverified",
                    "note": "Local CRM communications; draft/sent are unverified.",
                }
                ledger = _amount_summary(
                    conn, "ledger_entries", owner, "amount_paise",
                    status_column="kind",
                )
                # ledger_entries uses kind, not status, but _amount_summary generic handles it
                total_ledger = sum(item["amount_paise"] for item in ledger.values())
                # net = income - expense - refund - fee
                net_paise = (ledger.get("income", {}).get("amount_paise", 0)
                             - ledger.get("expense", {}).get("amount_paise", 0)
                             - ledger.get("refund", {}).get("amount_paise", 0)
                             - ledger.get("fee", {}).get("amount_paise", 0))
                result["ledger"] = {
                    "count": sum(item["count"] for item in ledger.values()),
                    "amount_paise": total_ledger,
                    "net_paise": net_paise,
                    "kinds": ledger,
                    "verification": "unverified",
                    "note": "Local ledger; net = income - expense - refund - fee, unverified.",
                }
                result["income"]["verified_amount_paise"] = 0
                # keep income status as not_available, but ledger net is separate
            if version >= 19:
                follow_rows = conn.execute(
                    "SELECT status, COUNT(*) FROM followups WHERE owner_id=? GROUP BY status",
                    (owner,),
                ).fetchall()
                follow_statuses = {row[0]: {"count": row[1]} for row in follow_rows}
                result["followups"] = {
                    "count": sum(item["count"] for item in follow_statuses.values()),
                    "statuses": follow_statuses,
                    "verification": "unverified",
                    "note": "Local followups; pending/completed/cancelled unverified.",
                }
            if version >= 20:
                inv_rows = conn.execute(
                    "SELECT status, COUNT(*), COALESCE(SUM(amount_paise),0) FROM invoices WHERE owner_id=? GROUP BY status",
                    (owner,),
                ).fetchall()
                inv_statuses = {row[0]: {"count": row[1], "amount_paise": row[2]} for row in inv_rows}
                result["invoices"] = {
                    "count": sum(item["count"] for item in inv_statuses.values()),
                    "amount_paise": sum(item["amount_paise"] for item in inv_statuses.values()),
                    "statuses": inv_statuses,
                    "verification": "unverified",
                    "note": "Local invoices draft→issued→paid/overdue/void, PDF stub hash, not issued demand.",
                }
                # 11-concept hardening: aggregate across tables, clearly separating unverified vs not_implemented
                # Concept breakdown per PAYMENTS.md
                def _c(table, status_col=None, value=None, amount_col="amount_paise"):
                    if status_col is None:
                        r = conn.execute(f"SELECT COUNT(*), COALESCE(SUM({amount_col}),0) FROM {table} WHERE owner_id=?", (owner,)).fetchone()
                        return {"count": r[0], "amount_paise": r[1]}
                    # Use amount_col = amount_paise or proposed_amount_paise etc.
                    r = conn.execute(f"SELECT COUNT(*), COALESCE(SUM({amount_col}),0) FROM {table} WHERE owner_id=? AND {status_col}=?", (owner, value)).fetchone()
                    return {"count": r[0], "amount_paise": r[1]}
                proposed = conn.execute("SELECT COUNT(*), COALESCE(SUM(proposed_amount_paise),0) FROM order_drafts WHERE owner_id=? AND status='draft'", (owner,)).fetchone()
                quoted = _c("financial_assertions", "stage", "quote_asserted")
                contracted = _c("financial_assertions", "stage", "contract_asserted")
                invoiced_asserted = _c("financial_assertions", "stage", "invoice_asserted")
                initiated = _c("financial_assertions", "stage", "initiated_asserted")
                inv_draft = conn.execute("SELECT COUNT(*), COALESCE(SUM(proposed_amount_paise),0) FROM invoice_drafts WHERE owner_id=? AND status='draft'", (owner,)).fetchone()
                invoices_issued_row = conn.execute("SELECT COUNT(*), COALESCE(SUM(amount_paise),0) FROM invoices WHERE owner_id=? AND status IN ('issued','paid','overdue')", (owner,)).fetchone()
                settled = {"count": 0, "amount_paise": 0}
                if version >= 14:
                    r = conn.execute("SELECT COUNT(*), COALESCE(SUM(amount_paise),0) FROM sandbox_payments WHERE owner_id=? AND stage='sandbox_settled'", (owner,)).fetchone()
                    settled = {"count": r[0], "amount_paise": r[1]}
                ledger_rows2 = conn.execute("SELECT kind, COALESCE(SUM(amount_paise),0), COUNT(*) FROM ledger_entries WHERE owner_id=? GROUP BY kind", (owner,)).fetchall()
                ledger_map2 = {k: {"amount_paise": 0, "count": 0} for k in ("income","expense","refund","fee")}
                for kind, amt, cnt in ledger_rows2:
                    ledger_map2[kind] = {"amount_paise": amt, "count": cnt}
                refunds = ledger_map2["refund"]
                expenses = ledger_map2["expense"]
                fees = ledger_map2["fee"]
                ledger_net = conn.execute("SELECT COALESCE(SUM(CASE WHEN kind='income' THEN amount_paise WHEN kind IN ('expense','refund','fee') THEN -amount_paise ELSE 0 END),0) FROM ledger_entries WHERE owner_id=?", (owner,)).fetchone()[0]
                net_realized = settled["amount_paise"] - refunds["amount_paise"] - fees["amount_paise"] - expenses["amount_paise"]
                result["ledger_11"] = {
                    "proposed": {"count": proposed[0], "amount_paise": proposed[1], "verification": "unverified"},
                    "quoted": {**quoted, "verification": "unverified"},
                    "contracted": {**contracted, "verification": "unverified"},
                    "invoiced_asserted": {**invoiced_asserted, "verification": "unverified"},
                    "invoiced_drafts": {"count": inv_draft[0], "amount_paise": inv_draft[1], "verification": "unverified"},
                    "invoices_issued": {"count": invoices_issued_row[0], "amount_paise": invoices_issued_row[1], "verification": "unverified", "note": "PDF stub"},
                    "payment_initiated": {**initiated, "verification": "unverified"},
                    "payment_received_verified": {"count": 0, "amount_paise": 0, "verification": "not_implemented"},
                    "payment_settled_synthetic": {**settled, "verification": "synthetic"},
                    "refunds": {**refunds, "verification": "unverified"},
                    "expenses": {**expenses, "verification": "unverified"},
                    "fees": {**fees, "verification": "unverified"},
                    "ledger_net_unverified": {"amount_paise": ledger_net, "verification": "unverified"},
                    "net_realized_synthetic": {"amount_paise": net_realized, "verification": "synthetic", "note": "settled(synthetic)-refunds-fees-expenses, not bank-verified"},
                    "note": "11-concept separation: verified income is 0; all others unverified/synthetic.",
                }
            return result

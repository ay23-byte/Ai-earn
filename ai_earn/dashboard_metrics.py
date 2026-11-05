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
            return result

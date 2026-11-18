"""Authenticated, read-only dashboard metrics for the local application.

This service deliberately reports assertions, drafts and payment observations
separately. No local record in the current schema is evidence of received money.
It is an internal application service, not an HTTP or public API.
"""
from typing import cast
from .financial_math import net_cash_paise
from .state_authorization import _authorized_transaction
from .state_storage import StateStorage
from .auth import AuthService
try:
    from .revenue_analytics import dashboard_for_conn as _rev_dashboard_for_conn
except Exception:
    _rev_dashboard_for_conn = None  # type: ignore[assignment]


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
        # P0-5: fetch UI display_name before locks (read-only profile, never for isolation)
        display_name = None
        username = None
        _owner_via_profile = None
        try:
            _prof = self._auth.profile(token)
            _owner_via_profile = _prof.get("id")
            username = _prof.get("username")
            display_name = _prof.get("display_name")
        except Exception:
            pass
        with _authorized_transaction(self._auth, self._state, token) as (conn, owner, now):
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            result = {
                "generated_at": now,
                "schema_version": version,
                "read_only": True,
                "account": {"id": owner, "username": username, "display_name": display_name},
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
                net_realized = net_cash_paise(
                    income_paise=settled["amount_paise"],
                    refunds_paise=refunds["amount_paise"],
                    fees_paise=fees["amount_paise"],
                    expenses_paise=expenses["amount_paise"],
                )
                # v28 verified finance breakdown
                payment_initiated_verified = {"count": 0, "amount_paise": 0, "verification": "verified", "currency": "INR"}
                payment_received_verified_v28 = {"count": 0, "amount_paise": 0, "verification": "verified", "currency": "INR"}
                payment_settled_verified = {"count": 0, "amount_paise": 0, "verification": "verified", "currency": "INR"}
                refunds_verified = {"count": 0, "amount_paise": 0, "verification": "verified", "currency": "INR"}
                expenses_verified = {"count": 0, "amount_paise": 0, "verification": "verified", "currency": "INR"}
                fees_verified = {"count": 0, "amount_paise": 0, "verification": "verified", "currency": "INR"}
                net_realized_verified = {"amount_paise": 0, "verification": "verified", "currency": "INR"}
                if version >= 28:
                    row = conn.execute("SELECT COUNT(*), COALESCE(SUM(amount_paise),0) FROM payment_records WHERE owner_id=? AND status='initiated' AND verified=1", (owner,)).fetchone()
                    payment_initiated_verified = {"count": row[0], "amount_paise": row[1], "verification": "verified", "currency": "INR"}
                    row = conn.execute("SELECT COUNT(*), COALESCE(SUM(amount_paise),0) FROM payment_records WHERE owner_id=? AND status='received' AND verified=1", (owner,)).fetchone()
                    payment_received_verified_v28 = {"count": row[0], "amount_paise": row[1], "verification": "verified", "currency": "INR"}
                    row = conn.execute("SELECT COUNT(*), COALESCE(SUM(amount_paise),0) FROM payment_records WHERE owner_id=? AND status='settled' AND verified=1", (owner,)).fetchone()
                    payment_settled_verified = {"count": row[0], "amount_paise": row[1], "verification": "verified", "currency": "INR"}
                    row = conn.execute("SELECT COUNT(*), COALESCE(SUM(amount_paise),0) FROM refund_records WHERE owner_id=? AND verified=1", (owner,)).fetchone()
                    refunds_verified = {"count": row[0], "amount_paise": row[1], "verification": "verified", "currency": "INR"}
                    row = conn.execute("SELECT COUNT(*), COALESCE(SUM(amount_paise),0) FROM expense_records WHERE owner_id=? AND verified=1", (owner,)).fetchone()
                    expenses_verified = {"count": row[0], "amount_paise": row[1], "verification": "verified", "currency": "INR"}
                    row = conn.execute("SELECT COUNT(*), COALESCE(SUM(amount_paise),0) FROM fee_records WHERE owner_id=? AND verified=1", (owner,)).fetchone()
                    fees_verified = {"count": row[0], "amount_paise": row[1], "verification": "verified", "currency": "INR"}
                    net_realized_verified = {
                        "amount_paise": net_cash_paise(
                            income_paise=cast(int, payment_settled_verified["amount_paise"]),
                            refunds_paise=cast(int, refunds_verified["amount_paise"]),
                            fees_paise=cast(int, fees_verified["amount_paise"]),
                            expenses_paise=cast(int, expenses_verified["amount_paise"]),
                        ),
                        "verification": "verified",
                        "currency": "INR",
                        "note": "settled_verified - refunds_verified - expenses_verified - fees_verified",
                    }
                # payment_received_verified legacy: if v28, show verified else not_implemented
                _prv = payment_received_verified_v28 if version >= 28 else {"count": 0, "amount_paise": 0, "verification": "not_implemented"}
                result["ledger_11"] = {
                    "proposed": {"count": proposed[0], "amount_paise": proposed[1], "verification": "unverified"},
                    "quoted": {**quoted, "verification": "unverified"},
                    "contracted": {**contracted, "verification": "unverified"},
                    "invoiced_asserted": {**invoiced_asserted, "verification": "unverified"},
                    "invoiced_drafts": {"count": inv_draft[0], "amount_paise": inv_draft[1], "verification": "unverified"},
                    "invoices_issued": {"count": invoices_issued_row[0], "amount_paise": invoices_issued_row[1], "verification": "unverified", "note": "PDF stub"},
                    "payment_initiated": {**initiated, "verification": "unverified"},
                    "payment_received_verified": _prv,
                    "payment_settled_synthetic": {**settled, "verification": "synthetic"},
                    "refunds": {**refunds, "verification": "unverified"},
                    "expenses": {**expenses, "verification": "unverified"},
                    "fees": {**fees, "verification": "unverified"},
                    "ledger_net_unverified": {"amount_paise": ledger_net, "verification": "unverified"},
                    "net_realized_synthetic": {"amount_paise": net_realized, "verification": "synthetic", "note": "settled(synthetic)-refunds-fees-expenses, not bank-verified"},
                    "payment_initiated_verified": payment_initiated_verified,
                    "payment_received_verified_v28": payment_received_verified_v28,
                    "payment_settled_verified": payment_settled_verified,
                    "refunds_verified": refunds_verified,
                    "expenses_verified": expenses_verified,
                    "fees_verified": fees_verified,
                    "net_realized_verified": net_realized_verified,
                    "note": "11-concept separation: legacy unverified/synthetic + v28 verified strictly separate.",
                }
            if version >= 28:
                # Finance v28 verified ledger metrics
                tax_row = conn.execute("SELECT jurisdiction, rate_bps FROM tax_configs WHERE owner_id=?", (owner,)).fetchone()
                tax_cfg = {"jurisdiction": tax_row[0], "rate_bps": tax_row[1], "currency": "INR"} if tax_row else {"count": 0, "note": "No tax config; invocable via finance TaxConfigs."}
                billing_cnt = conn.execute("SELECT COUNT(*) FROM billing_profiles WHERE owner_id=?", (owner,)).fetchone()[0]
                pay_cnt = conn.execute("SELECT COUNT(*) FROM payment_records WHERE owner_id=?", (owner,)).fetchone()[0]
                pay_verified_cnt = conn.execute("SELECT COUNT(*) FROM payment_records WHERE owner_id=? AND verified=1", (owner,)).fetchone()[0]
                exp_cnt = conn.execute("SELECT COUNT(*) FROM expense_records WHERE owner_id=?", (owner,)).fetchone()[0]
                refund_cnt = conn.execute("SELECT COUNT(*) FROM refund_records WHERE owner_id=?", (owner,)).fetchone()[0]
                fee_cnt = conn.execute("SELECT COUNT(*) FROM fee_records WHERE owner_id=?", (owner,)).fetchone()[0]
                # outstanding invoices: issued/overdue
                outstanding_cnt = conn.execute("SELECT COUNT(*) FROM invoices WHERE owner_id=? AND status IN ('issued','overdue')", (owner,)).fetchone()[0]
                overdue_cnt = conn.execute("SELECT COUNT(*) FROM invoices WHERE owner_id=? AND status IN ('issued','overdue') AND due_at IS NOT NULL AND due_at < ?", (owner, now)).fetchone()[0]
                result["finance_v28"] = {
                    "tax_config": tax_cfg,
                    "billing_profiles": {"count": billing_cnt, "currency": "INR"},
                    "payment_records": {"count": pay_cnt, "verified": pay_verified_cnt, "currency": "INR"},
                    "expense_records": {"count": exp_cnt, "currency": "INR"},
                    "refund_records": {"count": refund_cnt, "currency": "INR"},
                    "fee_records": {"count": fee_cnt, "currency": "INR"},
                    "outstanding_invoices": {"count": outstanding_cnt, "overdue": overdue_cnt, "currency": "INR"},
                    "net_realized_verified": net_realized_verified,
                    "verification": "verified",
                    "note": "Finance v28: INR-only, verified ledger strictly separate, due dates and billing isolated.",
                }
            if version >= 21:
                prov_rows = conn.execute(
                    "SELECT provider, status, COUNT(*), COALESCE(SUM(amount_paise),0) FROM payment_provider_orders WHERE owner_id=? GROUP BY provider, status",
                    (owner,),
                ).fetchall()
                # aggregate by status and provider
                prov_statuses: dict = {}
                prov_providers: dict = {}
                total_cnt = 0
                total_amt = 0
                for prov, status, cnt, amt in prov_rows:
                    prov_statuses.setdefault(status, {"count": 0, "amount_paise": 0})
                    prov_statuses[status]["count"] += cnt
                    prov_statuses[status]["amount_paise"] += amt
                    prov_providers.setdefault(prov, {"count": 0, "amount_paise": 0})
                    prov_providers[prov]["count"] += cnt
                    prov_providers[prov]["amount_paise"] += amt
                    total_cnt += cnt
                    total_amt += amt
                result["provider_orders"] = {
                    "count": total_cnt,
                    "amount_paise": total_amt,
                    "statuses": prov_statuses,
                    "providers": prov_providers,
                    "verification": "sandbox_only",
                    "note": "Sandbox provider orders; HMAC verified, not live settlement.",
                }
                # webhook events
                evt_cnt = conn.execute("SELECT COUNT(*) FROM payment_provider_events WHERE owner_id=?", (owner,)).fetchone()[0]
                result["provider_events"] = {
                    "count": evt_cnt,
                    "verification": "sandbox_only",
                    "note": "HMAC verified webhook events, idempotent by event_id.",
                }
            if version >= 22:
                # AI approvals aggregated by status (pending/approved/rejected/expired virtual/revoked)
                approv_rows = conn.execute(
                    "SELECT status, COUNT(*) FROM ai_approvals WHERE owner_id=? GROUP BY status",
                    (owner,),
                ).fetchall()
                approv_statuses = {row[0]: {"count": row[1]} for row in approv_rows}
                # virtual expired count: pending rows past expires_at
                import time as _time
                int(_time.time())
                # use DB now already captured as `now` in transaction; treat pending expired logically
                try:
                    pending_expired = conn.execute(
                        "SELECT COUNT(*) FROM ai_approvals WHERE owner_id=? AND status='pending' AND expires_at < ?",
                        (owner, now),
                    ).fetchone()[0]
                except Exception:
                    pending_expired = 0
                total_approv = sum(v["count"] for v in approv_statuses.values())
                result["ai_approvals"] = {
                    "count": total_approv,
                    "statuses": approv_statuses,
                    "pending_expired_virtual": pending_expired,
                    "verification": "unverified",
                    "note": "ApprovalQueue: consequential tools require explicit owner approval; pending past TTL treated as expired (virtual).",
                }
                # emergency stop
                emerg = conn.execute("SELECT stopped, updated_at, reason FROM ai_emergency WHERE owner_id=?", (owner,)).fetchone()
                if emerg is None:
                    result["ai_emergency"] = {"stopped": False, "note": "No emergency; consequential actions allowed if approved."}
                else:
                    result["ai_emergency"] = {"stopped": bool(emerg["stopped"]), "updated_at": emerg["updated_at"], "reason": emerg["reason"], "verification": "sandbox_only"}
                # AI provider/tooling note
                result["ai_policy"] = {
                    "tool_scopes": {"read": "direct", "draft": "draft_unverified", "consequential": "approval_required"},
                    "provider": "stub sandbox only; live OpenAI/Ollama blocked",
                    "injection_isolation": "external content treated as data, never as instruction; consequential tools never auto-executed",
                    "verification": "draft_unverified or approved_consequential, not authority",
                }
                # Analytics read-only summary (no new tables, real ledger/invoice aggregates, time-bounded via created_at)
                try:
                    # Use same owner+now but avoid re-entering transaction; compute directly via current conn
                    # Funnel quick counts (all time) for dashboard glance
                    funnel_total_opp = conn.execute("SELECT COUNT(*) FROM opportunities WHERE owner_id=?", (owner,)).fetchone()[0] if version >=15 else 0
                    funnel_total_cust = conn.execute("SELECT COUNT(*) FROM customers WHERE owner_id=?", (owner,)).fetchone()[0] if version >=17 else 0
                    funnel_total_inv = conn.execute("SELECT COUNT(*) FROM invoices WHERE owner_id=?", (owner,)).fetchone()[0] if version >=20 else 0
                    # MRR total income all time (unverified)
                    mrr_total = conn.execute("SELECT COALESCE(SUM(amount_paise),0) FROM ledger_entries WHERE owner_id=? AND kind='income'", (owner,)).fetchone()[0] if version >=18 else 0
                    # Monthly buckets for last display (recompute via same logic as Analytics.mrr but simplified)
                    mrr_buckets: dict = {}
                    if version >=18:
                        import datetime as _dt
                        rows = conn.execute("SELECT created_at, amount_paise FROM ledger_entries WHERE owner_id=? AND kind='income' ORDER BY created_at", (owner,)).fetchall()
                        for r in rows:
                            dt = _dt.datetime.utcfromtimestamp(r["created_at"])
                            key = f"{dt.year:04d}-{dt.month:02d}"
                            mrr_buckets[key] = mrr_buckets.get(key, 0) + r["amount_paise"]
                    result["analytics"] = {
                        "funnel": {"opportunities": funnel_total_opp, "customers": funnel_total_cust, "invoices": funnel_total_inv},
                        "mrr": {"total_paise": mrr_total, "monthly_buckets": mrr_buckets, "months_with_income": len(mrr_buckets), "verification": "unverified", "note": "Monthly ledger income buckets UTC (unverified, not recurring subscription)."},
                        "verification": "unverified",
                        "note": "Real-data analytics from persisted owned rows (inclusive time window handled by dedicated Analytics service for filtered queries; dashboard shows all-time glance).",
                    }
                except Exception:
                    result["analytics"] = {"note": "analytics unavailable", "verification": "unverified"}
            # Phase 14 revenue intelligence — always provide (real persisted, empty-state aware)
            try:
                if _rev_dashboard_for_conn is not None:
                    rev = _rev_dashboard_for_conn(conn, owner, now, from_ts=0, to_ts=4102444800, version=version)
                    result["revenue_analytics"] = rev
                    result["business_intelligence"] = rev
                    result["revenue_charts"] = rev["revenue_charts"]
                    result["expense_charts"] = rev["expense_charts"]
                    result["opportunity_funnel"] = rev["opportunity_funnel"]
                    result["customer_activity"] = rev["customer_activity"]
                    result["total_opportunities"] = rev["total_opportunities"]
                    result["active_opportunities"] = rev["active_opportunities"]
                    result["applications_submitted"] = rev["applications_submitted"]
                    result["proposals_accepted"] = rev["proposals_accepted"]
                    result["active_customers"] = rev["active_customers"]
                    result["active_projects"] = rev["active_projects"]
                    result["orders"] = rev["orders"]
                    result["invoices"] = rev["invoices"]
                    result["outstanding_amounts"] = rev["outstanding_amounts"]
                    result["verified_received_payments"] = rev["verified_received_payments"]
                    result["expenses"] = rev["expenses"]
                    result["platform_fees"] = rev["platform_fees"]
                    result["net_realized_income"] = rev["net_realized_income"]
                    result["monthly_income"] = rev["monthly_income"]
                    result["monthly_expenses"] = rev["monthly_expenses"]
                    result["conversion_rate"] = rev["conversion_rate"]
                    result["average_project_value"] = rev["average_project_value"]
                    result["workload"] = rev["workload"]
                    result["task_completion"] = rev["task_completion"]
                    result["income_separation"] = rev["income_separation"]
            except Exception as _rev_e:
                result["revenue_analytics"] = {"error": str(_rev_e), "verification": "unverified", "empty_state": True, "note": "Revenue analytics unavailable"}
            return result

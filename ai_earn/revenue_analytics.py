"""Revenue analytics and business intelligence — real persisted data only.

No fabrication, no attractive synthetic metrics. All aggregates are simple
counts/sums over owner-scoped rows in the requested time window (inclusive
unix seconds). Empty data returns zeros with explicit empty-state notes.
Verified income is strictly separated from unverified and projected.
"""
from __future__ import annotations

import csv
import datetime
import io
import json
import sqlite3
from typing import Dict

from .auth import AuthService
from .financial_math import net_cash_paise
from .state_authorization import _authorized_transaction
from .state_storage import StateStorage, StateStorageError

_MAX_TS = 4102444800

class RevenueAnalyticsError(Exception):
    pass

def _ts(v: int, name: str) -> int:
    if not isinstance(v, int) or not 0 <= v <= _MAX_TS:
        raise RevenueAnalyticsError(f"Invalid {name}.")
    return v

def _range(from_ts: int, to_ts: int) -> tuple[int, int]:
    f = _ts(from_ts, "from_ts")
    t = _ts(to_ts, "to_ts")
    if f > t:
        raise RevenueAnalyticsError("from_ts must be <= to_ts.")
    return f, t

def _version_check(conn: sqlite3.Connection) -> int:
    from .state_storage import SUPPORTED_STATE_VERSIONS
    ver = conn.execute("PRAGMA user_version").fetchone()[0]
    if ver not in SUPPORTED_STATE_VERSIONS:
        raise StateStorageError("Unsupported schema version.")
    return ver

def _bucket_month(ts: int) -> str:
    dt = datetime.datetime.fromtimestamp(ts, datetime.timezone.utc)
    return f"{dt.year:04d}-{dt.month:02d}"

def _count(conn, table: str, owner: str, from_ts: int, to_ts: int, time_col: str = "created_at", extra: str = "", params: tuple = ()) -> int:
    q = f"SELECT COUNT(*) FROM {table} WHERE owner_id=? AND {time_col} BETWEEN ? AND ? {extra}"
    return conn.execute(q, (owner, from_ts, to_ts) + params).fetchone()[0]

def _sum_amount(conn, table: str, owner: str, amount_col: str, from_ts: int, to_ts: int, time_col: str = "created_at", extra: str = "", params: tuple = ()) -> int:
    q = f"SELECT COALESCE(SUM({amount_col}),0) FROM {table} WHERE owner_id=? AND {time_col} BETWEEN ? AND ? {extra}"
    return conn.execute(q, (owner, from_ts, to_ts) + params).fetchone()[0]

def _group_status(conn, table: str, owner: str, from_ts: int, to_ts: int, status_col: str, time_col: str = "created_at", extra: str = "", params: tuple = ()) -> Dict[str, int]:
    q = f"SELECT {status_col}, COUNT(*) FROM {table} WHERE owner_id=? AND {time_col} BETWEEN ? AND ? {extra} GROUP BY {status_col}"
    rows = conn.execute(q, (owner, from_ts, to_ts) + params).fetchall()
    return {r[0]: r[1] for r in rows}

def _avg_amount(conn, table: str, owner: str, amount_col: str, from_ts: int, to_ts: int, time_col: str = "created_at") -> int:
    q = f"SELECT COALESCE(AVG({amount_col}),0) FROM {table} WHERE owner_id=? AND {time_col} BETWEEN ? AND ?"
    row = conn.execute(q, (owner, from_ts, to_ts)).fetchone()[0]
    return int(row) if row is not None else 0

def _monthly_buckets(conn, owner: str, table: str, amount_col: str, time_col: str, from_ts: int, to_ts: int, *, extra_where: str = "", kind_filter: str | None = None) -> Dict[str, int]:
    if kind_filter is not None:
        rows = conn.execute(
            f"SELECT {time_col}, {amount_col} FROM {table} WHERE owner_id=? AND {time_col} BETWEEN ? AND ? AND kind=? {extra_where} ORDER BY {time_col}",
            (owner, from_ts, to_ts, kind_filter),
        ).fetchall()
    else:
        if extra_where:
            q = f"SELECT {time_col}, {amount_col} FROM {table} WHERE owner_id=? AND {time_col} BETWEEN ? AND ? {extra_where} ORDER BY {time_col}"
            rows = conn.execute(q, (owner, from_ts, to_ts)).fetchall()
        else:
            rows = conn.execute(
                f"SELECT {time_col}, {amount_col} FROM {table} WHERE owner_id=? AND {time_col} BETWEEN ? AND ? ORDER BY {time_col}",
                (owner, from_ts, to_ts),
            ).fetchall()
    buckets: Dict[str, int] = {}
    for r in rows:
        buckets[_bucket_month(r[time_col])] = buckets.get(_bucket_month(r[time_col]), 0) + r[amount_col]
    return buckets

def _compute_dashboard_inner(conn, owner: str, now: int, version: int, f: int, t: int) -> dict:
    def safe_count(table, time_col="created_at", extra="", params=()):
        try:
            return _count(conn, table, owner, f, t, time_col, extra, params)
        except Exception:
            return 0
    def safe_sum(table, amount_col, time_col="created_at", extra="", params=()):
        try:
            return _sum_amount(conn, table, owner, amount_col, f, t, time_col, extra, params)
        except Exception:
            return 0

    # 1 Total opportunities
    total_opps = safe_count("opportunities") if version >= 15 else 0
    # 2 Active opportunities
    active_opps = 0
    if version >= 15:
        try:
            active_opps = conn.execute("SELECT COUNT(*) FROM opportunities WHERE owner_id=? AND status IN ('discovered','vetted','pursuing') AND created_at BETWEEN ? AND ?", (owner, f, t)).fetchone()[0]
        except Exception:
            active_opps = 0
    # 3 Applications submitted: proposals sent+
    applications_submitted = 0
    if version >= 25:
        try:
            applications_submitted = conn.execute("SELECT COUNT(*) FROM proposals WHERE owner_id=? AND status IN ('sent','accepted','rejected','cancelled','archived') AND created_at BETWEEN ? AND ?", (owner, f, t)).fetchone()[0]
        except Exception:
            applications_submitted = 0
    # 4 Proposals accepted
    proposals_accepted = 0
    if version >= 25:
        try:
            proposals_accepted = conn.execute("SELECT COUNT(*) FROM proposals WHERE owner_id=? AND status='accepted' AND created_at BETWEEN ? AND ?", (owner, f, t)).fetchone()[0]
        except Exception:
            proposals_accepted = 0
    # 5 Active customers
    active_customers = 0
    if version >= 17:
        try:
            active_customers = conn.execute("SELECT COUNT(*) FROM customers WHERE owner_id=? AND status IN ('lead','contacted','converted') AND created_at BETWEEN ? AND ?", (owner, f, t)).fetchone()[0]
        except Exception:
            active_customers = 0
    # 6 Active projects
    active_projects = 0
    if version >= 23:
        try:
            if version >= 27:
                active_projects = conn.execute("""
                    SELECT COUNT(*) FROM customer_projects cp
                    JOIN project_tasks pt ON cp.project_task_id=pt.id AND cp.owner_id=pt.owner_id
                    WHERE cp.owner_id=? AND cp.created_at BETWEEN ? AND ?
                    AND pt.status IN ('draft','pending_review','approved','scheduled','in_progress')
                """, (owner, f, t)).fetchone()[0]
            else:
                active_projects = conn.execute("SELECT COUNT(*) FROM project_tasks WHERE owner_id=? AND status IN ('draft','pending_review','approved','scheduled','in_progress') AND created_at BETWEEN ? AND ?", (owner, f, t)).fetchone()[0]
        except Exception:
            active_projects = 0
    # 7 Orders
    orders_count = safe_count("customer_orders") if version >= 27 else 0
    orders_amount = safe_sum("customer_orders", "amount_paise") if version >= 27 else 0
    order_drafts_count = safe_count("order_drafts") if version >= 5 else 0
    order_drafts_amount = safe_sum("order_drafts", "proposed_amount_paise") if version >= 5 else 0
    # 8 Invoices
    invoices_count = safe_count("invoices") if version >= 20 else 0
    invoices_amount = safe_sum("invoices", "amount_paise") if version >= 20 else 0
    invoice_drafts_count = safe_count("invoice_drafts") if version >= 6 else 0
    invoice_drafts_amount = safe_sum("invoice_drafts", "proposed_amount_paise") if version >= 6 else 0
    # 9 Outstanding amounts
    outstanding_count = 0
    outstanding_amount = 0
    overdue_count = 0
    overdue_amount = 0
    if version >= 20:
        try:
            row = conn.execute("SELECT COUNT(*), COALESCE(SUM(amount_paise),0) FROM invoices WHERE owner_id=? AND status IN ('issued','overdue') AND created_at BETWEEN ? AND ?", (owner, f, t)).fetchone()
            outstanding_count, outstanding_amount = row[0], row[1]
            row2 = conn.execute("SELECT COUNT(*), COALESCE(SUM(amount_paise),0) FROM invoices WHERE owner_id=? AND status='overdue' AND created_at BETWEEN ? AND ?", (owner, f, t)).fetchone()
            overdue_count = row2[0]
            overdue_amount = row2[1]
            try:
                due_overdue = conn.execute("SELECT COUNT(*), COALESCE(SUM(amount_paise),0) FROM invoices WHERE owner_id=? AND status IN ('issued','overdue') AND due_at IS NOT NULL AND due_at < ? AND created_at BETWEEN ? AND ?", (owner, now, f, t)).fetchone()
                if due_overdue[0] > overdue_count:
                    overdue_count, overdue_amount = due_overdue[0], due_overdue[1]
            except Exception:
                pass
        except Exception:
            pass
    # 10 Verified received payments
    verified_received_count = 0
    verified_received_amount = 0
    if version >= 28:
        try:
            row = conn.execute("SELECT COUNT(*), COALESCE(SUM(amount_paise),0) FROM payment_records WHERE owner_id=? AND verified=1 AND status IN ('received','settled') AND created_at BETWEEN ? AND ?", (owner, f, t)).fetchone()
            verified_received_count, verified_received_amount = row[0], row[1]
        except Exception:
            pass
    # 11 Expenses
    expenses_count = 0
    expenses_amount = 0
    if version >= 28:
        try:
            row = conn.execute("SELECT COUNT(*), COALESCE(SUM(amount_paise),0) FROM expense_records WHERE owner_id=? AND created_at BETWEEN ? AND ?", (owner, f, t)).fetchone()
            expenses_count, expenses_amount = row[0], row[1]
        except Exception:
            pass
    ledger_expense_amount = 0
    if version >= 18:
        try:
            ledger_expense_amount = conn.execute("SELECT COALESCE(SUM(amount_paise),0) FROM ledger_entries WHERE owner_id=? AND kind='expense' AND created_at BETWEEN ? AND ?", (owner, f, t)).fetchone()[0]
        except Exception:
            pass
    # 12 Platform fees
    platform_fees_count = 0
    platform_fees_amount = 0
    if version >= 28:
        try:
            row = conn.execute("SELECT COUNT(*), COALESCE(SUM(amount_paise),0) FROM fee_records WHERE owner_id=? AND fee_type='platform' AND created_at BETWEEN ? AND ?", (owner, f, t)).fetchone()
            platform_fees_count, platform_fees_amount = row[0], row[1]
        except Exception:
            pass
    ledger_fee_amount = 0
    if version >= 18:
        try:
            ledger_fee_amount = conn.execute("SELECT COALESCE(SUM(amount_paise),0) FROM ledger_entries WHERE owner_id=? AND kind='fee' AND created_at BETWEEN ? AND ?", (owner, f, t)).fetchone()[0]
        except Exception:
            pass
    # 13 Net realized income (verified)
    net_realized_amount = 0
    net_realized_verified_breakdown = {}
    if version >= 28:
        try:
            settled = conn.execute("SELECT COALESCE(SUM(amount_paise),0) FROM payment_records WHERE owner_id=? AND verified=1 AND status='settled' AND created_at BETWEEN ? AND ?", (owner, f, t)).fetchone()[0]
            refunds = conn.execute("SELECT COALESCE(SUM(amount_paise),0) FROM refund_records WHERE owner_id=? AND verified=1 AND created_at BETWEEN ? AND ?", (owner, f, t)).fetchone()[0]
            fees = conn.execute("SELECT COALESCE(SUM(amount_paise),0) FROM fee_records WHERE owner_id=? AND verified=1 AND created_at BETWEEN ? AND ?", (owner, f, t)).fetchone()[0]
            exps = conn.execute("SELECT COALESCE(SUM(amount_paise),0) FROM expense_records WHERE owner_id=? AND verified=1 AND created_at BETWEEN ? AND ?", (owner, f, t)).fetchone()[0]
            net_realized_amount = net_cash_paise(
                income_paise=settled,
                refunds_paise=refunds,
                fees_paise=fees,
                expenses_paise=exps,
            )
            net_realized_verified_breakdown = {"settled_verified": settled, "refunds_verified": refunds, "fees_verified": fees, "expenses_verified": exps}
        except Exception:
            net_realized_amount = 0
    ledger_net = 0
    if version >= 18:
        try:
            ledger_net = conn.execute("SELECT COALESCE(SUM(CASE WHEN kind='income' THEN amount_paise WHEN kind IN ('expense','refund','fee') THEN -amount_paise ELSE 0 END),0) FROM ledger_entries WHERE owner_id=? AND created_at BETWEEN ? AND ?", (owner, f, t)).fetchone()[0]
        except Exception:
            pass
    # 14 Monthly income
    monthly_income_realized = {}
    monthly_income_unverified = {}
    if version >= 28:
        try:
            monthly_income_realized = _monthly_buckets(conn, owner, "payment_records", "amount_paise", "created_at", f, t, extra_where=" AND verified=1 AND status IN ('received','settled')")
        except Exception:
            monthly_income_realized = {}
    if version >= 18:
        try:
            monthly_income_unverified = _monthly_buckets(conn, owner, "ledger_entries", "amount_paise", "created_at", f, t, kind_filter="income")
        except Exception:
            monthly_income_unverified = {}
    # 15 Monthly expenses (verified expense_records preferred, fallback to ledger expense to show real data)
    monthly_expenses = {}
    monthly_expenses_ledger = {}
    if version >= 18:
        try:
            monthly_expenses_ledger = _monthly_buckets(conn, owner, "ledger_entries", "amount_paise", "created_at", f, t, kind_filter="expense")
        except Exception:
            monthly_expenses_ledger = {}
    if version >= 28:
        try:
            monthly_expenses = _monthly_buckets(conn, owner, "expense_records", "amount_paise", "created_at", f, t)
            if not monthly_expenses and monthly_expenses_ledger:
                monthly_expenses = monthly_expenses_ledger
        except Exception:
            monthly_expenses = monthly_expenses_ledger
    else:
        monthly_expenses = monthly_expenses_ledger
    # 16 Conversion rate
    conv = {}
    if version >= 15:
        try:
            opp_by_status = _group_status(conn, "opportunities", owner, f, t, "status")
            discovered = opp_by_status.get("discovered", 0)
            vetted = opp_by_status.get("vetted", 0)
            pursuing = opp_by_status.get("pursuing", 0)
            conv["opportunities"] = {"by_status": opp_by_status, "vetted_rate": (vetted / discovered) if discovered else 0.0, "pursuing_rate": (pursuing / vetted) if vetted else 0.0}
        except Exception:
            conv["opportunities"] = {"by_status": {}, "vetted_rate": 0.0, "pursuing_rate": 0.0}
    if version >= 17:
        try:
            cust_by_status = _group_status(conn, "customers", owner, f, t, "status")
            lead = cust_by_status.get("lead", 0)
            contacted = cust_by_status.get("contacted", 0)
            converted = cust_by_status.get("converted", 0)
            conv["customers"] = {"by_status": cust_by_status, "contacted_rate": (contacted / lead) if lead else 0.0, "converted_rate": (converted / contacted) if contacted else 0.0, "converted_over_lead": (converted / lead) if lead else 0.0}
        except Exception:
            conv["customers"] = {"by_status": {}, "contacted_rate": 0.0}
    if version >= 25:
        try:
            prop_by_status = _group_status(conn, "proposals", owner, f, t, "status")
            sent = prop_by_status.get("sent", 0)
            accepted = prop_by_status.get("accepted", 0)
            conv["proposals"] = {"by_status": prop_by_status, "accepted_rate": (accepted / sent) if sent else 0.0, "accepted_over_total": (accepted / sum(prop_by_status.values())) if sum(prop_by_status.values()) else 0.0}
        except Exception:
            conv["proposals"] = {"by_status": {}, "accepted_rate": 0.0}
    # 17 Average project value
    avg_project_value = 0
    if version >= 27:
        try:
            avg_project_value = _avg_amount(conn, "customer_orders", owner, "amount_paise", f, t)
        except Exception:
            avg_project_value = 0
        if avg_project_value == 0 and version >= 26:
            try:
                avg_project_value = _avg_amount(conn, "product_sales", owner, "amount_paise", f, t)
            except Exception:
                pass
    # 18 Workload and task completion
    workload = {}
    if version >= 7:
        try:
            queued = conn.execute("SELECT COUNT(*) FROM task_queue WHERE owner_id=? AND status IN ('queued','leased') AND created_at BETWEEN ? AND ?", (owner, f, t)).fetchone()[0]
            workload["queued_tasks"] = queued
        except Exception:
            workload["queued_tasks"] = 0
    if version >= 23:
        try:
            task_by_status = _group_status(conn, "project_tasks", owner, f, t, "status")
            total_tasks = sum(task_by_status.values())
            completed = task_by_status.get("completed", 0)
            workload["project_tasks"] = {"total": total_tasks, "by_status": task_by_status, "completed": completed, "completion_rate": (completed / total_tasks) if total_tasks else 0.0}
        except Exception:
            workload["project_tasks"] = {"total": 0, "by_status": {}, "completed": 0, "completion_rate": 0.0}
    if version >= 19:
        try:
            pending_follow = conn.execute("SELECT COUNT(*) FROM followups WHERE owner_id=? AND status='pending' AND created_at BETWEEN ? AND ?", (owner, f, t)).fetchone()[0]
            workload["pending_followups"] = pending_follow
        except Exception:
            workload["pending_followups"] = 0
    workload["ledger_net_paise"] = ledger_net
    # monthly trends aggregated
    projected_buckets = {}
    if version >= 5:
        try:
            projected_buckets = _monthly_buckets(conn, owner, "order_drafts", "proposed_amount_paise", "created_at", f, t)
        except Exception:
            projected_buckets = {}
    if version >= 13:
        try:
            rows = conn.execute("SELECT created_at, amount_paise FROM financial_assertions WHERE owner_id=? AND created_at BETWEEN ? AND ? ORDER BY created_at", (owner, f, t)).fetchall()
            for r in rows:
                k = _bucket_month(r["created_at"])
                projected_buckets[k] = projected_buckets.get(k, 0) + r["amount_paise"]
        except Exception:
            pass
    revenue_charts = {
        "realized_monthly": monthly_income_realized,
        "unverified_monthly": monthly_income_unverified,
        "projected_monthly": projected_buckets,
        "verification": "realized=verified settled payments; unverified=ledger income; projected=order_drafts + assertions (not revenue)",
        "empty_state": (not monthly_income_realized and not monthly_income_unverified and not projected_buckets),
    }
    # expense_charts uses verified monthly plus ledger breakdown; empty only if both empty
    exp_ledger_monthly = {}
    if version >= 18:
        try:
            exp_ledger_monthly = _monthly_buckets(conn, owner, "ledger_entries", "amount_paise", "created_at", f, t, kind_filter="expense")
        except Exception:
            exp_ledger_monthly = {}
    expense_charts = {
        "monthly": monthly_expenses,
        "ledger_expense_monthly": exp_ledger_monthly,
        "verification": "verified=expense_records; ledger=unverified ledger expense",
        "empty_state": not monthly_expenses and not exp_ledger_monthly,
    }
    opp_funnel = {}
    if version >= 15:
        try:
            opp_by_status = _group_status(conn, "opportunities", owner, f, t, "status")
            opp_funnel = {
                "total": sum(opp_by_status.values()),
                "by_status": opp_by_status,
                "active": active_opps,
                "verification": "unverified — manual/connector discovered, not verified or contacted",
                "empty_state": sum(opp_by_status.values()) == 0,
                "note": "Empty — no opportunities in this period" if sum(opp_by_status.values()) == 0 else "Counts are persisted owned rows",
            }
        except Exception:
            opp_funnel = {"total": 0, "by_status": {}, "empty_state": True}
    customer_activity = {}
    if version >= 17:
        try:
            cust_by_status = _group_status(conn, "customers", owner, f, t, "status")
            comm_count = safe_count("communications") if version >= 18 else 0
            order_count = orders_count
            follow_count = safe_count("followups") if version >= 19 else 0
            customer_activity = {
                "customers_by_status": cust_by_status,
                "total_customers": sum(cust_by_status.values()),
                "communications_in_period": comm_count,
                "orders_in_period": order_count,
                "followups_in_period": follow_count,
                "active_customers": active_customers,
                "verification": "unverified local CRM",
                "empty_state": sum(cust_by_status.values()) == 0,
                "note": "Empty — no customers in this period" if sum(cust_by_status.values()) == 0 else "Activity counts are persisted owned rows in window",
            }
        except Exception:
            customer_activity = {"total_customers": 0, "empty_state": True}
    empty = (total_opps == 0 and invoices_count == 0 and verified_received_count == 0 and expenses_count == 0 and orders_count == 0 and active_projects == 0 and total_opps == 0)
    return {
        "from_ts": f,
        "to_ts": t,
        "generated_at": now,
        "read_only": True,
        "empty_state": empty,
        "verification": "verified=reconciled settled payments; unverified=ledger/drafts; projected=drafts+assertions not revenue",
        "total_opportunities": {"count": total_opps, "verification": "unverified", "note": "Manual/connector discovered; not verified or contacted." if total_opps else "Empty — no opportunities in this period."},
        "active_opportunities": {"count": active_opps, "verification": "unverified", "note": "discovered/vetted/pursuing"},
        "applications_submitted": {"count": applications_submitted, "verification": "unverified", "note": "Proposals sent/accepted/rejected/cancelled/archived; draft not counted. Empty if no proposals."},
        "proposals_accepted": {"count": proposals_accepted, "verification": "unverified", "note": "status=accepted"},
        "active_customers": {"count": active_customers, "verification": "unverified", "note": "lead/contacted/converted; rejected excluded"},
        "active_projects": {"count": active_projects, "verification": "unverified", "note": "customer_projects linked to draft/pending_review/approved/scheduled/in_progress"},
        "orders": {"count": orders_count, "amount_paise": orders_amount, "order_drafts": {"count": order_drafts_count, "amount_paise": order_drafts_amount}, "verification": "unverified", "currency": "INR", "note": "customer_orders are unverified drafts until fulfilled; order_drafts are projected"},
        "invoices": {"count": invoices_count, "amount_paise": invoices_amount, "drafts": {"count": invoice_drafts_count, "amount_paise": invoice_drafts_amount}, "verification": "unverified", "currency": "INR", "note": "PDF stub, not issued demand; drafts not issued"},
        "outstanding_amounts": {"count": outstanding_count, "amount_paise": outstanding_amount, "overdue_count": overdue_count, "overdue_amount_paise": overdue_amount, "verification": "unverified", "currency": "INR", "empty_state": outstanding_count == 0, "note": "Empty — no outstanding invoices" if outstanding_count == 0 else "issued+overdue"},
        "verified_received_payments": {"count": verified_received_count, "amount_paise": verified_received_amount, "verification": "verified", "currency": "INR", "empty_state": verified_received_count == 0, "note": "Empty — no verified received payments in this period" if verified_received_count == 0 else "payment_records verified=1 status received/settled"},
        "expenses": {"count": expenses_count, "amount_paise": expenses_amount, "ledger_expense_paise": ledger_expense_amount, "verification": "verified vs unverified ledger", "currency": "INR", "empty_state": expenses_count == 0 and ledger_expense_amount == 0, "note": "Empty — no expenses" if expenses_count == 0 else "expense_records verified; ledger expense is unverified"},
        "platform_fees": {"count": platform_fees_count, "amount_paise": platform_fees_amount, "ledger_fee_paise": ledger_fee_amount, "verification": "verified", "currency": "INR", "note": "fee_records platform; ledger fee is unverified"},
        "net_realized_income": {"amount_paise": net_realized_amount, "breakdown": net_realized_verified_breakdown, "ledger_net_unverified": ledger_net, "verification": "verified", "currency": "INR", "note": "settled_verified - refunds_verified - fees_verified - expenses_verified; ledger_net is unverified income - expenses - refunds - fees", "empty_state": net_realized_amount == 0 and ledger_net == 0},
        "monthly_income": {"realized": monthly_income_realized, "unverified": monthly_income_unverified, "total_realized_paise": sum(monthly_income_realized.values()), "total_unverified_paise": sum(monthly_income_unverified.values()), "verification": "realized=verified; unverified=ledger", "empty_state": not monthly_income_realized and not monthly_income_unverified, "note": "Empty — no income in this period" if not monthly_income_realized and not monthly_income_unverified else "UTC calendar month buckets YYYY-MM"},
        "monthly_expenses": {"monthly": monthly_expenses, "total_paise": sum(monthly_expenses.values()), "verification": "verified expense_records; ledger fallback", "empty_state": not monthly_expenses, "note": "Empty — no expenses in this period" if not monthly_expenses else "UTC buckets"},
        "conversion_rate": conv,
        "average_project_value": {"amount_paise": avg_project_value, "currency": "INR", "verification": "unverified", "note": "Average customer_orders amount; fallback product_sales. Empty if 0", "empty_state": avg_project_value == 0},
        "workload": workload,
        "task_completion": workload.get("project_tasks", {"completion_rate": 0.0}),
        "monthly_trends": {"revenue_realized": monthly_income_realized, "expenses": monthly_expenses, "unverified_income": monthly_income_unverified, "projected": projected_buckets},
        "revenue_charts": revenue_charts,
        "expense_charts": expense_charts,
        "opportunity_funnel": opp_funnel,
        "customer_activity": customer_activity,
        "income_separation": {
            "realized_income": {"amount_paise": verified_received_amount, "verification": "verified", "note": "Verified received/settled payments only"},
            "unverified_income": {"amount_paise": sum(monthly_income_unverified.values()), "verification": "unverified", "note": "Ledger income; not verified settlement"},
            "projected_revenue": {"amount_paise": sum(projected_buckets.values()), "verification": "projected", "note": "Order drafts + assertions; not revenue, not contracted"},
            "net_realized": {"amount_paise": net_realized_amount, "verification": "verified"},
        },
    }

def dashboard_for_conn(conn, owner: str, now: int, *, from_ts: int = 0, to_ts: int = _MAX_TS, version: int | None = None) -> dict:
    f, t = _range(from_ts, to_ts)
    if version is None:
        try:
            version = conn.execute("PRAGMA user_version").fetchone()[0]
        except Exception:
            version = 28
    return _compute_dashboard_inner(conn, owner, now, version, f, t)

class RevenueAnalytics:
    """Owner-scoped, read-only revenue intelligence."""

    def __init__(self, auth: AuthService, state: StateStorage):
        self._auth = auth
        self._state = state

    @staticmethod
    def _dashboard_conn(conn, owner: str, now: int, *, from_ts: int = 0, to_ts: int = _MAX_TS, version: int | None = None) -> dict:
        return dashboard_for_conn(conn, owner, now, from_ts=from_ts, to_ts=to_ts, version=version)

    def dashboard(self, token: str, *, from_ts: int = 0, to_ts: int = _MAX_TS) -> dict:
        f, t = _range(from_ts, to_ts)
        with _authorized_transaction(self._auth, self._state, token) as (conn, owner, now):
            ver = _version_check(conn)
            return _compute_dashboard_inner(conn, owner, now, ver, f, t)

    def revenue_analytics(self, token: str, *, from_ts: int = 0, to_ts: int = _MAX_TS):
        return self.dashboard(token, from_ts=from_ts, to_ts=to_ts)

    def business_intelligence(self, token: str, *, from_ts: int = 0, to_ts: int = _MAX_TS):
        return self.dashboard(token, from_ts=from_ts, to_ts=to_ts)

    def monthly_trends(self, token: str, *, from_ts: int = 0, to_ts: int = _MAX_TS):
        d = self.dashboard(token, from_ts=from_ts, to_ts=to_ts)
        return d["monthly_trends"]

    def revenue_trends(self, token: str, *, from_ts: int = 0, to_ts: int = _MAX_TS):
        return self.monthly_trends(token, from_ts=from_ts, to_ts=to_ts)

    def expense_trends(self, token: str, *, from_ts: int = 0, to_ts: int = _MAX_TS):
        d = self.dashboard(token, from_ts=from_ts, to_ts=to_ts)
        return {"monthly_expenses": d["monthly_expenses"], "expense_charts": d["expense_charts"]}

    def opportunity_funnel(self, token: str, *, from_ts: int = 0, to_ts: int = _MAX_TS):
        d = self.dashboard(token, from_ts=from_ts, to_ts=to_ts)
        return d["opportunity_funnel"]

    def customer_activity(self, token: str, *, from_ts: int = 0, to_ts: int = _MAX_TS):
        d = self.dashboard(token, from_ts=from_ts, to_ts=to_ts)
        return d["customer_activity"]

    def conversion(self, token: str, *, from_ts: int = 0, to_ts: int = _MAX_TS):
        d = self.dashboard(token, from_ts=from_ts, to_ts=to_ts)
        return d["conversion_rate"]

    def export_csv(self, token: str, *, from_ts: int = 0, to_ts: int = _MAX_TS) -> str:
        f, t = _range(from_ts, to_ts)
        d = self.dashboard(token, from_ts=f, to_ts=t)
        out = io.StringIO()
        w = csv.writer(out, lineterminator="\n")
        w.writerow(["metric", "period_from", "period_to", "value_paise_or_count", "verification", "note"])
        w.writerow(["total_opportunities", f, t, d["total_opportunities"]["count"], "unverified", d["total_opportunities"]["note"]])
        w.writerow(["active_opportunities", f, t, d["active_opportunities"]["count"], "unverified", ""])
        w.writerow(["applications_submitted", f, t, d["applications_submitted"]["count"], "unverified", ""])
        w.writerow(["proposals_accepted", f, t, d["proposals_accepted"]["count"], "unverified", ""])
        w.writerow(["active_customers", f, t, d["active_customers"]["count"], "unverified", ""])
        w.writerow(["active_projects", f, t, d["active_projects"]["count"], "unverified", ""])
        w.writerow(["orders_count", f, t, d["orders"]["count"], "unverified", ""])
        w.writerow(["orders_amount_paise", f, t, d["orders"]["amount_paise"], "unverified", "INR"])
        w.writerow(["invoices_count", f, t, d["invoices"]["count"], "unverified", ""])
        w.writerow(["invoices_amount_paise", f, t, d["invoices"]["amount_paise"], "unverified", "INR"])
        w.writerow(["outstanding_count", f, t, d["outstanding_amounts"]["count"], "unverified", ""])
        w.writerow(["outstanding_amount_paise", f, t, d["outstanding_amounts"]["amount_paise"], "unverified", "INR"])
        w.writerow(["verified_received_payments_count", f, t, d["verified_received_payments"]["count"], "verified", ""])
        w.writerow(["verified_received_payments_amount_paise", f, t, d["verified_received_payments"]["amount_paise"], "verified", "INR"])
        w.writerow(["expenses_count", f, t, d["expenses"]["count"], "verified", ""])
        w.writerow(["expenses_amount_paise", f, t, d["expenses"]["amount_paise"], "verified", "INR"])
        w.writerow(["platform_fees_amount_paise", f, t, d["platform_fees"]["amount_paise"], "verified", "INR"])
        w.writerow(["net_realized_income_paise", f, t, d["net_realized_income"]["amount_paise"], "verified", "settled - refunds - fees - expenses"])
        w.writerow(["average_project_value_paise", f, t, d["average_project_value"]["amount_paise"], "unverified", ""])
        for mon, amt in sorted(d["monthly_income"]["realized"].items()):
            w.writerow([f"monthly_income_realized_{mon}", f, t, amt, "verified", "UTC bucket"])
        for mon, amt in sorted(d["monthly_income"]["unverified"].items()):
            w.writerow([f"monthly_income_unverified_{mon}", f, t, amt, "unverified", "UTC bucket"])
        for mon, amt in sorted(d["monthly_expenses"]["monthly"].items()):
            w.writerow([f"monthly_expenses_{mon}", f, t, amt, "verified", "UTC bucket"])
        for st, rate in d["conversion_rate"].items():
            if isinstance(rate, dict):
                for k, v in rate.items():
                    if isinstance(v, float):
                        w.writerow([f"conversion_{st}_{k}", f, t, v, "unverified", "rate 0-1"])
        w.writerow(["task_completion_rate", f, t, d["task_completion"].get("completion_rate", 0.0), "unverified", "completed/total"])
        if d["empty_state"]:
            w.writerow(["empty_state", f, t, "true", "none", "Empty — no data in this period; no fabrication"])
        csv_text = out.getvalue()
        if len(csv_text) > 1_000_000:
            raise RevenueAnalyticsError("Export exceeds 1M chars.")
        return csv_text

    def export_report(self, token: str, *, from_ts: int = 0, to_ts: int = _MAX_TS, fmt: str = "csv") -> str:
        if fmt not in ("csv", "json"):
            raise RevenueAnalyticsError("Invalid format.")
        if fmt == "csv":
            return self.export_csv(token, from_ts=from_ts, to_ts=to_ts)
        d = self.dashboard(token, from_ts=from_ts, to_ts=to_ts)
        return json.dumps(d, ensure_ascii=True, indent=2, sort_keys=True)

    def workload(self, token: str, *, from_ts: int = 0, to_ts: int = _MAX_TS):
        d = self.dashboard(token, from_ts=from_ts, to_ts=to_ts)
        return d["workload"]

    def task_completion(self, token: str, *, from_ts: int = 0, to_ts: int = _MAX_TS):
        d = self.dashboard(token, from_ts=from_ts, to_ts=to_ts)
        return d["task_completion"]

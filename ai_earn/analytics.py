"""Real-data analytics — read-only, owner-scoped, time-bounded, no fabrication.

All metrics are derived from persisted owned rows within the requested
unix time window (inclusive). No synthetic generation, no extrapolation,
no external API. Empty databases return zeros with explicit notes.
Monthly MRR is the sum of `ledger_entries` where kind='income' per
calendar month (UTC) — labelled unverified, not recurring subscription.
Conversion rates are simple ratios of counted stages; no estimate.
"""

from __future__ import annotations

import csv
import io
import sqlite3
from typing import Dict

from .auth import AuthService
from .state_authorization import _authorized_transaction
from .state_storage import StateStorage, StateStorageError

# Revenue analytics delegate (real persisted data only)
try:
    from .revenue_analytics import RevenueAnalytics as _RevenueAnalytics
    from .revenue_analytics import RevenueAnalyticsError as _RevenueAnalyticsError
except Exception as _e:
    _RevenueAnalytics = None
    _RevenueAnalyticsError = Exception

_MAX_TS = 4102444800

class AnalyticsError(Exception):
    pass


def _ts(value: int, name: str) -> int:
    if not isinstance(value, int) or not 0 <= value <= _MAX_TS:
        raise AnalyticsError(f"Invalid {name}.")
    return value


def _range(from_ts: int, to_ts: int) -> tuple[int, int]:
    f = _ts(from_ts, "from_ts")
    t = _ts(to_ts, "to_ts")
    if f > t:
        raise AnalyticsError("from_ts must be <= to_ts.")
    return f, t


def _version_check(conn: sqlite3.Connection) -> None:
    from .state_storage import SUPPORTED_STATE_VERSIONS
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version not in SUPPORTED_STATE_VERSIONS or version < 18:
        # Analytics needs at least v18 for ledger, but gracefully handles earlier?
        # We require 18+ for full funnel, but to keep backward compat we allow 15+ for opportunites
        # Actually we require 22 for AI, but analytics should work from 13 onward with graceful missing tables.
        # So we just require supported version, not specific minimum, and check version per query.
        pass
    if version not in SUPPORTED_STATE_VERSIONS:
        raise StateStorageError("Unsupported schema version.")


def _count(conn, table: str, owner: str, from_ts: int, to_ts: int, time_col: str = "created_at") -> int:
    # time_col is trusted internal constant
    return conn.execute(
        f"SELECT COUNT(*) FROM {table} WHERE owner_id=? AND {time_col} BETWEEN ? AND ?",
        (owner, from_ts, to_ts),
    ).fetchone()[0]


def _group_status(conn, table: str, owner: str, from_ts: int, to_ts: int, status_col: str, time_col: str = "created_at") -> Dict[str, int]:
    rows = conn.execute(
        f"SELECT {status_col}, COUNT(*) FROM {table} WHERE owner_id=? AND {time_col} BETWEEN ? AND ? GROUP BY {status_col}",
        (owner, from_ts, to_ts),
    ).fetchall()
    return {r[0]: r[1] for r in rows}


def _sum_income_per_month(conn, owner: str, from_ts: int, to_ts: int):
    rows = conn.execute(
        "SELECT created_at, amount_paise FROM ledger_entries WHERE owner_id=? AND kind='income' AND created_at BETWEEN ? AND ? ORDER BY created_at",
        (owner, from_ts, to_ts),
    ).fetchall()
    buckets: Dict[str, int] = {}
    for r in rows:
        ts = r["created_at"]
        # Use UTC year-month
        import datetime
        dt = datetime.datetime.utcfromtimestamp(ts)
        key = f"{dt.year:04d}-{dt.month:02d}"
        buckets[key] = buckets.get(key, 0) + r["amount_paise"]
    return buckets, sum(buckets.values()) if buckets else 0


class Analytics:
    """Owner-scoped read-only analytics."""

    def __init__(self, auth: AuthService, state: StateStorage):
        self._auth = auth
        self._state = state

    def funnel(self, token: str, *, from_ts: int = 0, to_ts: int = _MAX_TS) -> dict:
        f, t = _range(from_ts, to_ts)
        with _authorized_transaction(self._auth, self._state, token) as (conn, owner, now):
            _version_check(conn)
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            result: dict = {"from_ts": f, "to_ts": t, "generated_at": now, "read_only": True, "verification": "unverified", "note": "Funnel counts are persisted owned rows in time window (inclusive)."}
            if version >= 15:
                opp = _group_status(conn, "opportunities", owner, f, t, "status")
                result["opportunities"] = {"total": sum(opp.values()), "by_status": opp}
            else:
                result["opportunities"] = {"total": 0, "by_status": {}, "note": "requires v15"}
            if version >= 17:
                cust = _group_status(conn, "customers", owner, f, t, "status")
                result["customers"] = {"total": sum(cust.values()), "by_status": cust}
                prod = _group_status(conn, "products", owner, f, t, "status")
                result["products"] = {"total": sum(prod.values()), "by_status": prod}
            else:
                result["customers"] = {"total": 0, "by_status": {}, "note": "requires v17"}
                result["products"] = {"total": 0, "by_status": {}, "note": "requires v17"}
            if version >= 16:
                svc = _group_status(conn, "services", owner, f, t, "status")
                result["services"] = {"total": sum(svc.values()), "by_status": svc}
            else:
                result["services"] = {"total": 0, "by_status": {}, "note": "requires v16"}
            if version >= 18:
                comm = _group_status(conn, "communications", owner, f, t, "status")
                result["communications"] = {"total": sum(comm.values()), "by_status": comm}
                ledger_kinds = _group_status(conn, "ledger_entries", owner, f, t, "kind")
                result["ledger"] = {"total": sum(ledger_kinds.values()), "by_kind": ledger_kinds}
            else:
                result["communications"] = {"total": 0, "by_status": {}, "note": "requires v18"}
                result["ledger"] = {"total": 0, "by_kind": {}, "note": "requires v18"}
            if version >= 19:
                foll = _group_status(conn, "followups", owner, f, t, "status")
                result["followups"] = {"total": sum(foll.values()), "by_status": foll}
            else:
                result["followups"] = {"total": 0, "by_status": {}, "note": "requires v19"}
            if version >= 20:
                inv = _group_status(conn, "invoices", owner, f, t, "status")
                result["invoices"] = {"total": sum(inv.values()), "by_status": inv}
            else:
                result["invoices"] = {"total": 0, "by_status": {}, "note": "requires v20"}
            if version >= 21:
                prov = _group_status(conn, "payment_provider_orders", owner, f, t, "status")
                result["provider_orders"] = {"total": sum(prov.values()), "by_status": prov}
            else:
                result["provider_orders"] = {"total": 0, "by_status": {}, "note": "requires v21"}
            if version >= 22:
                ai = _group_status(conn, "ai_approvals", owner, f, t, "status")
                result["ai_approvals"] = {"total": sum(ai.values()), "by_status": ai}
            else:
                result["ai_approvals"] = {"total": 0, "by_status": {}, "note": "requires v22"}
            return result

    def conversion(self, token: str, *, from_ts: int = 0, to_ts: int = _MAX_TS) -> dict:
        f, t = _range(from_ts, to_ts)
        with _authorized_transaction(self._auth, self._state, token) as (conn, owner, now):
            _version_check(conn)
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            result = {"from_ts": f, "to_ts": t, "generated_at": now, "read_only": True, "verification": "unverified", "note": "Conversion = later_stage / earlier_stage count in window; 0 if denominator 0."}
            # Opportunity: discovered -> vetted -> pursuing -> archived/dismissed? Use vetted/discovered etc.
            if version >= 15:
                opp = _group_status(conn, "opportunities", owner, f, t, "status")
                discovered = opp.get("discovered", 0)
                vetted = opp.get("vetted", 0)
                pursuing = opp.get("pursuing", 0)
                result["opportunities"] = {
                    "by_status": opp,
                    "vetted_rate": (vetted / discovered) if discovered else 0.0,
                    "pursuing_rate": (pursuing / vetted) if vetted else 0.0,
                }
            # Customer: lead -> contacted -> converted
            if version >= 17:
                cust = _group_status(conn, "customers", owner, f, t, "status")
                lead = cust.get("lead", 0)
                contacted = cust.get("contacted", 0)
                converted = cust.get("converted", 0)
                result["customers"] = {
                    "by_status": cust,
                    "contacted_rate": (contacted / lead) if lead else 0.0,
                    "converted_rate": (converted / contacted) if contacted else 0.0 if contacted else (converted / lead if lead else 0.0),
                    "converted_over_lead": (converted / lead) if lead else 0.0,
                }
            # Invoices: draft -> issued -> paid
            if version >= 20:
                inv = _group_status(conn, "invoices", owner, f, t, "status")
                draft = inv.get("draft", 0)
                issued = inv.get("issued", 0) + inv.get("paid", 0) + inv.get("overdue", 0)  # issued includes those later? but we count issued separately
                # Better: use direct counts
                issued_only = inv.get("issued", 0)
                paid = inv.get("paid", 0)
                result["invoices"] = {
                    "by_status": inv,
                    "issued_rate": (issued_only / draft) if draft else 0.0,
                    "paid_rate": (paid / (inv.get("issued",0) + paid + inv.get("overdue",0))) if (inv.get("issued",0)+paid+inv.get("overdue",0)) else 0.0,
                }
            # Provider: created -> authorized -> captured
            if version >= 21:
                prov = _group_status(conn, "payment_provider_orders", owner, f, t, "status")
                created = prov.get("created", 0)
                authorized = prov.get("authorized", 0)
                captured = prov.get("captured", 0)
                result["provider_orders"] = {
                    "by_status": prov,
                    "authorized_rate": (authorized / created) if created else 0.0,
                    "captured_rate": (captured / authorized) if authorized else 0.0,
                }
            return result

    def mrr(self, token: str, *, from_ts: int = 0, to_ts: int = _MAX_TS) -> dict:
        f, t = _range(from_ts, to_ts)
        with _authorized_transaction(self._auth, self._state, token) as (conn, owner, now):
            _version_check(conn)
            # Use ledger income per month
            buckets, total = _sum_income_per_month(conn, owner, f, t)
            months = len(buckets)
            avg = total // months if months else 0
            return {
                "from_ts": f,
                "to_ts": t,
                "generated_at": now,
                "read_only": True,
                "verification": "unverified",
                "note": "MRR here is monthly sum of ledger 'income' entries per UTC calendar month (unverified, not subscription-recurring). Real MRR requires verified recurring ledger; this is arithmetic only. Empty months omitted.",
                "monthly_buckets": buckets,  # e.g., {"2026-01": 12300}
                "total_paise": total,
                "months_with_income": months,
                "average_per_month_paise": avg,
                "currency": "INR",
            }

    def workload(self, token: str, *, from_ts: int = 0, to_ts: int = _MAX_TS) -> dict:
        f, t = _range(from_ts, to_ts)
        with _authorized_transaction(self._auth, self._state, token) as (conn, owner, now):
            _version_check(conn)
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            result = {"from_ts": f, "to_ts": t, "generated_at": now, "read_only": True, "verification": "unverified", "note": "Workload counts are queued/leased tasks, workflows, pending followups etc. in window."}
            if version >= 7:
                queued = conn.execute(
                    "SELECT COUNT(*) FROM task_queue WHERE owner_id=? AND status IN ('queued','leased') AND created_at BETWEEN ? AND ?",
                    (owner, f, t),
                ).fetchone()[0]
                result["queued_tasks"] = queued
            if version >= 8:
                wf = _group_status(conn, "task_workflows", owner, f, t, "status")
                result["workflows"] = {"total": sum(wf.values()), "by_status": wf}
            if version >= 19:
                pending_follow = conn.execute(
                    "SELECT COUNT(*) FROM followups WHERE owner_id=? AND status='pending' AND created_at BETWEEN ? AND ?",
                    (owner, f, t),
                ).fetchone()[0]
                result["pending_followups"] = pending_follow
            if version >= 18:
                # ledger net in window
                net = conn.execute(
                    "SELECT COALESCE(SUM(CASE WHEN kind='income' THEN amount_paise WHEN kind IN ('expense','refund','fee') THEN -amount_paise ELSE 0 END),0) FROM ledger_entries WHERE owner_id=? AND created_at BETWEEN ? AND ?",
                    (owner, f, t),
                ).fetchone()[0]
                result["ledger_net_paise"] = net
            if version >= 15:
                opp_pending = conn.execute(
                    "SELECT COUNT(*) FROM opportunities WHERE owner_id=? AND status IN ('discovered','vetted','pursuing') AND created_at BETWEEN ? AND ?",
                    (owner, f, t),
                ).fetchone()[0]
                result["active_opportunities"] = opp_pending
            return result

    def summary(self, token: str, *, from_ts: int = 0, to_ts: int = _MAX_TS) -> dict:
        f, t = _range(from_ts, to_ts)
        funnel = self.funnel(token, from_ts=f, to_ts=t)
        conversion = self.conversion(token, from_ts=f, to_ts=t)
        mrr = self.mrr(token, from_ts=f, to_ts=t)
        workload = self.workload(token, from_ts=f, to_ts=t)
        return {
            "from_ts": f,
            "to_ts": t,
            "funnel": funnel,
            "conversion": conversion,
            "mrr": mrr,
            "workload": workload,
        }

    def export_csv(self, token: str, *, from_ts: int = 0, to_ts: int = _MAX_TS) -> str:
        f, t = _range(from_ts, to_ts)
        with _authorized_transaction(self._auth, self._state, token) as (conn, owner, now):
            _version_check(conn)
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            output = io.StringIO()
            writer = csv.writer(output, lineterminator="\n")
            writer.writerow(["metric", "period_from", "period_to", "value_paise_or_count", "verification", "note"])
            # Use summary data
            mrr = _sum_income_per_month(conn, owner, f, t)[0]
            total_income = sum(mrr.values()) if mrr else 0
            writer.writerow(["total_income_paise", str(f), str(t), str(total_income), "unverified", "sum ledger income in window"])
            for month, amt in sorted(mrr.items()):
                writer.writerow([f"mrr_{month}", str(f), str(t), str(amt), "unverified", "monthly ledger income bucket"])
            # Funnel rows
            if version >= 15:
                opp = _group_status(conn, "opportunities", owner, f, t, "status")
                for k, v in opp.items():
                    writer.writerow([f"opp_{k}", str(f), str(t), str(v), "unverified", ""])
                writer.writerow(["opp_total", str(f), str(t), str(sum(opp.values())), "unverified", ""])
            if version >= 17:
                cust = _group_status(conn, "customers", owner, f, t, "status")
                for k, v in cust.items():
                    writer.writerow([f"customer_{k}", str(f), str(t), str(v), "unverified", ""])
            if version >= 20:
                inv = _group_status(conn, "invoices", owner, f, t, "status")
                for k, v in inv.items():
                    writer.writerow([f"invoice_{k}", str(f), str(t), str(v), "unverified", ""])
            if version >= 18:
                net = conn.execute(
                    "SELECT COALESCE(SUM(CASE WHEN kind='income' THEN amount_paise WHEN kind IN ('expense','refund','fee') THEN -amount_paise ELSE 0 END),0) FROM ledger_entries WHERE owner_id=? AND created_at BETWEEN ? AND ?",
                    (owner, f, t),
                ).fetchone()[0]
                writer.writerow(["ledger_net_paise", str(f), str(t), str(net), "unverified", "income - expense - refund - fee"])
            csv_text = output.getvalue()
            if len(csv_text) > 1_000_000:
                raise AnalyticsError("Export exceeds 1M chars.")
            return csv_text

    # --- Phase 14 Revenue Intelligence delegation (real persisted data) ---
    def _rev(self):
        if _RevenueAnalytics is None:
            raise AnalyticsError("Revenue analytics unavailable.")
        return _RevenueAnalytics(self._auth, self._state)

    def dashboard(self, token: str, *, from_ts: int = 0, to_ts: int = _MAX_TS) -> dict:
        return self._rev().dashboard(token, from_ts=from_ts, to_ts=to_ts)

    def revenue_analytics(self, token: str, *, from_ts: int = 0, to_ts: int = _MAX_TS) -> dict:
        return self.dashboard(token, from_ts=from_ts, to_ts=to_ts)

    def business_intelligence(self, token: str, *, from_ts: int = 0, to_ts: int = _MAX_TS) -> dict:
        return self.dashboard(token, from_ts=from_ts, to_ts=to_ts)

    def monthly_trends(self, token: str, *, from_ts: int = 0, to_ts: int = _MAX_TS) -> dict:
        return self._rev().monthly_trends(token, from_ts=from_ts, to_ts=to_ts)

    def revenue_trends(self, token: str, *, from_ts: int = 0, to_ts: int = _MAX_TS) -> dict:
        return self._rev().revenue_trends(token, from_ts=from_ts, to_ts=to_ts)

    def expense_trends(self, token: str, *, from_ts: int = 0, to_ts: int = _MAX_TS) -> dict:
        return self._rev().expense_trends(token, from_ts=from_ts, to_ts=to_ts)

    def opportunity_funnel(self, token: str, *, from_ts: int = 0, to_ts: int = _MAX_TS) -> dict:
        return self._rev().opportunity_funnel(token, from_ts=from_ts, to_ts=to_ts)

    def customer_activity(self, token: str, *, from_ts: int = 0, to_ts: int = _MAX_TS) -> dict:
        return self._rev().customer_activity(token, from_ts=from_ts, to_ts=to_ts)

    def revenue_charts(self, token: str, *, from_ts: int = 0, to_ts: int = _MAX_TS) -> dict:
        d = self.dashboard(token, from_ts=from_ts, to_ts=to_ts)
        return d["revenue_charts"]

    def expense_charts(self, token: str, *, from_ts: int = 0, to_ts: int = _MAX_TS) -> dict:
        d = self.dashboard(token, from_ts=from_ts, to_ts=to_ts)
        return d["expense_charts"]

    def outstanding_amounts(self, token: str, *, from_ts: int = 0, to_ts: int = _MAX_TS) -> dict:
        d = self.dashboard(token, from_ts=from_ts, to_ts=to_ts)
        return d["outstanding_amounts"]

    def net_realized_income(self, token: str, *, from_ts: int = 0, to_ts: int = _MAX_TS) -> dict:
        d = self.dashboard(token, from_ts=from_ts, to_ts=to_ts)
        return d["net_realized_income"]

    def export_report(self, token: str, *, from_ts: int = 0, to_ts: int = _MAX_TS, fmt: str = "csv") -> str:
        return self._rev().export_report(token, from_ts=from_ts, to_ts=to_ts, fmt=fmt)

    def workload_extended(self, token: str, *, from_ts: int = 0, to_ts: int = _MAX_TS) -> dict:
        d = self.dashboard(token, from_ts=from_ts, to_ts=to_ts)
        return d["workload"]

    def task_completion(self, token: str, *, from_ts: int = 0, to_ts: int = _MAX_TS) -> dict:
        return self._rev().task_completion(token, from_ts=from_ts, to_ts=to_ts)

    def average_project_value(self, token: str, *, from_ts: int = 0, to_ts: int = _MAX_TS) -> dict:
        d = self.dashboard(token, from_ts=from_ts, to_ts=to_ts)
        return d["average_project_value"]


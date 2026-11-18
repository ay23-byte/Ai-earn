"""Cross-report reconciliation (P1-3): finance records vs ledger vs analytics.

Compares owner-scoped totals that are computed through different pipelines
(payment_records, ledger_entries) and reports exact paise discrepancies.
Read-only; never mutates financial state.
"""

from __future__ import annotations

import sqlite3

from .auth import AuthService
from .state_authorization import _authorized_transaction
from .state_storage import StateStorage, StateStorageError


class ReportingReconciliationError(Exception):
    """Reconciliation could not be performed."""


def _version(conn: sqlite3.Connection) -> None:
    if conn.execute("PRAGMA user_version").fetchone()[0] < 28:
        raise StateStorageError("State schema version 28 or later is required for reporting reconciliation.")


class ReportingReconciliation:
    def __init__(self, auth: AuthService, state: StateStorage):
        self._auth = auth
        self._state = state

    def reconcile(self, token: str) -> dict:
        """Return per-source verified totals and any exact discrepancies."""
        with _authorized_transaction(self._auth, self._state, token) as (conn, owner, _now):
            _version(conn)
            settled_verified = conn.execute(
                "SELECT COALESCE(SUM(amount_paise),0) FROM payment_records WHERE owner_id=? AND status='settled' AND verified=1",
                (owner,),
            ).fetchone()[0]
            ledger_income = conn.execute(
                "SELECT COALESCE(SUM(amount_paise),0) FROM ledger_entries WHERE owner_id=? AND kind='income'",
                (owner,),
            ).fetchone()[0]
            ledger_expense = conn.execute(
                "SELECT COALESCE(SUM(amount_paise),0) FROM ledger_entries WHERE owner_id=? AND kind='expense'",
                (owner,),
            ).fetchone()[0]
            discrepancies = []
            if settled_verified != ledger_income:
                discrepancies.append(
                    {
                        "field": "verified_income_paise",
                        "payment_records_paise": settled_verified,
                        "ledger_paise": ledger_income,
                        "difference_paise": settled_verified - ledger_income,
                    }
                )
            return {
                "settled_verified_paise": settled_verified,
                "ledger_income_paise": ledger_income,
                "ledger_expense_paise": ledger_expense,
                "discrepancies": discrepancies,
                "consistent": not discrepancies,
            }

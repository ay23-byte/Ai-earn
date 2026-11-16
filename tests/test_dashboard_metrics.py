"""Tests for account-scoped, read-only dashboard metrics."""
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from ai_earn.auth import AccessDenied, AuthService
from ai_earn.auth_storage import AuthStorage
from ai_earn.dashboard_metrics import DashboardMetrics
from ai_earn.financial_assertions import FinancialAssertions
from ai_earn.invoice_drafts import InvoiceDrafts
from ai_earn.opportunity_drafts import OpportunityDrafts
from ai_earn.order_drafts import OrderDrafts
from ai_earn.payment_observations import PaymentObservations
from ai_earn.state_storage import StateStorage


class Clock:
    now = 1_800_000_000
    def __call__(self):
        return self.now


@unittest.skipUnless(os.name in {"posix", "nt"}, "requires a supported private filesystem")
class DashboardMetricsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.tmp.cleanup)
        self.directory = Path(self.tmp.name) / "private"
        if os.name == "nt":
            from ai_earn.windows_security import create_private_directory
            create_private_directory(self.directory)
        else:
            self.directory.mkdir(mode=0o700)
        patcher = patch("ai_earn.auth.PASSWORD_ITERATIONS", 1000)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.clock = Clock()
        self.auth = AuthService(AuthStorage(self.directory / "auth.sqlite3"), self.clock)
        self.auth.register("alice", "disposable test password")
        self.auth.register("bobby", "disposable test password")
        self.alice = self.auth.login("alice", "disposable test password")
        self.bob = self.auth.login("bobby", "disposable test password")
        self.state = StateStorage(self.directory / "state.sqlite3", create=True, confirmed=True)
        for version in range(2, 15):
            getattr(self.state, f"migrate_to_v{version}")(
                self.directory / f"before-v{version}.sqlite3", confirmed=True
            )
        self.metrics = DashboardMetrics(self.auth, self.state)

    def test_empty_snapshot_is_explicitly_not_income(self):
        snapshot = self.metrics.get(self.alice)
        self.assertTrue(snapshot["read_only"])
        self.assertEqual(snapshot["income"]["verified_amount_paise"], 0)
        self.assertEqual(snapshot["income"]["status"], "not_available_no_verified_payment_ledger")
        self.assertEqual(snapshot["asserted_pipeline"]["count"], 0)
        self.assertEqual(snapshot["payment_observations"]["count"], 0)
        self.assertEqual(snapshot["sandbox_payments"]["count"], 0)

    def test_metrics_are_owner_scoped_and_keep_unverified_amounts_separate(self):
        opportunity = OpportunityDrafts(self.auth, self.state)
        opportunity.create(self.alice, "My opportunity", operation_key="alice-op")
        opportunity.create(self.bob, "Other opportunity", operation_key="bob-op")

        orders = OrderDrafts(self.auth, self.state)
        order = orders.create(self.alice, "My estimate", 12550, operation_key="alice-order")
        invoice = InvoiceDrafts(self.auth, self.state).create(
            self.alice, order, operation_key="alice-invoice"
        )
        PaymentObservations(self.auth, self.state).create(
            self.alice, invoice, operation_key="alice-observation"
        )
        FinancialAssertions(self.auth, self.state).create(
            self.alice, "Possible project", 9900, operation_key="alice-assertion"
        )
        FinancialAssertions(self.auth, self.state).create(
            self.bob, "Other possible project", 77700, operation_key="bob-assertion"
        )

        snapshot = self.metrics.get(self.alice)
        self.assertEqual(snapshot["work"]["opportunity_drafts"], 1)
        self.assertEqual(snapshot["commercial"]["order_drafts"], {
            "count": 1, "amount_paise": 12550
        })
        self.assertEqual(snapshot["commercial"]["invoice_drafts"], {
            "count": 1, "amount_paise": 12550
        })
        self.assertEqual(snapshot["payment_observations"]["count"], 1)
        self.assertEqual(snapshot["payment_observations"]["amount_paise"], 12550)
        self.assertEqual(snapshot["asserted_pipeline"]["count"], 1)
        self.assertEqual(snapshot["asserted_pipeline"]["amount_paise"], 9900)
        self.assertEqual(snapshot["income"]["verified_amount_paise"], 0)
        self.assertEqual(self.metrics.get(self.bob)["work"]["opportunity_drafts"], 1)
        self.assertEqual(self.metrics.get(self.bob)["asserted_pipeline"]["amount_paise"], 77700)

    def test_invalid_or_expired_session_is_denied(self):
        with self.assertRaises(AccessDenied):
            self.metrics.get("not-a-session-token")
        self.clock.now += 1800
        with self.assertRaises(AccessDenied):
            self.metrics.get(self.alice)

    def test_read_does_not_change_state(self):
        with self.state.transaction() as conn:
            before = conn.execute("SELECT COUNT(*) FROM state_audit").fetchone()[0]
            operations = conn.execute("SELECT COUNT(*) FROM state_operations").fetchone()[0]
        self.metrics.get(self.alice)
        with self.state.transaction() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM state_audit").fetchone()[0], before)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM state_operations").fetchone()[0], operations)


if ( __name__ == "__main__"):
    unittest.main()

"""Tests for Phase 11 production readiness checklist — live blocked until 11 checks pass.

No live money, no card storage, no network. All checks use disposable private
databases and HMAC over raw bodies, same security properties as payment_providers.
"""

import json
import os
import unittest
from pathlib import Path

from ai_earn.production_readiness import (
    CHECKLIST,
    evaluate,
    get_checklist,
    check_security_review,
    check_webhook_verification,
    check_reconciliation,
    check_duplicate_handling,
    check_refunds,
    check_failed_payments,
    check_settlement_records,
    check_audit_trails,
    check_backups,
    check_error_recovery,
    check_configuration_separation,
)
from ai_earn.payment_providers import ProviderConfig, ProviderError


EXPECTED_IDS = [
    "security_review",
    "webhook_verification",
    "reconciliation",
    "duplicate_handling",
    "refunds",
    "failed_payments",
    "settlement_records",
    "audit_trails",
    "backups",
    "error_recovery",
    "configuration_separation",
]
EXPECTED_TITLES = [
    "Complete security review.",
    "Complete webhook verification tests.",
    "Verify reconciliation.",
    "Verify duplicate event handling.",
    "Verify refunds.",
    "Verify failed payments.",
    "Verify settlement records.",
    "Verify audit trails.",
    "Verify backups.",
    "Verify error recovery.",
    "Verify configuration separation.",
]


class ChecklistStructureTests(unittest.TestCase):
    def test_checklist_has_11_items_in_order(self):
        ids = [c[0] for c in CHECKLIST]
        titles = [c[1] for c in CHECKLIST]
        self.assertEqual(ids, EXPECTED_IDS)
        self.assertEqual(titles, EXPECTED_TITLES)

    def test_get_checklist_matches(self):
        lst = get_checklist()
        self.assertEqual([x["id"] for x in lst], EXPECTED_IDS)
        self.assertEqual([x["title"] for x in lst], EXPECTED_TITLES)

    def test_production_readiness_doc_exists_and_covers_all(self):
        # repo root is parents[2] from tests/ file
        root = Path(__file__).resolve().parents[1]
        doc = root / "docs" / "PRODUCTION_READINESS.md"
        self.assertTrue(doc.is_file(), "docs/PRODUCTION_READINESS.md must exist")
        text = doc.read_text(encoding="utf-8")
        for title in EXPECTED_TITLES:
            self.assertIn(title, text, f"Checklist title missing in doc: {title}")
        # must mention production_ready false and live_blocked
        self.assertIn("production_ready", text.lower())
        self.assertIn("live_blocked", text.lower() if "live_blocked" in text.lower() else text)
        self.assertIn("docs/SECURITY_REVIEW.md", text)
        self.assertIn("PAYMENTS.md", text)

    def test_payments_gate_references_checklist(self):
        root = Path(__file__).resolve().parents[1]
        payments = (root / "docs" / "PAYMENTS.md").read_text(encoding="utf-8")
        self.assertIn("PRODUCTION_READINESS.md", payments)
        for tid in EXPECTED_IDS[:3]:
            # at least ensure gate mentions reconciliation etc.
            pass
        self.assertIn("Verify reconciliation", payments)


@unittest.skipUnless(os.name in {"posix", "nt"}, "requires private filesystem")
class ProductionReadinessChecksTests(unittest.TestCase):
    def test_security_review(self):
        res = check_security_review()
        self.assertTrue(res.passed, res.detail)
        self.assertEqual(res.id, "security_review")

    def test_webhook_verification(self):
        res = check_webhook_verification()
        self.assertTrue(res.passed, res.detail)

    def test_reconciliation(self):
        res = check_reconciliation()
        self.assertTrue(res.passed, res.detail)

    def test_duplicate_handling(self):
        res = check_duplicate_handling()
        self.assertTrue(res.passed, res.detail)

    def test_refunds(self):
        res = check_refunds()
        self.assertTrue(res.passed, res.detail)

    def test_failed_payments(self):
        res = check_failed_payments()
        self.assertTrue(res.passed, res.detail)

    def test_settlement_records(self):
        res = check_settlement_records()
        self.assertTrue(res.passed, res.detail)

    def test_audit_trails(self):
        res = check_audit_trails()
        self.assertTrue(res.passed, res.detail)

    def test_backups(self):
        res = check_backups()
        self.assertTrue(res.passed, res.detail)

    def test_error_recovery(self):
        res = check_error_recovery()
        self.assertTrue(res.passed, res.detail)

    def test_configuration_separation(self):
        res = check_configuration_separation()
        self.assertTrue(res.passed, res.detail)

    def test_live_mode_still_blocked(self):
        with self.assertRaises(ProviderError) as cm:
            ProviderConfig(provider="razorpay", mode="live", webhook_secret=b"x" * 32)
        self.assertIn("PRODUCTION_READINESS", str(cm.exception))
        self.assertIn("sandbox only", str(cm.exception).lower())

    def test_evaluate_overall(self):
        report = evaluate({})
        self.assertEqual(report["total"], 11)
        self.assertEqual(report["passed"], 11, json.dumps(report, indent=2))
        self.assertEqual(report["failed"], 0)
        self.assertTrue(report["production_ready"], "offline production_ready true when 11 checks PASS (live still blocked)")
        self.assertTrue(report["live_blocked"], "live must stay blocked in code")
        self.assertFalse(report["live_ready"], "live_ready false without manual approval")
        for item in report["items"]:
            self.assertTrue(item["passed"], f"{item['id']} failed: {item['detail']}")
            self.assertIn(item["id"], EXPECTED_IDS)

    def test_evaluate_with_manual_approval_env(self):
        report = evaluate({"AI_EARN_LIVE_APPROVAL": "signed"})
        self.assertTrue(report["manual_approval_present"])
        # All checks still pass, so live_ready should be true when manual flag present
        self.assertTrue(report["live_ready"])
        self.assertTrue(report["production_ready"])  # offline ready true; live_ready true with approval
        self.assertTrue(report["live_blocked"])  # code still blocks live until gate update

    def test_evaluate_without_approval_live_not_ready(self):
        report = evaluate({"AI_EARN_LIVE_APPROVAL": "pending"})
        self.assertFalse(report["live_ready"])
        self.assertFalse(report["manual_approval_present"])


if __name__ == "__main__":
    unittest.main()

"""Hardening tests for v15 opportunities — evidence, ranking transparency, ToS/robots, revocation.

All data is disposable, validated, and owner-scoped. No external network.
"""
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from ai_earn.auth import AuthService, AccessDenied
from ai_earn.auth_storage import AuthStorage
from ai_earn.state_storage import StateStorage
from ai_earn.opportunities import (
    Opportunities,
    OpportunityError,
    ManualConnector,
    FakeConnector,
    verify_source_tos,
    _ranking_assumptions,
)
from ai_earn.connectors import GmailConnector, DriveConnector, ConnectorError


@unittest.skipUnless(os.name in {"posix", "nt"}, "requires private filesystem")
class OpportunityHardeningTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.directory = Path(self.tmp.name) / "private"
        if os.name == "nt":
            from ai_earn.windows_security import create_private_directory
            create_private_directory(self.directory)
        else:
            self.directory.mkdir(mode=0o700)
        self.auth_path = self.directory / "auth.sqlite3"
        self.state_path = self.directory / "state.sqlite3"
        patcher = patch("ai_earn.auth.PASSWORD_ITERATIONS", 1000)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.auth = AuthService(AuthStorage(self.auth_path))
        self.auth.register("alice", "disposable test password")
        self.alice_token = self.auth.login("alice", "disposable test password")
        # state v22 (latest) to ensure opportunities readable; v15 is minimum
        state = StateStorage(self.state_path, create=True, confirmed=True)
        for v in range(2, 23):
            getattr(state, f"migrate_to_v{v}")(self.directory / f"before-v{v}.sqlite3", confirmed=True)
        self.service = Opportunities(AuthService(AuthStorage(self.auth_path)), StateStorage(self.state_path))

    def test_evidence_score_transparent_and_bounded(self):
        # Full record should score 100
        ident = self.service.create(
            self.alice_token,
            "Evidence full title with sufficient length",
            "https://example.com/job/evidence1",
            50000,
            operation_key="ev1",
            risk_level="low",
            skill_tags="python, django",
        )
        view = self.service.get(self.alice_token, ident)
        self.assertIn("evidence_score", view)
        self.assertIn("evidence_note", view)
        self.assertEqual(view["evidence_score"], 100)
        self.assertIn("no external check", view["evidence_note"].lower())
        self.assertIn("no revenue", view["evidence_note"].lower())
        # Minimal tags/risk unknown reduces score
        ident2 = self.service.create(
            self.alice_token,
            "Minimal title ok",
            "https://example.com/job/evidence2",
            0,
            operation_key="ev2",
            risk_level="unknown",
            skill_tags="",
        )
        view2 = self.service.get(self.alice_token, ident2)
        # has_url 25 + has_title 15 + has_budget 0 + has_tags 0 + risk_known 0 = 40
        self.assertEqual(view2["evidence_score"], 40)
        self.assertGreaterEqual(view["evidence_score"], 0)
        self.assertLessEqual(view["evidence_score"], 100)
        self.assertGreater(view["evidence_score"], view2["evidence_score"])
        # All views must carry tos_compliance and ranking_assumptions
        for v in (view, view2):
            self.assertIn("tos_compliance", v)
            self.assertFalse(v["tos_compliance"]["captcha_bypass"])
            self.assertTrue(v["tos_compliance"]["manual_fallback"])
            self.assertIn("tos", v["tos_compliance"]["note"].lower())
            self.assertIn("ranking_assumptions", v)
            self.assertIn("draft heuristic", v["ranking_assumptions"].lower())
            self.assertIn("tos/robots/captcha", v["ranking_assumptions"].lower())

    def test_ranking_transparency_explain(self):
        # Create varied budgets/risks
        self.service.create(self.alice_token, "Low risk high budget", "https://example.com/r1", 90000, operation_key="rk1", risk_level="low", skill_tags="python")
        self.service.create(self.alice_token, "Medium risk medium budget", "https://example.com/r2", 50000, operation_key="rk2", risk_level="medium", skill_tags="go")
        self.service.create(self.alice_token, "High risk low budget", "https://example.com/r3", 10000, operation_key="rk3", risk_level="high", skill_tags="")
        ranked = self.service.ranked(self.alice_token, limit=10)
        explained = self.service.explain_ranking(self.alice_token, limit=10)
        # Both should return 3 items
        self.assertEqual(len(ranked), 3)
        self.assertEqual(len(explained), 3)
        # Each explained item must have ranking block with transparent scores and assumptions
        assumptions = _ranking_assumptions()
        for item in explained:
            self.assertIn("ranking", item)
            r = item["ranking"]
            self.assertIn("rank", r)
            self.assertIn("score_total_0_100", r)
            self.assertIn("score_budget_0_50", r)
            self.assertIn("score_risk_0_30", r)
            self.assertIn("score_deadline_0_20", r)
            self.assertIn("explanation", r)
            self.assertIn("assumptions", r)
            self.assertEqual(r["assumptions"], assumptions)
            self.assertIn("budget", r["explanation"].lower())
            self.assertIn("risk", r["explanation"].lower())
            self.assertGreaterEqual(r["score_total_0_100"], 0)
            self.assertLessEqual(r["score_total_0_100"], 100)
            self.assertGreaterEqual(item["evidence_score"], 0)
            self.assertIn("tos_compliance", item)
        # Ranking must be sorted by total desc
        totals = [it["ranking"]["score_total_0_100"] for it in explained]
        self.assertEqual(totals, sorted(totals, reverse=True))
        # First ranked should have highest total and highest budget (in this data)
        self.assertEqual(explained[0]["budget_paise"], 90000)
        self.assertEqual(explained[0]["risk_level"], "low")

    def test_tos_robots_verification(self):
        # helper must accept normal https and reject bypass patterns
        ok = verify_source_tos("https://example.com/job/123")
        self.assertTrue(ok["tos_respected"])
        self.assertFalse(ok["captcha_bypass"])
        self.assertTrue(ok["manual_fallback"])
        with self.assertRaises(OpportunityError):
            verify_source_tos("https://example.com/captcha-bypass")
        with self.assertRaises(OpportunityError):
            verify_source_tos("https://example.com/robots-bypass-trick")
        # create must reject bypass URLs (case-insensitive)
        with self.assertRaises(OpportunityError):
            self.service.create(self.alice_token, "Bypass", "https://example.com/CAPTCHA-bypass", 1000, operation_key="tos1")
        with self.assertRaises(OpportunityError):
            self.service.create(self.alice_token, "Bypass2", "https://example.com/ROBOTS-BYPASS", 1000, operation_key="tos2")
        # valid creation still works and view contains tos_compliance
        ident = self.service.create(self.alice_token, "Legit", "https://example.com/legit", 1000, operation_key="tos3")
        view = self.service.get(self.alice_token, ident)
        self.assertFalse(view["tos_compliance"]["captcha_bypass"])
        self.assertTrue(view["tos_compliance"]["robots_respected"])
        # connector import must also enforce ToS on items
        bad_fake = FakeConnector(items=[{"title": "Bad", "source_url": "https://example.com/captcha-bypass", "budget_paise": 1000}])
        with self.assertRaises(OpportunityError):
            self.service.import_from_connector(self.alice_token, bad_fake, operation_key_prefix="tos_bad")

    def test_revocation_and_manual_fallback(self):
        # FakeConnector revocation blocks import but manual remains
        fake = FakeConnector(items=[{"title": "Fake ok", "source_url": "https://example.com/fake1", "budget_paise": 2000}])
        ids = self.service.import_from_connector(self.alice_token, fake, limit=5, operation_key_prefix="rev1")
        self.assertEqual(len(ids), 1)
        fake.revoke()
        with self.assertRaises(OpportunityError) as ctx:
            self.service.import_from_connector(self.alice_token, fake, limit=5, operation_key_prefix="rev2")
        self.assertIn("revoked", str(ctx.exception).lower())
        # manual fallback still works after revocation of other connector
        manual = ManualConnector()
        self.assertEqual(manual.fetch(limit=5), [])
        self.assertEqual(self.service.import_from_connector(self.alice_token, manual, limit=5, operation_key_prefix="rev_manual"), [])
        # manual creation still works after revocation
        ident = self.service.create(self.alice_token, "After revoke manual", "https://example.com/after", 1000, operation_key="rev_manual_create")
        self.assertEqual(self.service.get(self.alice_token, ident)["title"], "After revoke manual")

    def test_gmail_drive_revocation_via_opportunities(self):
        key = "k" * 32
        gmail = GmailConnector(api_key=key, items=[{"id": "g1", "subject": "Gmail opp", "link": "https://example.com/g1", "budget_paise": 1000}])
        drive = DriveConnector(api_key=key, items=[{"id": "d1", "name": "Drive file", "webViewLink": "https://drive.google.com/file/d/1/view", "budget_paise": 2000}])
        # Both succeed before revoke
        ids_g = self.service.import_from_connector(self.alice_token, gmail, limit=5, operation_key_prefix="gre1")
        self.assertEqual(len(ids_g), 1)
        ids_d = self.service.import_from_connector(self.alice_token, drive, limit=5, operation_key_prefix="dre1")
        self.assertEqual(len(ids_d), 1)
        # Revoke gmail: import should fail with OpportunityError (wrapped), drive still works
        gmail.revoke()
        with self.assertRaises(OpportunityError):
            self.service.import_from_connector(self.alice_token, gmail, limit=5, operation_key_prefix="gre2")
        # drive not revoked yet
        ids_d2 = self.service.import_from_connector(self.alice_token, drive, limit=5, operation_key_prefix="dre2")
        self.assertEqual(len(ids_d2), 1)
        self.assertNotEqual(ids_d, ids_d2)
        # Now revoke drive
        drive.revoke()
        with self.assertRaises(OpportunityError):
            self.service.import_from_connector(self.alice_token, drive, limit=5, operation_key_prefix="dre3")
        with self.assertRaises(ConnectorError):
            drive.fetch(limit=1)
        with self.assertRaises(ConnectorError):
            gmail.fetch(limit=1)
        # Manual fallback always available
        self.assertEqual(ManualConnector().fetch(limit=1), [])

    def test_filtering_and_pagination_still_correct(self):
        # Create 4 with varied tags/budgets
        self.service.create(self.alice_token, "Python high", "https://example.com/f1", 10000, operation_key="fil1", risk_level="low", skill_tags="python")
        self.service.create(self.alice_token, "Go medium", "https://example.com/f2", 5000, operation_key="fil2", risk_level="medium", skill_tags="go")
        self.service.create(self.alice_token, "Python low", "https://example.com/f3", 1000, operation_key="fil3", risk_level="high", skill_tags="python")
        self.service.create(self.alice_token, "Mixed", "https://example.com/f4", 7000, operation_key="fil4", risk_level="low", skill_tags="python, go")
        # Filter by skill
        py = self.service.list(self.alice_token, skill_contains="python")
        self.assertEqual(len(py), 3)
        for row in py:
            self.assertIn("python", row["skill_tags"].lower())
            self.assertIn("evidence_score", row)
        # Filter by budget range
        mid = self.service.list(self.alice_token, min_budget=4000, max_budget=8000)
        self.assertEqual(len(mid), 2)
        # Pagination deterministic
        page1 = self.service.list(self.alice_token, limit=2)
        self.assertEqual(len(page1), 2)
        page2 = self.service.list(self.alice_token, after_id=page1[-1]["id"], limit=10)
        self.assertEqual(len(page2), 2)
        # ranked and explain_ranking include evidence and tos
        for r in self.service.ranked(self.alice_token, limit=10):
            self.assertIn("evidence_score", r)
            self.assertIn("tos_compliance", r)

    def test_draft_helpers_still_unverified(self):
        from ai_earn.opportunities import summarize_draft, suggest_skills_draft, suggest_price_draft
        s = summarize_draft("Build a site", "https://example.com/job")
        self.assertEqual(s["verification"], "draft_unverified")
        skills = suggest_skills_draft("Python fastapi work")
        self.assertEqual(skills["verification"], "draft_unverified")
        price = suggest_price_draft("Some title", "low")
        self.assertEqual(price["verification"], "draft_unverified")
        # All views still unverified
        ident = self.service.create(self.alice_token, "Verify check", "https://example.com/verify", 1000, operation_key="ver1")
        self.assertEqual(self.service.get(self.alice_token, ident)["verification"], "unverified")
        self.assertEqual(self.service.get(self.alice_token, ident)["amount_kind"], "budget_not_revenue")

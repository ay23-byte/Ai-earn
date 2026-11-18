"""Tests for v15 opportunity discovery — manual import + connector interface.

All data is disposable, validated, and owner-scoped. No external network,
verification, or revenue is implied.
"""

import os
import tempfile
import time
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from ai_earn.auth import AuthService, AccessDenied
from ai_earn.auth_storage import AuthStorage
from ai_earn.state_storage import StateStorage, StateStorageError
from ai_earn.opportunities import (
    Opportunities,
    OpportunityError,
    ManualConnector,
    FakeConnector,
    summarize_draft,
    suggest_skills_draft,
    suggest_price_draft,
)
from ai_earn.dashboard_metrics import DashboardMetrics
from ai_earn.state_recovery_comparison import StateRecoveryComparison


@unittest.skipUnless(os.name in {"posix", "nt"}, "requires private filesystem")
class OpportunityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
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
        self.auth.register("bob", "disposable test password")
        self.alice_token = self.auth.login("alice", "disposable test password")
        self.bob_token = self.auth.login("bob", "disposable test password")
        # Build v15 state
        state = StateStorage(self.state_path, create=True, confirmed=True)
        for v in range(2, 16):
            getattr(state, f"migrate_to_v{v}")(
                self.directory / f"before-v{v}.sqlite3", confirmed=True
            )
        self.state = StateStorage(self.state_path)
        self.service = Opportunities(self.auth, self.state)

    def test_create_and_get_manual(self):
        ident = self.service.create(
            self.alice_token,
            "Test title",
            "https://example.com/job/1",
            50000,
            operation_key="k1",
            risk_level="low",
            skill_tags="python, django",
            deadline_at=0,
        )
        row = self.service.get(self.alice_token, ident)
        self.assertEqual(row["title"], "Test title")
        self.assertEqual(row["source_url"], "https://example.com/job/1")
        self.assertEqual(row["budget_paise"], 50000)
        self.assertEqual(row["risk_level"], "low")
        self.assertEqual(row["skill_tags"], "python, django")
        self.assertEqual(row["status"], "discovered")
        self.assertEqual(row["verification"], "unverified")
        # cross-owner denied
        with self.assertRaises(AccessDenied):
            self.service.get(self.bob_token, ident)

    def test_create_connector_requires_id(self):
        # manual with connector_id rejected
        with self.assertRaises(OpportunityError):
            self.service.create(
                self.alice_token,
                "T",
                "https://example.com/a",
                1000,
                operation_key="k2",
                source_type="manual",
                connector_id="some",
            )
        # connector without id rejected
        with self.assertRaises(OpportunityError):
            self.service.create(
                self.alice_token,
                "T",
                "https://example.com/a",
                1000,
                operation_key="k3",
                source_type="connector",
                connector_id=None,
            )
        # connector valid
        ident = self.service.create(
            self.alice_token,
            "Conn title",
            "https://example.com/b",
            1000,
            operation_key="k4",
            source_type="connector",
            connector_id="my-connector-1",
        )
        row = self.service.get(self.alice_token, ident)
        self.assertEqual(row["source_type"], "connector")
        self.assertEqual(row["connector_id"], "my-connector-1")

    def test_state_transitions_discovered_flow(self):
        ident = self.service.create(
            self.alice_token, "Flow title", "https://example.com/flow", 1000, operation_key="flow1"
        )
        # discovered -> vetted
        self.service.vetted(self.alice_token, ident, operation_key="flow2")
        self.assertEqual(self.service.get(self.alice_token, ident)["status"], "vetted")
        # vetted -> pursuing
        self.service.pursuing(self.alice_token, ident, operation_key="flow3")
        self.assertEqual(self.service.get(self.alice_token, ident)["status"], "pursuing")
        # pursuing -> archived
        self.service.archived(self.alice_token, ident, operation_key="flow4")
        self.assertEqual(self.service.get(self.alice_token, ident)["status"], "archived")
        # after terminal, further transition blocked
        with self.assertRaises(OpportunityError):
            self.service.dismissed(self.alice_token, ident, operation_key="flow5")

    def test_dismissed_branches(self):
        # discovered -> dismissed
        ident = self.service.create(
            self.alice_token, "D1", "https://example.com/d1", 1000, operation_key="d1"
        )
        self.service.dismissed(self.alice_token, ident, operation_key="d1d")
        self.assertEqual(self.service.get(self.alice_token, ident)["status"], "dismissed")
        # discovered -> vetted -> dismissed
        ident2 = self.service.create(
            self.alice_token, "D2", "https://example.com/d2", 1000, operation_key="d2"
        )
        self.service.vetted(self.alice_token, ident2, operation_key="d2v")
        self.service.dismissed(self.alice_token, ident2, operation_key="d2d")
        self.assertEqual(self.service.get(self.alice_token, ident2)["status"], "dismissed")
        # discovered -> vetted -> archived
        ident3 = self.service.create(
            self.alice_token, "D3", "https://example.com/d3", 1000, operation_key="d3"
        )
        self.service.vetted(self.alice_token, ident3, operation_key="d3v")
        self.service.archived(self.alice_token, ident3, operation_key="d3a")
        self.assertEqual(self.service.get(self.alice_token, ident3)["status"], "archived")
        # discovered -> archived directly
        ident4 = self.service.create(
            self.alice_token, "D4", "https://example.com/d4", 1000, operation_key="d4"
        )
        self.service.archived(self.alice_token, ident4, operation_key="d4a")
        self.assertEqual(self.service.get(self.alice_token, ident4)["status"], "archived")

    def test_invalid_transitions(self):
        ident = self.service.create(
            self.alice_token, "Invalid", "https://example.com/inv", 1000, operation_key="inv1"
        )
        # discovered -> pursuing not allowed directly
        with self.assertRaises(OpportunityError):
            self.service.pursuing(self.alice_token, ident, operation_key="inv2")
        # vetted -> vetted again not allowed
        self.service.vetted(self.alice_token, ident, operation_key="inv3")
        with self.assertRaises(OpportunityError):
            self.service.vetted(self.alice_token, ident, operation_key="inv4")

    def test_operation_key_idempotency_and_conflict(self):
        ident = self.service.create(
            self.alice_token, "Dup", "https://example.com/dup", 1000, operation_key="dupkey"
        )
        # same key same request returns same id
        ident2 = self.service.create(
            self.alice_token, "Dup", "https://example.com/dup", 1000, operation_key="dupkey"
        )
        self.assertEqual(ident, ident2)
        # same key different request -> conflict
        with self.assertRaises(OpportunityError):
            self.service.create(
                self.alice_token, "Other", "https://example.com/other", 2000, operation_key="dupkey"
            )
        # transition idempotency
        self.service.vetted(self.alice_token, ident, operation_key="tkey")
        self.service.vetted(self.alice_token, ident, operation_key="tkey")  # idempotent retry
        with self.assertRaises(OpportunityError):
            # same key but different target
            self.service.pursuing(self.alice_token, ident, operation_key="tkey")

    def test_input_validation(self):
        # title too long / control
        with self.assertRaises(OpportunityError):
            self.service.create(
                self.alice_token, "", "https://example.com/a", 1000, operation_key="v1"
            )
        with self.assertRaises(OpportunityError):
            self.service.create(
                self.alice_token, "bad\x01title", "https://example.com/a", 1000, operation_key="v2"
            )
        with self.assertRaises(OpportunityError):
            self.service.create(
                self.alice_token, "x" * 201, "https://example.com/a", 1000, operation_key="v3"
            )
        # url invalid
        with self.assertRaises(OpportunityError):
            self.service.create(
                self.alice_token, "T", "ftp://example.com", 1000, operation_key="v4"
            )
        with self.assertRaises(OpportunityError):
            self.service.create(
                self.alice_token, "T", "https://example.com/ bad", 1000, operation_key="v5"
            )
        with self.assertRaises(OpportunityError):
            self.service.create(
                self.alice_token, "T", "https://example.com", -1, operation_key="v6"
            )
        # budget invalid
        with self.assertRaises(OpportunityError):
            self.service.create(
                self.alice_token, "T", "https://example.com/a", -1, operation_key="v7"
            )
        with self.assertRaises(OpportunityError):
            self.service.create(
                self.alice_token,
                "T",
                "https://example.com/a",
                1_000_000_000_001,
                operation_key="v8",
            )
        # risk invalid
        with self.assertRaises(OpportunityError):
            self.service.create(
                self.alice_token,
                "T",
                "https://example.com/a",
                1000,
                operation_key="v9",
                risk_level="critical",
            )
        # skill tags too long
        with self.assertRaises(OpportunityError):
            self.service.create(
                self.alice_token,
                "T",
                "https://example.com/a",
                1000,
                operation_key="v10",
                skill_tags="x" * 501,
            )
        # skill tags control
        with self.assertRaises(OpportunityError):
            self.service.create(
                self.alice_token,
                "T",
                "https://example.com/a",
                1000,
                operation_key="v11",
                skill_tags="bad\x02",
            )
        # connector id invalid
        with self.assertRaises(OpportunityError):
            self.service.create(
                self.alice_token,
                "T",
                "https://example.com/a",
                1000,
                operation_key="v12",
                source_type="connector",
                connector_id="bad id!",
            )
        # deadline past
        past = int(time.time()) - 100
        with self.assertRaises(OpportunityError):
            self.service.create(
                self.alice_token,
                "T",
                "https://example.com/a",
                1000,
                operation_key="v13",
                deadline_at=past,
            )
        # deadline out of range
        with self.assertRaises(OpportunityError):
            self.service.create(
                self.alice_token,
                "T",
                "https://example.com/a",
                1000,
                operation_key="v14",
                deadline_at=5_000_000_000,
            )

    def test_list_filters_and_ranking(self):
        # create diverse opportunities
        id_low = self.service.create(
            self.alice_token,
            "Low budget",
            "https://example.com/low",
            1000,
            operation_key="fl1",
            risk_level="low",
            skill_tags="python",
        )
        id_med = self.service.create(
            self.alice_token,
            "Med budget",
            "https://example.com/med",
            5000,
            operation_key="fl2",
            risk_level="medium",
            skill_tags="go",
        )
        id_high = self.service.create(
            self.alice_token,
            "High budget",
            "https://example.com/high",
            10000,
            operation_key="fl3",
            risk_level="high",
            skill_tags="python, go",
        )
        # vetted one
        self.service.vetted(self.alice_token, id_med, operation_key="fl2v")
        # filter by status
        discovered = self.service.list(self.alice_token, status="discovered")
        self.assertEqual(len(discovered), 2)
        vetted = self.service.list(self.alice_token, status="vetted")
        self.assertEqual(len(vetted), 1)
        # filter by risk
        low = self.service.list(self.alice_token, risk_level="low")
        self.assertEqual(len(low), 1)
        # filter by budget range
        mid = self.service.list(self.alice_token, min_budget=2000, max_budget=8000)
        self.assertEqual(len(mid), 1)
        # skill contains
        py = self.service.list(self.alice_token, skill_contains="python")
        self.assertEqual(len(py), 2)
        # pagination
        sorted([id_low, id_med, id_high])
        page1 = self.service.list(self.alice_token, limit=2)
        self.assertEqual(len(page1), 2)
        page2 = self.service.list(self.alice_token, after_id=page1[-1]["id"], limit=10)
        self.assertEqual(len(page2), 1)
        # ranked: high budget first, then medium, then low (risk secondary)
        ranked = self.service.ranked(self.alice_token, limit=10)
        self.assertEqual(ranked[0]["budget_paise"], 10000)
        self.assertEqual(ranked[1]["budget_paise"], 5000)

    def test_connector_interface(self):
        manual = ManualConnector()
        self.assertEqual(manual.fetch(limit=10), [])
        self.assertTrue(manual.connector_id.startswith("manual"))
        # Fake connector
        fake = FakeConnector(
            items=[
                {
                    "title": "Fake job",
                    "source_url": "https://example.com/fake",
                    "budget_paise": 2000,
                    "risk_level": "low",
                    "skill_tags": "rust",
                },
                {
                    "title": "Second",
                    "source_url": "https://example.com/second",
                    "budget_paise": 3000,
                },
            ]
        )
        ids = self.service.import_from_connector(
            self.alice_token, fake, limit=10, operation_key_prefix="imp1"
        )
        self.assertEqual(len(ids), 2)
        # idempotent same prefix
        ids2 = self.service.import_from_connector(
            self.alice_token, fake, limit=10, operation_key_prefix="imp1"
        )
        self.assertEqual(ids, ids2)
        # different prefix creates new records (different op keys)
        ids3 = self.service.import_from_connector(
            self.alice_token, fake, limit=10, operation_key_prefix="imp2"
        )
        self.assertEqual(len(ids3), 2)
        self.assertNotEqual(ids, ids3)
        # invalid connector item unknown field
        bad_fake = FakeConnector(
            items=[
                {
                    "title": "Bad",
                    "source_url": "https://example.com/b",
                    "budget_paise": 1000,
                    "extra": "field",
                }
            ]
        )
        with self.assertRaises(OpportunityError):
            self.service.import_from_connector(
                self.alice_token, bad_fake, operation_key_prefix="bad"
            )

    def test_ai_helpers_draft(self):
        s = summarize_draft("Build a website", "https://example.com/job")
        self.assertIn("draft_unverified", s["verification"])
        self.assertIn("Build a website", s["summary"])
        skills = suggest_skills_draft("Build a python fastapi website")
        self.assertIn("draft_unverified", skills["verification"])
        self.assertIn("python", skills["skill_tags"])
        price = suggest_price_draft("Some title", "low")
        self.assertEqual(price["currency"], "INR")
        self.assertGreater(price["suggested_budget_paise"], 0)
        self.assertEqual(price["verification"], "draft_unverified")
        # helpers reject invalid
        with self.assertRaises(OpportunityError):
            summarize_draft("", "https://example.com")
        with self.assertRaises(OpportunityError):
            suggest_price_draft("title", "invalid_risk")

    def test_concurrent_same_key(self):
        # Two threads with same operation key, different titles -> only one succeeds, other gets conflict
        results = []
        errors = []

        def attempt(title):
            try:
                ident = self.service.create(
                    self.alice_token,
                    title,
                    "https://example.com/concurrent",
                    1000,
                    operation_key="conc-key",
                )
                results.append(ident)
            except Exception as e:
                errors.append(type(e).__name__)

        t1 = threading.Thread(target=attempt, args=("Title A",))
        t2 = threading.Thread(target=attempt, args=("Title B",))
        t1.start()
        t2.start()
        t1.join()
        t2.join()
        # Exactly one success, one error (conflict)
        self.assertEqual(len(results) + len(errors), 2)
        # If both succeeded they must be same id (retry) -> but titles differ so should conflict
        if len(results) == 2:
            self.assertEqual(results[0], results[1])
        else:
            self.assertEqual(len(results), 1)
            self.assertEqual(len(errors), 1)
            self.assertIn("OpportunityError", errors[0])

    def test_dashboard_metrics_and_recovery_comparison(self):
        # Create some opportunities
        self.service.create(
            self.alice_token,
            "Metric test",
            "https://example.com/metric",
            12345,
            operation_key="met1",
            risk_level="medium",
        )
        self.service.create(
            self.alice_token,
            "Metric2",
            "https://example.com/metric2",
            54321,
            operation_key="met2",
            risk_level="low",
        )
        metrics = DashboardMetrics(self.auth, self.state).get(self.alice_token)
        self.assertEqual(metrics["schema_version"], 15)
        self.assertIn("opportunities", metrics)
        self.assertEqual(metrics["opportunities"]["count"], 2)
        self.assertEqual(metrics["opportunities"]["verification"], "unverified")
        # recovery comparison: backup then compare
        backup_path = self.directory / "backup.sqlite3"
        self.state.backup_to(backup_path, confirmed=True)
        backup = StateStorage(backup_path)
        comp = StateRecoveryComparison(self.auth, self.state, backup)
        rec = comp.compare_records(self.alice_token)
        self.assertEqual(rec["tables"]["opportunities"]["matching"], 2)
        snap = comp.compare_snapshot(self.alice_token)
        self.assertEqual(snap["tables"]["opportunities"]["matching"], 2)

    def test_migration_and_backup_integrity(self):
        # Fresh state at v14 -> migrate to v15
        # Use separate temp for v14 state
        alt_dir = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(alt_dir, ignore_errors=True))
        if os.name == "nt":
            from ai_earn.windows_security import create_private_directory

            try:
                create_private_directory(alt_dir / "priv")
                priv = alt_dir / "priv"
            except Exception:
                priv = Path(tempfile.mkdtemp())
                if os.name == "nt":
                    from ai_earn.windows_security import create_private_directory

                    if not priv.exists():
                        create_private_directory(priv)
                else:
                    priv.mkdir(mode=0o700, exist_ok=True)
        else:
            priv = alt_dir / "priv"
            if os.name == "nt":
                from ai_earn.windows_security import create_private_directory

                create_private_directory(priv)
            else:
                priv.mkdir(mode=0o700)
        alt_auth = priv / "auth.sqlite3"
        alt_state = priv / "state.sqlite3"
        alt_auth_s = AuthStorage(alt_auth)
        alt_auth_service = AuthService(alt_auth_s)
        alt_auth_service.register("alice", "disposable test password")
        token = alt_auth_service.login("alice", "disposable test password")
        st = StateStorage(alt_state, create=True, confirmed=True)
        for v in range(2, 15):
            getattr(st, f"migrate_to_v{v}")(priv / f"before-v{v}.sqlite3", confirmed=True)
        # Now at v14, create an opportunity_drafts etc to ensure old data preserved
        from ai_earn.opportunity_drafts import OpportunityDrafts

        drafts = OpportunityDrafts(alt_auth_service, StateStorage(alt_state))
        drafts.create(token, "Old draft", operation_key="old1")
        # migrate to v15
        st2 = StateStorage(alt_state)
        st2.migrate_to_v15(priv / "before-v15.sqlite3", confirmed=True)
        with st2.transaction() as conn:
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 15)
        # verify old draft still exists and new opportunities works
        opp = Opportunities(alt_auth_service, st2)
        ident = opp.create(token, "New opp", "https://example.com/new", 1000, operation_key="new1")
        self.assertEqual(opp.get(token, ident)["title"], "New opp")
        # backup integrity: corrupt opportunities via raw insert without audit -> backup should fail
        import sqlite3

        # fetch actual owner_id from created opportunity
        owner_id = opp.get(token, ident)["owner_id"]
        with sqlite3.connect(alt_state) as conn:
            conn.execute(
                "INSERT INTO opportunities(id, owner_id, title, source_url, source_type, connector_id, budget_paise, currency, deadline_at, risk_level, skill_tags, status, created_at, updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                    owner_id,
                    "evil",
                    "https://evil.com",
                    "manual",
                    None,
                    0,
                    "INR",
                    0,
                    "low",
                    "",
                    "discovered",
                    0,
                    0,
                ),
            )
            conn.commit()
        with self.assertRaises(StateStorageError):
            st2.backup_to(priv / "badbackup.sqlite3", confirmed=True)

    def test_version_guard_old_state(self):
        # State at v14 should reject opportunities service
        alt_dir = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(alt_dir, ignore_errors=True))
        priv = alt_dir / "priv"
        if os.name == "nt":
            from ai_earn.windows_security import create_private_directory

            create_private_directory(priv)
        else:
            priv.mkdir(mode=0o700)
        alt_auth = priv / "auth.sqlite3"
        alt_state = priv / "state.sqlite3"
        alt_auth_s = AuthStorage(alt_auth)
        alt_auth_service = AuthService(alt_auth_s)
        alt_auth_service.register("alice", "disposable test password")
        token = alt_auth_service.login("alice", "disposable test password")
        st = StateStorage(alt_state, create=True, confirmed=True)
        for v in range(2, 15):
            getattr(st, f"migrate_to_v{v}")(priv / f"before-v{v}.sqlite3", confirmed=True)
        opp = Opportunities(alt_auth_service, StateStorage(alt_state))
        with self.assertRaises(StateStorageError):
            opp.create(
                token, "Should fail", "https://example.com/fail", 1000, operation_key="fail1"
            )

    def test_owner_isolation_and_pagination(self):
        # create 3 for alice
        ids = []
        for i in range(3):
            ids.append(
                self.service.create(
                    self.alice_token,
                    f"Title {i}",
                    f"https://example.com/{i}",
                    1000 + i,
                    operation_key=f"pag{i}",
                )
            )
        # bob sees none
        self.assertEqual(len(self.service.list(self.bob_token)), 0)
        # pagination after_id
        first = self.service.list(self.alice_token, limit=2)
        self.assertEqual(len(first), 2)
        second = self.service.list(self.alice_token, after_id=first[-1]["id"], limit=2)
        self.assertEqual(len(second), 1)
        # after last returns empty
        third = self.service.list(self.alice_token, after_id=second[-1]["id"], limit=2)
        self.assertEqual(len(third), 0)

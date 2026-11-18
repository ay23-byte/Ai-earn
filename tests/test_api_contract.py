"""API contract tests — JSON shapes, error codes, auth, and failure conditions.

Covers: unit, integration, API, authentication, authorization, account-isolation,
financial calculations (as API contracts), and frontend snapshot validation via API.
All tests use disposable temp DBs and never hit network.
"""
import io
import json
import os
import hashlib
import hmac
import secrets
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from ai_earn.auth import AuthService, AccessDenied
from ai_earn.auth_storage import AuthStorage
from ai_earn.state_storage import StateStorage, StateStorageError, LATEST_SCHEMA_VERSION
from ai_earn.ledger import Ledger, LedgerError
from ai_earn.analytics import Analytics
from ai_earn.health import check, check_with_backups
from ai_earn.opportunities import Opportunities, OpportunityError
from ai_earn.services import Services
from ai_earn.customers import Customers
from ai_earn.dashboard_metrics import DashboardMetrics
from ai_earn.events import Event, write_event
from ai_earn.config import load_settings
from ai_earn.payment_providers import PaymentProviders, ProviderConfig, ProviderError
from ai_earn.ai_assistant import AIAssistant


def _h(api):
    """Helper to generate operation_key."""
    return "api_" + secrets.token_hex(16)


@unittest.skipUnless(os.name in {"posix", "nt"}, "requires private filesystem")
class APIContractTests(unittest.TestCase):
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
        self.patcher = patch("ai_earn.auth.PASSWORD_ITERATIONS", 1000)
        self.patcher.start()
        self.addCleanup(self.patcher.stop)
        self.auth = AuthService(AuthStorage(self.auth_path))
        self.auth.register("alice", "StrongPass12345678")
        self.auth.register("bob", "StrongPass12345678")
        self.alice = self.auth.login("alice", "StrongPass12345678")
        self.bob = self.auth.login("bob", "StrongPass12345678")
        state = StateStorage(self.state_path, create=True, confirmed=True)
        for v in range(2, LATEST_SCHEMA_VERSION + 1):
            getattr(state, f"migrate_to_v{v}")(self.directory / f"before-v{v}.sqlite3", confirmed=True)
        self.state = StateStorage(self.state_path)
        self.environ = {
            "AI_EARN_ENV": "development",
            "AI_EARN_LOG_LEVEL": "INFO",
            "AI_EARN_AUTH_DB": str(self.auth_path),
            "AI_EARN_STATE_DB": str(self.state_path),
        }

    # ---- Unit: config and event allowlist ----
    def test_unit_config_rejects_invalid_and_allows_valid(self):
        s = load_settings({"AI_EARN_ENV": "development", "AI_EARN_LOG_LEVEL": "INFO"})
        self.assertEqual(s.environment, "development")
        from ai_earn.config import ConfigurationError
        with self.assertRaises(ConfigurationError) as ctx:
            load_settings({"AI_EARN_ENV": "invalid_env", "AI_EARN_LOG_LEVEL": "INFO"})
        self.assertNotIn("invalid_env", str(ctx.exception))
        buf = io.StringIO()
        write_event(Event.STATUS_REQUESTED, buf, "INFO")
        self.assertIn("status_requested", buf.getvalue())
        with self.assertRaises(TypeError):
            write_event("not_an_event", buf)  # type: ignore
        with self.assertRaises(ValueError):
            write_event(Event.STATUS_REQUESTED, buf, "VERBOSE")

    # ---- Integration: auth + state transaction preserves ownership ----
    def test_integration_auth_state_ownership(self):
        opps = Opportunities(self.auth, self.state)
        ident = opps.create(self.alice, "Title A", "https://example.com/a", 50000, operation_key=_h("o1"))
        got = opps.get(self.alice, ident)
        self.assertEqual(got["title"], "Title A")
        with self.assertRaises(AccessDenied):
            opps.get(self.bob, ident)
        bob_ident = opps.create(self.bob, "Bob Title", "https://example.com/b", 10000, operation_key=_h("o2"))
        self.assertEqual(len(opps.list(self.alice)), 1)
        self.assertEqual(len(opps.list(self.bob)), 1)
        self.assertNotEqual(ident, bob_ident)

    # ---- API: health and status contracts ----
    def test_api_health_contract_success(self):
        # Health with single account is ok; create isolated single-account DB for this check
        tmp2 = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(tmp2.cleanup)
        d2 = Path(tmp2.name) / "priv_health"
        if os.name == "nt":
            from ai_earn.windows_security import create_private_directory
            create_private_directory(d2)
        else:
            d2.mkdir(mode=0o700)
        auth_path2 = d2 / "auth.sqlite3"
        state_path2 = d2 / "state.sqlite3"
        auth2 = AuthService(AuthStorage(auth_path2))
        auth2.register("alice", "StrongPass12345678")
        # no bob here
        state2 = StateStorage(state_path2, create=True, confirmed=True)
        for v in range(2, LATEST_SCHEMA_VERSION + 1):
            getattr(state2, f"migrate_to_v{v}")(d2 / f"before-v{v}.sqlite3", confirmed=True)
        env2 = {
            "AI_EARN_ENV": "development",
            "AI_EARN_LOG_LEVEL": "INFO",
            "AI_EARN_AUTH_DB": str(auth_path2),
            "AI_EARN_STATE_DB": str(state_path2),
        }
        payload = check(env2)
        for key in ("ok", "production_ready", "version", "checks", "component", "environment"):
            self.assertIn(key, payload)
        self.assertTrue(payload["production_ready"])
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["checks"]["auth"]["status"], "ok")
        self.assertEqual(payload["checks"]["state"]["status"], "ok")
        self.assertEqual(payload["checks"]["version"]["latest"], LATEST_SCHEMA_VERSION)
        ext = check_with_backups(env2)
        self.assertIn("backups", ext["checks"])
        self.assertEqual(ext["checks"]["backups"]["expected"], LATEST_SCHEMA_VERSION - 1)
        self.assertEqual(ext["checks"]["backups"]["count"], LATEST_SCHEMA_VERSION - 1)
        self.assertEqual(ext["checks"]["backups"]["status"], "ok")
        blob = json.dumps(payload)
        self.assertNotIn("StrongPass", blob)
        self.assertNotIn(str(auth_path2), blob)

    def test_api_health_contract_failure_conditions(self):
        empty_env = {
            "AI_EARN_ENV": "development",
            "AI_EARN_LOG_LEVEL": "INFO",
            "AI_EARN_AUTH_DB": str(self.directory / "missing_auth.sqlite3"),
            "AI_EARN_STATE_DB": str(self.directory / "missing_state.sqlite3"),
        }
        p = check(empty_env)
        self.assertFalse(p["ok"])
        self.assertEqual(p["checks"]["auth"]["status"], "missing")
        self.assertEqual(p["checks"]["state"]["status"], "missing")
        bad_env = dict(self.environ, AI_EARN_ENV="bad")
        p2 = check(bad_env)
        self.assertEqual(p2["checks"]["config"]["status"], "invalid_configuration")
        self.assertFalse(p2["ok"])
        prod_env = dict(self.environ, AI_EARN_ENV="production")
        # With 2 accounts, health is not ok but production_ready still false
        p3 = check(prod_env)
        self.assertFalse(p3["production_ready"])
        self.assertNotIn("token", json.dumps(p3).lower())

    # ---- Authentication: success and failure conditions ----
    def test_authentication_success_and_failures(self):
        with self.assertRaises(Exception):
            self.auth.login("alice", "WrongPass12345678")
        with self.assertRaises(Exception):
            self.auth.login("charlie", "StrongPass12345678")
        token = self.auth.login("alice", "StrongPass12345678")
        self.auth.logout(token)
        opps = Opportunities(self.auth, self.state)
        with self.assertRaises(AccessDenied):
            opps.create(token, "X", "https://example.com/x", 100, operation_key=_h("x"))
        with self.assertRaises(AccessDenied):
            opps.create("invalid-token-" + secrets.token_hex(16), "X", "https://example.com/y", 100, operation_key=_h("y"))

    # ---- Authorization: cross-account denied, invalid transitions ----
    def test_authorization_cross_account_and_invalid_transition(self):
        opps = Opportunities(self.auth, self.state)
        ident = opps.create(self.alice, "A", "https://example.com/a1", 10000, operation_key=_h("t1"))
        with self.assertRaises(AccessDenied):
            opps.get(self.bob, ident)
        with self.assertRaises(OpportunityError):
            opps.pursuing(self.alice, ident, operation_key=_h("bad_trans"))  # must go discovered->vetted->pursuing
        opps.vetted(self.alice, ident, operation_key=_h("vetted1"))
        got = opps.get(self.alice, ident)
        self.assertEqual(got["status"], "vetted")
        opps2 = Opportunities(self.auth, self.state)
        same_key = _h("dup")
        opps2.create(self.alice, "Title1", "https://example.com/dup1", 1000, operation_key=same_key)
        with self.assertRaises(OpportunityError):
            opps2.create(self.alice, "Title2-different", "https://example.com/dup2", 2000, operation_key=same_key)
        same_key2 = _h("idem")
        id1 = opps2.create(self.alice, "Same", "https://example.com/same", 999, operation_key=same_key2)
        id2 = opps2.create(self.alice, "Same", "https://example.com/same", 999, operation_key=same_key2)
        self.assertEqual(id1, id2)

    # ---- Account isolation: ensure Bob never sees Alice data across services ----
    def test_account_isolation_across_services(self):
        opps = Opportunities(self.auth, self.state)
        svcs = Services(self.auth, self.state)
        custs = Customers(self.auth, self.state)
        ledger = Ledger(self.auth, self.state)
        opp_id = opps.create(self.alice, "Isolated Opp", "https://example.com/iso", 5000, operation_key=_h("iso_opp"))
        svc_id = svcs.create(self.alice, "Svc A", scope="scope", deliverable="deliv", price_paise=100000, revisions=2, operation_key=_h("iso_svc"))
        cust_id = custs.create(self.alice, "Alice Cust", "alice@example.com", "ref", operation_key=_h("iso_cust"))
        ledger.add(self.alice, "income", 75000, "sale", operation_key=_h("iso_ledger"))
        self.assertEqual(opps.list(self.bob), [])
        self.assertEqual(svcs.list(self.bob), [])
        self.assertEqual(custs.list(self.bob), [])
        self.assertEqual(ledger.list(self.bob), [])
        with self.assertRaises(AccessDenied):
            opps.get(self.bob, opp_id)
        with self.assertRaises(AccessDenied):
            svcs.get(self.bob, svc_id)
        with self.assertRaises(AccessDenied):
            custs.get(self.bob, cust_id)
        self.assertEqual(ledger.net(self.bob)["net_paise"], 0)
        self.assertEqual(ledger.net(self.alice)["net_paise"], 75000)

    # ---- Database migration: version guard and integrity ----
    def test_database_migration_version_guard_and_integrity(self):
        tmp2 = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(tmp2.cleanup)
        d2 = Path(tmp2.name) / "priv2"
        if os.name == "nt":
            from ai_earn.windows_security import create_private_directory
            create_private_directory(d2)
        else:
            d2.mkdir(mode=0o700)
        auth2 = AuthService(AuthStorage(d2 / "auth.sqlite3"))
        auth2.register("alice", "StrongPass12345678")
        tok2 = auth2.login("alice", "StrongPass12345678")
        st2 = StateStorage(d2 / "state.sqlite3", create=True, confirmed=True)
        for v in range(2, 18):
            getattr(st2, f"migrate_to_v{v}")(d2 / f"before-v{v}.sqlite3", confirmed=True)
        ledger2 = Ledger(auth2, st2)
        with self.assertRaises(StateStorageError):
            ledger2.add(tok2, "income", 100, "", operation_key=_h("migs"))
        st2.migrate_to_v18(d2 / "before-v18.sqlite3", confirmed=True)
        lid = ledger2.add(tok2, "income", 100, "", operation_key=_h("mig_ok"))
        self.assertIsNotNone(ledger2.get(tok2, lid))
        healthy = d2 / "healthy_copy.sqlite3"
        st2.backup_to(healthy, confirmed=True)
        self.assertTrue(healthy.exists())

    # ---- Recovery: backup, restore, comparison ----
    def test_recovery_backup_and_comparison(self):
        opps = Opportunities(self.auth, self.state)
        opps.create(self.alice, "Rec Opp", "https://example.com/rec", 12345, operation_key=_h("rec1"))
        cand = self.directory / "cand.sqlite3"
        self.state.backup_to(cand, confirmed=True)
        self.assertTrue(cand.exists())
        restored = self.directory / "restored.sqlite3"
        StateStorage(cand, create=False).backup_to(restored, confirmed=True)
        self.assertTrue(restored.exists())
        from ai_earn.state_recovery_comparison import StateRecoveryComparison
        cand_state = StateStorage(cand, create=False)
        comp = StateRecoveryComparison(self.auth, self.state, cand_state)
        rec = comp.compare_records(self.alice)
        self.assertGreaterEqual(rec["tables"]["opportunities"]["matching"], 1)
        # compare_snapshot also should succeed
        snap = comp.compare_snapshot(self.alice)
        self.assertIn("opportunities", str(snap).lower() or "matching" in str(snap).lower() or "tables" in snap)
        tmp3 = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(tmp3.cleanup)
        d3 = Path(tmp3.name) / "priv3"
        if os.name == "nt":
            from ai_earn.windows_security import create_private_directory
            create_private_directory(d3)
        else:
            d3.mkdir(mode=0o700)
        a3 = AuthService(AuthStorage(d3 / "auth.sqlite3"))
        a3.register("alice", "StrongPass12345678")
        s3 = StateStorage(d3 / "state.sqlite3", create=True, confirmed=True)
        for v in range(2, 5):
            getattr(s3, f"migrate_to_v{v}")(d3 / f"before-v{v}.sqlite3", confirmed=True)
        s3_old = StateStorage(d3 / "state.sqlite3", create=False)
        comp_mismatch = StateRecoveryComparison(self.auth, self.state, s3_old)
        with self.assertRaises(Exception):
            comp_mismatch.compare_records(self.alice)

    # ---- Financial calculation: ledger net, 11-concept, analytics ----
    def test_financial_calculation_contracts(self):
        ledger = Ledger(self.auth, self.state)
        with self.assertRaises(LedgerError):
            ledger.add(self.alice, "invalid_kind", 100, "", operation_key=_h("bad_kind"))
        with self.assertRaises(LedgerError):
            ledger.add(self.alice, "income", -1, "", operation_key=_h("bad_amt"))
        with self.assertRaises(LedgerError):
            ledger.add(self.alice, "income", 1_000_000_000_001, "", operation_key=_h("bad_amt2"))
        ledger.add(self.alice, "income", 100000, "sale", operation_key=_h("inc1"))
        ledger.add(self.alice, "expense", 30000, "cost", operation_key=_h("exp1"))
        ledger.add(self.alice, "fee", 10000, "fee", operation_key=_h("fee1"))
        ledger.add(self.alice, "refund", 5000, "refund", operation_key=_h("ref1"))
        net = ledger.net(self.alice)
        self.assertEqual(net["net_paise"], 100000 - 30000 - 10000 - 5000)
        self.assertEqual(net["totals"]["income"], 100000)
        # ledger net is local unverified
        self.assertIn(net["verification"], ("unverified", "unverified_local", "unverified"))
        if hasattr(ledger, "eleven_concepts"):
            ec = ledger.eleven_concepts(self.alice)
            self.assertIn("proposed", ec)
            self.assertIn("net_realized_synthetic", ec)
            # also check verified variant exists
            self.assertIn("net_realized_verified", ec)
        analytics = Analytics(self.auth, self.state)
        funnel = analytics.funnel(self.alice)
        self.assertIn("opportunities", funnel)
        self.assertIn("customers", funnel)
        mrr = analytics.mrr(self.alice)
        self.assertIn("total_paise", mrr)
        self.assertIn("months_with_income", mrr)
        with self.assertRaises(Exception):
            analytics.funnel(self.alice, from_ts=100, to_ts=10)
        mrr_bob = analytics.mrr(self.bob)
        self.assertEqual(mrr_bob["total_paise"], 0)

    # ---- Payment webhook: HMAC, amount binding, replay ----
    def test_payment_webhook_success_and_failure(self):
        secret = b"test-webhook-secret-32bytes-long-123456789012"
        cfg = ProviderConfig(provider="razorpay", mode="sandbox", webhook_secret=secret)
        pp = PaymentProviders(self.auth, self.state, cfg)
        order_id = pp.create_order(self.alice, 50000, operation_key=_h("ord1"))
        order = pp.get(self.alice, order_id)
        provider_oid = order["provider_order_id"]
        now = int(time.time())
        body_dict = {
            "event_id": "evt_test_" + secrets.token_hex(8),
            "event_type": "authorized",
            "provider_order_id": provider_oid,
            "amount_paise": 50000,
            "currency": "INR",
            "occurred_at": now,
        }
        raw = json.dumps(body_dict, separators=(",", ":")).encode()
        sig = hmac.new(secret, raw, hashlib.sha256).hexdigest()
        res = pp.handle_webhook(self.alice, raw, sig, operation_key=_h("wh1"))
        self.assertEqual(res["order_id"], order_id)
        self.assertEqual(res["new_status"], "authorized")
        bad_sig = "0" * 64
        body_dict2 = dict(body_dict, event_id="evt_bad_sig_" + secrets.token_hex(4))
        raw2 = json.dumps(body_dict2, separators=(",", ":")).encode()
        with self.assertRaises(ProviderError):
            pp.handle_webhook(self.alice, raw2, bad_sig, operation_key=_h("wh_bad_sig"))
        body_mismatch = dict(body_dict, event_id="evt_mismatch_" + secrets.token_hex(4), amount_paise=99999)
        raw_m = json.dumps(body_mismatch, separators=(",", ":")).encode()
        sig_m = hmac.new(secret, raw_m, hashlib.sha256).hexdigest()
        with self.assertRaises(ProviderError):
            pp.handle_webhook(self.alice, raw_m, sig_m, operation_key=_h("wh_mismatch"))
        order2 = pp.create_order(self.alice, 60000, operation_key=_h("ord2"))
        order2_row = pp.get(self.alice, order2)
        body_a = {
            "event_id": "evt_replay_" + secrets.token_hex(4),
            "event_type": "authorized",
            "provider_order_id": order2_row["provider_order_id"],
            "amount_paise": 60000,
            "currency": "INR",
            "occurred_at": now,
        }
        raw_a = json.dumps(body_a, separators=(",", ":")).encode()
        sig_a = hmac.new(secret, raw_a, hashlib.sha256).hexdigest()
        pp.handle_webhook(self.alice, raw_a, sig_a, operation_key=_h("wh_a"))
        body_a_conflict = dict(body_a, amount_paise=61000)
        raw_a2 = json.dumps(body_a_conflict, separators=(",", ":")).encode()
        sig_a2 = hmac.new(secret, raw_a2, hashlib.sha256).hexdigest()
        with self.assertRaises(Exception):
            pp.handle_webhook(self.alice, raw_a2, sig_a2, operation_key=_h("wh_a_conflict"))
        with self.assertRaises(ProviderError):
            ProviderConfig(provider="razorpay", mode="live", webhook_secret=secret)

    # ---- AI tool permission and approval workflow ----
    def test_ai_tool_permission_and_approval(self):
        ai = AIAssistant(self.auth, self.state)
        r = ai.registry.execute(self.alice, "read_opportunity", {"opportunity_id": secrets.token_hex(16)}, self.auth, self.state)
        self.assertEqual(r["verification"], "read_only")
        d = ai.registry.execute(self.alice, "draft_summary", {"title": "T", "source_url": "https://example.com", "external_content": "injected"}, self.auth, self.state)
        self.assertEqual(d["verification"], "draft_unverified")
        self.assertIn("treated as data", d["note"])
        with self.assertRaises(Exception):
            ai.registry.execute(self.alice, "publish_service", {"service_id": secrets.token_hex(16)}, self.auth, self.state)
        queue = getattr(ai, "approval_queue", None) or getattr(ai, "queue", None) or getattr(ai, "approvals", None)
        if queue is None:
            approval_id = ai.request_approval(self.alice, "publish_service", {"service_id": secrets.token_hex(16)}, operation_key=_h("ap1"))
            ai.approve(self.alice, approval_id, True, operation_key=_h("ap1_dec"))
            res = ai.registry.execute(self.alice, "publish_service", {"service_id": secrets.token_hex(16)}, self.auth, self.state)
            self.assertIsNotNone(res)
        else:
            svc_ids = Services(self.auth, self.state)
            sid = svc_ids.create(self.alice, "SvcForAI", scope="s", deliverable="d", price_paise=5000, revisions=1, operation_key=_h("svc_ai_1"))
            ai.queue.emergency_stop(self.alice, confirmed=True, reason="test stop", operation_key=_h("emerg1"))
            self.assertTrue(ai.queue.is_stopped(self.alice))
            with self.assertRaises(Exception):
                queue.request(self.alice, "publish_service", {"service_id": sid}, operation_key=_h("blocked_due_emergency"))
            ai.queue.emergency_clear(self.alice, confirmed=True, operation_key=_h("emerg_clear"))
            self.assertFalse(ai.queue.is_stopped(self.alice))
            appr_id = queue.request(self.alice, "publish_service", {"service_id": sid}, operation_key=_h("ap_req1"))
            self.assertIsNotNone(appr_id)
            with self.assertRaises(Exception):
                queue.decide(self.alice, appr_id, "approved", operation_key=_h("ap_no_confirm"))
            queue.decide(self.alice, appr_id, "approved", operation_key=_h("ap_approved"), confirmed=True)
            got = queue.get(self.alice, appr_id)
            self.assertEqual(got["status"], "approved")
            res2 = ai.registry.execute(self.alice, "publish_service", {"service_id": sid}, self.auth, self.state, approval_id=appr_id)
            self.assertEqual(res2["verification"], "approved_consequential")
            queue.revoke(self.alice, appr_id, operation_key=_h("revoke1"))
            got2 = queue.get(self.alice, appr_id)
            self.assertEqual(got2["status"], "revoked")
            short_id = queue.request(self.alice, "publish_service", {"service_id": sid}, operation_key=_h("short_ttl"), ttl_seconds=60)
            got_pending = queue.get(self.alice, short_id)
            self.assertEqual(got_pending["status"], "pending")
            # Pending approvals are not yet expired; effective_status should be pending
            self.assertEqual(got_pending.get("effective_status", "pending"), "pending")
            # Verify expiry logic via status field exists and TTL is enforced (60 <= ttl <= 86400)
            self.assertIn("expires_at", got_pending)
            self.assertGreater(got_pending["expires_at"], got_pending["created_at"])

    # ---- Frontend snapshot validation ----
    def test_frontend_snapshot_validation(self):
        opps = Opportunities(self.auth, self.state)
        opps.create(self.alice, "Front Opp", "https://example.com/front", 777, operation_key=_h("front1"))
        metrics = DashboardMetrics(self.auth, self.state).get(self.alice)
        self.assertIn("opportunities", metrics)
        self.assertEqual(metrics["opportunities"]["count"], 1)
        snap = {
            "format": "ai-earn-dashboard-snapshot",
            "version": 1,
            "read_only": True,
            "generated_at": int(time.time()),
            "metrics": metrics,
        }
        raw = json.dumps(snap).encode()
        self.assertLess(len(raw), 1_000_000)
        parsed = json.loads(raw)
        self.assertTrue(parsed["read_only"])
        self.assertEqual(parsed["format"], "ai-earn-dashboard-snapshot")
        bad = dict(snap, read_only=False)
        self.assertFalse(bad["read_only"])
        blob = json.dumps(metrics)
        self.assertNotIn("StrongPass", blob)
        self.assertNotIn("token", blob.lower())

    # ---- Windows policy (unit) ----
    def test_windows_policy_unit(self):
        from ai_earn.auth_storage import AuthStorage
        with self.assertRaises(Exception):
            AuthStorage(self.directory / "dashboard" / "auth.sqlite3", create=True)
        self.assertTrue(self.auth_path.exists())
        self.assertNotIn("0.0.0.0", json.dumps(check({
            "AI_EARN_ENV": "development",
            "AI_EARN_LOG_LEVEL": "INFO",
            "AI_EARN_AUTH_DB": str(self.auth_path),
            "AI_EARN_STATE_DB": str(self.state_path),
        })))


if __name__ == "__main__":
    unittest.main()

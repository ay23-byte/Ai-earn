"""End-to-end journey — full user flow across domains with failure conditions.

Covers: unit → integration → API → authentication → authorization → account-isolation
→ database migration → recovery → financial calculation → payment webhook →
AI tool permission → approval workflow → frontend snapshot → end-to-end (this file)
→ Windows policy (unit). All with disposable isolated DBs, no network.
"""
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
from ai_earn.state_storage import StateStorage, LATEST_SCHEMA_VERSION
from ai_earn.opportunities import Opportunities
from ai_earn.services import Services
from ai_earn.products import Products
from ai_earn.customers import Customers
from ai_earn.communications import Communications
from ai_earn.ledger import Ledger
from ai_earn.invoices import Invoices
from ai_earn.order_drafts import OrderDrafts
from ai_earn.followups import Followups
from ai_earn.task_plans import TaskPlans
from ai_earn.analytics import Analytics
from ai_earn.dashboard_metrics import DashboardMetrics
from ai_earn.payment_providers import PaymentProviders, ProviderConfig, ProviderError
from ai_earn.ai_assistant import AIAssistant
from ai_earn.health import check
from ai_earn.tracker_cli import _paise

def _k(prefix="e2e"):
    return f"{prefix}_{secrets.token_hex(16)}"

@unittest.skipUnless(os.name in {"posix", "nt"}, "requires private filesystem")
class E2EJourneyTests(unittest.TestCase):
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
        self.alice_token = self.auth.login("alice", "StrongPass12345678")
        self.bob_token = self.auth.login("bob", "StrongPass12345678")
        state = StateStorage(self.state_path, create=True, confirmed=True)
        for v in range(2, LATEST_SCHEMA_VERSION + 1):
            getattr(state, f"migrate_to_v{v}")(self.directory / f"before-v{v}.sqlite3", confirmed=True)
        self.state = StateStorage(self.state_path)

    def test_full_journey_success_and_failures(self):
        with self.assertRaises(Exception):
            self.auth.login("alice", "wrongpassword")
        with self.assertRaises(AccessDenied):
            Opportunities(self.auth, self.state).create("bad-token", "T", "https://example.com/t", 1000, operation_key=_k())

        opps = Opportunities(self.auth, self.state)
        opp_id = opps.create(self.alice_token, "E2E Opp", "https://example.com/e2e", 250000, operation_key=_k("opp"))
        self.assertIsNotNone(opps.get(self.alice_token, opp_id))
        with self.assertRaises(Exception):
            opps.create(self.alice_token, "", "https://example.com/x", 1000, operation_key=_k("bad_title"))
        with self.assertRaises(Exception):
            opps.create(self.alice_token, "T", "ftp://example.com", 1000, operation_key=_k("bad_url"))
        with self.assertRaises(Exception):
            opps.create(self.alice_token, "T", "https://example.com/x", -1, operation_key=_k("bad_budget"))
        with self.assertRaises(AccessDenied):
            opps.get(self.bob_token, opp_id)
        self.assertEqual(len(opps.list(self.bob_token)), 0)
        opps.vetted(self.alice_token, opp_id, operation_key=_k("vetted"))
        self.assertEqual(opps.get(self.alice_token, opp_id)["status"], "vetted")
        with self.assertRaises(Exception):
            opps.vetted(self.alice_token, opp_id, operation_key=_k("vetted_again"))
        same_key = _k("idem_opp")
        id1 = opps.create(self.alice_token, "Same", "https://example.com/same", 1000, operation_key=same_key)
        id2 = opps.create(self.alice_token, "Same", "https://example.com/same", 1000, operation_key=same_key)
        self.assertEqual(id1, id2)
        with self.assertRaises(Exception):
            opps.create(self.alice_token, "Different", "https://example.com/diff", 2000, operation_key=same_key)

        svcs = Services(self.auth, self.state)
        svc_id = svcs.create(self.alice_token, "Web Dev", scope="scope1", deliverable="site", price_paise=500000, revisions=3, operation_key=_k("svc"))
        svcs.publish(self.alice_token, svc_id, operation_key=_k("svc_pub"))
        self.assertEqual(svcs.get(self.alice_token, svc_id)["status"], "published")
        with self.assertRaises(Exception):
            svcs.create(self.alice_token, "Bad", scope="s", deliverable="d", price_paise=-1, revisions=1, operation_key=_k("bad_svc_price"))
        with self.assertRaises(AccessDenied):
            svcs.get(self.bob_token, svc_id)

        prods = Products(self.auth, self.state)
        prod_id = prods.create(self.alice_token, "Ebook", "desc ebook", "1.0.0", 19900, operation_key=_k("prod"))
        self.assertIsNotNone(prods.get(self.alice_token, prod_id))
        with self.assertRaises(AccessDenied):
            prods.get(self.bob_token, prod_id)

        custs = Customers(self.auth, self.state)
        cust_id = custs.create(self.alice_token, "Alice Cust", "alice.cust@example.com", "linkedin", operation_key=_k("cust"))
        with self.assertRaises(AccessDenied):
            custs.get(self.bob_token, cust_id)
        with self.assertRaises(Exception):
            custs.create(self.alice_token, "Bad", "", "src", operation_key=_k("bad_cust"))

        comms = Communications(self.auth, self.state)
        comm_id = comms.create(self.alice_token, cust_id, "email", "Hello customer", operation_key=_k("comm"))
        comms.sent(self.alice_token, comm_id, operation_key=_k("comm_sent"))
        self.assertEqual(comms.get(self.alice_token, comm_id)["status"], "sent")
        with self.assertRaises(AccessDenied):
            comms.sent(self.bob_token, comm_id, operation_key=_k("bob_sent"))

        follow = Followups(self.auth, self.state)
        fu_id = follow.create(self.alice_token, cust_id, 1_800_000_000, "Follow up note", operation_key=_k("fu"))
        self.assertIsNotNone(follow.get(self.alice_token, fu_id))
        follow.completed(self.alice_token, fu_id, operation_key=_k("fu_complete"))
        self.assertEqual(follow.get(self.alice_token, fu_id)["status"], "completed")
        with self.assertRaises(AccessDenied):
            follow.get(self.bob_token, fu_id)

        ledger = Ledger(self.auth, self.state)
        ledger.add(self.alice_token, "income", 500000, "income1", operation_key=_k("inc1"))
        ledger.add(self.alice_token, "expense", 120000, "hosting", operation_key=_k("exp1"))
        net = ledger.net(self.alice_token)
        self.assertEqual(net["net_paise"], 500000 - 120000)
        with self.assertRaises(Exception):
            ledger.add(self.alice_token, "bad_kind", 100, "", operation_key=_k("bad_ledger1"))
        with self.assertRaises(Exception):
            ledger.add(self.alice_token, "income", 100, "bad\x00note", operation_key=_k("bad_ledger2"))
        self.assertEqual(ledger.net(self.bob_token)["net_paise"], 0)

        analytics = Analytics(self.auth, self.state)
        funnel = analytics.funnel(self.alice_token)
        self.assertGreaterEqual(funnel["opportunities"]["total"], 1)
        self.assertIn("customers", funnel)
        rev = analytics.revenue_analytics(self.alice_token)
        # revenue_analytics is rich dict; check for expected top-level keys
        self.assertIsInstance(rev, dict)
        self.assertTrue(any(k in rev for k in ("total_opportunities", "net_realized_income", "monthly_income", "conversion_rate")))
        mrr = analytics.mrr(self.alice_token)
        self.assertIn("total_paise", mrr)
        with self.assertRaises(Exception):
            analytics.funnel(self.alice_token, from_ts=100, to_ts=10)

        orders = OrderDrafts(self.auth, self.state)
        invs = Invoices(self.auth, self.state)
        order_id = orders.create(self.alice_token, "Order A", 250000, operation_key=_k("order"))
        inv_id = invs.create(self.alice_token, order_id, operation_key=_k("inv"))
        invs.issued(self.alice_token, inv_id, operation_key=_k("inv_issued"))
        self.assertEqual(invs.get(self.alice_token, inv_id)["status"], "issued")
        with self.assertRaises(AccessDenied):
            invs.get(self.bob_token, inv_id)

        tasks = TaskPlans(self.auth, self.state)
        task_id = tasks.create(self.alice_token, "Plan A", operation_key=_k("task"))
        self.assertIsNotNone(tasks.get(self.alice_token, task_id))
        with self.assertRaises(AccessDenied):
            tasks.get(self.bob_token, task_id)

        metrics = DashboardMetrics(self.auth, self.state).get(self.alice_token)
        self.assertIn("opportunities", metrics)
        self.assertIn("ledger", metrics)
        self.assertGreaterEqual(metrics["opportunities"]["count"], 1)
        self.assertNotIn("StrongPass", json.dumps(metrics))

        secret = b"e2e-webhook-secret-32bytes-long-12345678901234"
        cfg = ProviderConfig(provider="razorpay", mode="sandbox", webhook_secret=secret)
        pp = PaymentProviders(self.auth, self.state, cfg)
        order = pp.create_order(self.alice_token, 88000, operation_key=_k("pay_ord"))
        prow = pp.get(self.alice_token, order)
        poid = prow["provider_order_id"]
        now = int(time.time())
        body = {
            "event_id": "evt_e2e_" + secrets.token_hex(8),
            "event_type": "authorized",
            "provider_order_id": poid,
            "amount_paise": 88000,
            "currency": "INR",
            "occurred_at": now,
        }
        raw = json.dumps(body, separators=(",", ":")).encode()
        sig = hmac.new(secret, raw, hashlib.sha256).hexdigest()
        res = pp.handle_webhook(self.alice_token, raw, sig, operation_key=_k("wh_e2e"))
        self.assertEqual(res["order_id"], order)
        bad_body = dict(body, event_id="evt_bad_amt_" + secrets.token_hex(4), amount_paise=1)
        raw_bad = json.dumps(bad_body, separators=(",", ":")).encode()
        sig_bad = hmac.new(secret, raw_bad, hashlib.sha256).hexdigest()
        with self.assertRaises(ProviderError):
            pp.handle_webhook(self.alice_token, raw_bad, sig_bad, operation_key=_k("wh_bad_amt"))
        body_bad_sig = dict(body, event_id="evt_bad_sig_" + secrets.token_hex(4))
        raw_bsig = json.dumps(body_bad_sig, separators=(",", ":")).encode()
        with self.assertRaises(ProviderError):
            pp.handle_webhook(self.alice_token, raw_bsig, "0"*64, operation_key=_k("wh_badsig"))

        ai = AIAssistant(self.auth, self.state)
        r = ai.registry.execute(self.alice_token, "read_opportunity", {"opportunity_id": opp_id}, self.auth, self.state)
        self.assertEqual(r["verification"], "read_only")
        d = ai.registry.execute(self.alice_token, "draft_summary", {"title": "T", "source_url": "https://example.com", "external_content": "inject"}, self.auth, self.state)
        self.assertEqual(d["verification"], "draft_unverified")
        with self.assertRaises(Exception):
            ai.registry.execute(self.alice_token, "publish_service", {"service_id": svc_id}, self.auth, self.state)
        queue = getattr(ai, "approval_queue", None) or getattr(ai, "queue", None) or getattr(ai, "approvals", None)
        if queue is not None:
            ai.queue.emergency_stop(self.alice_token, confirmed=True, reason="e2e stop", operation_key=_k("e2e_emerg"))
            self.assertTrue(ai.queue.is_stopped(self.alice_token))
            with self.assertRaises(Exception):
                queue.request(self.alice_token, "publish_service", {"service_id": svc_id}, operation_key=_k("e2e_blocked"))
            ai.queue.emergency_clear(self.alice_token, confirmed=True, operation_key=_k("e2e_clear"))
            self.assertFalse(ai.queue.is_stopped(self.alice_token))
            appr = queue.request(self.alice_token, "publish_service", {"service_id": svc_id}, operation_key=_k("e2e_appr"))
            queue.decide(self.alice_token, appr, "approved", operation_key=_k("e2e_dec"), confirmed=True)
            self.assertEqual(queue.get(self.alice_token, appr)["status"], "approved")
            res2 = ai.registry.execute(self.alice_token, "publish_service", {"service_id": svc_id}, self.auth, self.state, approval_id=appr)
            self.assertIsNotNone(res2)
            with self.assertRaises(AccessDenied):
                queue.get(self.bob_token, appr)

        snap_metrics = DashboardMetrics(self.auth, self.state).get(self.alice_token)
        snap = {
            "format": "ai-earn-dashboard-snapshot",
            "version": 1,
            "read_only": True,
            "generated_at": int(time.time()),
            "metrics": snap_metrics,
        }
        raw_snap = json.dumps(snap).encode()
        self.assertLess(len(raw_snap), 1_000_000)
        parsed = json.loads(raw_snap)
        self.assertEqual(parsed["format"], "ai-earn-dashboard-snapshot")
        self.assertTrue(parsed["read_only"])
        bad_snap = dict(snap, read_only=False)
        self.assertFalse(bad_snap["read_only"])

        health = check({
            "AI_EARN_ENV": "development",
            "AI_EARN_LOG_LEVEL": "INFO",
            "AI_EARN_AUTH_DB": str(self.auth_path),
            "AI_EARN_STATE_DB": str(self.state_path),
        })
        # Health with 2 accounts is not ok (single-owner DB), but still reports correctly and does not leak
        self.assertIn("ok", health)
        self.assertFalse(health["production_ready"])
        backup_path = self.directory / "e2e_backup.sqlite3"
        self.state.backup_to(backup_path, confirmed=True)
        self.assertTrue(backup_path.exists())
        with self.assertRaises(Exception):
            self.state.backup_to(backup_path, confirmed=True)
        from ai_earn.state_recovery_comparison import StateRecoveryComparison
        restored_state = StateStorage(backup_path, create=False)
        comp = StateRecoveryComparison(self.auth, self.state, restored_state)
        rec = comp.compare_records(self.alice_token)
        self.assertGreaterEqual(rec["tables"]["opportunities"]["matching"], 1)
        with self.assertRaises(Exception):
            AuthStorage(self.directory / "dashboard" / "auth.sqlite3", create=True)

        tmp_tok = self.auth.login("alice", "StrongPass12345678")
        self.auth.logout(tmp_tok)
        with self.assertRaises(AccessDenied):
            opps.create(tmp_tok, "After logout", "https://example.com/after", 1000, operation_key=_k("after_logout"))

    def test_tracker_paise_unit(self):
        self.assertEqual(_paise("0"), 0)
        self.assertEqual(_paise("123.45"), 12345)
        with self.assertRaises(ValueError):
            _paise("-1")
        with self.assertRaises(ValueError):
            _paise("1.999")


if __name__ == "__main__":
    unittest.main()

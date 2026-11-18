"""Tests for v21 provider sandbox — HMAC verified, idempotent, auditable, owner-scoped."""

import hashlib
import hmac
import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from ai_earn.auth import AuthService, AccessDenied
from ai_earn.auth_storage import AuthStorage
from ai_earn.state_storage import StateStorage, StateStorageError
from ai_earn.payment_providers import PaymentProviders, ProviderConfig, ProviderError
from ai_earn.dashboard_metrics import DashboardMetrics
from ai_earn.state_recovery_comparison import StateRecoveryComparison


def _razorpay_signature(secret: bytes, body: bytes) -> str:
    return hmac.new(secret, body, hashlib.sha256).hexdigest()


def _stripe_signature(secret: bytes, body: bytes, timestamp: int) -> str:
    signed = f"{timestamp}.{body.decode('utf-8')}".encode("utf-8")
    v1 = hmac.new(secret, signed, hashlib.sha256).hexdigest()
    return f"t={timestamp},v1={v1}"


@unittest.skipUnless(os.name in {"posix", "nt"}, "requires private filesystem")
class ProviderSandboxTests(unittest.TestCase):
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
        state = StateStorage(self.state_path, create=True, confirmed=True)
        for v in range(2, 22):
            getattr(state, f"migrate_to_v{v}")(
                self.directory / f"before-v{v}.sqlite3", confirmed=True
            )
        self.state = StateStorage(self.state_path)
        self.secret_rzp = b"rzp-test-secret-32bytes-long-enough-1234"
        self.secret_stripe = b"stripe-test-secret-32bytes-long-1234xx"
        self.cfg_rzp = ProviderConfig(
            provider="razorpay", mode="sandbox", webhook_secret=self.secret_rzp
        )
        self.cfg_stripe = ProviderConfig(
            provider="stripe", mode="sandbox", webhook_secret=self.secret_stripe
        )
        self.rzp = PaymentProviders(self.auth, self.state, self.cfg_rzp)
        self.stripe = PaymentProviders(self.auth, self.state, self.cfg_stripe)

    def _order(self, provider="razorpay", amount=50000, op="op1"):
        cfg = self.cfg_rzp if provider == "razorpay" else self.cfg_stripe
        pp = self.rzp if provider == "razorpay" else self.stripe
        return pp.create_order(self.alice_token, amount, operation_key=op), pp, cfg

    def test_create_and_get(self):
        oid, _, _ = self._order(op="k1")
        item = self.rzp.get(self.alice_token, oid)
        self.assertEqual(item["amount_paise"], 50000)
        self.assertEqual(item["currency"], "INR")
        self.assertEqual(item["status"], "created")
        self.assertEqual(item["provider"], "razorpay")
        self.assertEqual(item["mode"], "sandbox")
        self.assertTrue(item["provider_order_id"].startswith("order_"))
        with self.assertRaises(AccessDenied):
            self.rzp.get(self.bob_token, oid)

    def test_list_and_filter(self):
        o1, pp1, _ = self._order("razorpay", 10000, "k1")
        o2, pp2, _ = self._order("stripe", 20000, "k2")
        # List all
        lst = self.rzp.list(self.alice_token)
        self.assertEqual(len(lst), 2)
        lst_rzp = self.rzp.list(self.alice_token, provider="razorpay")
        self.assertEqual(len(lst_rzp), 1)
        self.assertEqual(lst_rzp[0]["id"], o1)
        lst_created = self.rzp.list(self.alice_token, status="created")
        self.assertEqual(len(lst_created), 2)
        # Pagination
        page1 = self.rzp.list(self.alice_token, limit=1)
        self.assertEqual(len(page1), 1)
        page2 = self.rzp.list(self.alice_token, after_id=page1[0]["id"], limit=1)
        self.assertEqual(len(page2), 1)
        self.assertNotEqual(page1[0]["id"], page2[0]["id"])

    def test_webhook_razorpay_lifecycle(self):
        oid, pp, cfg = self._order(op="k1")
        info = pp.get(self.alice_token, oid)
        info["created_at"] + 10
        # Need to use actual now from auth clock? But body occurred_at must be within 300 of webhook time.
        # Our webhook handler uses auth.clock() as now, which is current time. We can patch auth.clock.
        # Use info created_at as base; we will trigger webhook shortly after.
        # For this test, occurred_at = now (handler's now) so must be close. Use current time.
        # Get current time via auth.clock()
        cur = int(self.auth.clock())
        body_dict = {
            "event_id": "evt_1",
            "event_type": "authorized",
            "provider_order_id": info["provider_order_id"],
            "amount_paise": 50000,
            "currency": "INR",
            "occurred_at": cur,
        }
        body = json.dumps(body_dict, separators=(",", ":")).encode()
        sig = _razorpay_signature(self.secret_rzp, body)
        res = pp.handle_webhook(self.alice_token, body, sig, operation_key="wh1")
        self.assertEqual(res["new_status"], "authorized")
        self.assertEqual(pp.get(self.alice_token, oid)["status"], "authorized")
        # captured
        cur2 = int(self.auth.clock())
        body_dict2 = {
            "event_id": "evt_2",
            "event_type": "captured",
            "provider_order_id": info["provider_order_id"],
            "amount_paise": 50000,
            "currency": "INR",
            "occurred_at": cur2,
        }
        body2 = json.dumps(body_dict2, separators=(",", ":")).encode()
        sig2 = _razorpay_signature(self.secret_rzp, body2)
        res2 = pp.handle_webhook(self.alice_token, body2, sig2, operation_key="wh2")
        self.assertEqual(res2["new_status"], "captured")
        # settled
        cur3 = int(self.auth.clock())
        body_dict3 = {
            "event_id": "evt_3",
            "event_type": "settled",
            "provider_order_id": info["provider_order_id"],
            "amount_paise": 50000,
            "currency": "INR",
            "occurred_at": cur3,
        }
        body3 = json.dumps(body_dict3, separators=(",", ":")).encode()
        sig3 = _razorpay_signature(self.secret_rzp, body3)
        pp.handle_webhook(self.alice_token, body3, sig3, operation_key="wh3")
        self.assertEqual(pp.get(self.alice_token, oid)["status"], "settled")

    def test_webhook_stripe_lifecycle(self):
        oid, pp, _ = self._order("stripe", 70000, "k1")
        info = pp.get(self.alice_token, oid)
        cur = int(self.auth.clock())
        body_dict = {
            "event_id": "evt_s1",
            "event_type": "authorized",
            "provider_order_id": info["provider_order_id"],
            "amount_paise": 70000,
            "currency": "INR",
            "occurred_at": cur,
        }
        body = json.dumps(body_dict, separators=(",", ":")).encode()
        sig = _stripe_signature(self.secret_stripe, body, cur)
        res = pp.handle_webhook(self.alice_token, body, sig, operation_key="whs1")
        self.assertEqual(res["new_status"], "authorized")
        self.assertEqual(pp.get(self.alice_token, oid)["status"], "authorized")

    def test_invalid_signature(self):
        oid, pp, _ = self._order(op="k1")
        info = pp.get(self.alice_token, oid)
        cur = int(self.auth.clock())
        body_dict = {
            "event_id": "evt_bad",
            "event_type": "authorized",
            "provider_order_id": info["provider_order_id"],
            "amount_paise": 50000,
            "currency": "INR",
            "occurred_at": cur,
        }
        body = json.dumps(body_dict, separators=(",", ":")).encode()
        bad_sig = "0" * 64
        with self.assertRaises(ProviderError):
            pp.handle_webhook(self.alice_token, body, bad_sig, operation_key="wh1")
        # stripe bad
        oid2, pp2, _ = self._order("stripe", 50000, "k2")
        info2 = pp2.get(self.alice_token, oid2)
        body_dict2 = {
            "event_id": "evt_bad2",
            "event_type": "authorized",
            "provider_order_id": info2["provider_order_id"],
            "amount_paise": 50000,
            "currency": "INR",
            "occurred_at": cur,
        }
        body2 = json.dumps(body_dict2, separators=(",", ":")).encode()
        bad_stripe = "t=123,v1=" + "0" * 64
        with self.assertRaises(ProviderError):
            pp2.handle_webhook(self.alice_token, body2, bad_stripe, operation_key="whs2")

    def test_amount_mismatch(self):
        oid, pp, _ = self._order(op="k1")
        info = pp.get(self.alice_token, oid)
        cur = int(self.auth.clock())
        body_dict = {
            "event_id": "evt_amt",
            "event_type": "authorized",
            "provider_order_id": info["provider_order_id"],
            "amount_paise": 1,
            "currency": "INR",
            "occurred_at": cur,
        }
        body = json.dumps(body_dict, separators=(",", ":")).encode()
        sig = _razorpay_signature(self.secret_rzp, body)
        with self.assertRaises(ProviderError):
            pp.handle_webhook(self.alice_token, body, sig, operation_key="wh1")

    def test_invalid_transition(self):
        oid, pp, _ = self._order(op="k1")
        info = pp.get(self.alice_token, oid)
        cur = int(self.auth.clock())
        # try captured directly without authorized should fail (created -> captured not allowed)
        body_dict = {
            "event_id": "evt_invalid",
            "event_type": "captured",
            "provider_order_id": info["provider_order_id"],
            "amount_paise": 50000,
            "currency": "INR",
            "occurred_at": cur,
        }
        body = json.dumps(body_dict, separators=(",", ":")).encode()
        sig = _razorpay_signature(self.secret_rzp, body)
        with self.assertRaises(ProviderError):
            pp.handle_webhook(self.alice_token, body, sig, operation_key="wh1")

    def test_idempotency_and_conflict(self):
        oid, pp, _ = self._order(op="k1")
        # same key same request
        oid2 = pp.create_order(self.alice_token, 50000, operation_key="same")
        oid3 = pp.create_order(self.alice_token, 50000, operation_key="same")
        self.assertEqual(oid2, oid3)
        with self.assertRaises(ProviderError):
            pp.create_order(self.alice_token, 60000, operation_key="same")
        # webhook idempotency same key same body
        info = pp.get(self.alice_token, oid)
        cur = int(self.auth.clock())
        body_dict = {
            "event_id": "evt_idemp",
            "event_type": "authorized",
            "provider_order_id": info["provider_order_id"],
            "amount_paise": 50000,
            "currency": "INR",
            "occurred_at": cur,
        }
        body = json.dumps(body_dict, separators=(",", ":")).encode()
        sig = _razorpay_signature(self.secret_rzp, body)
        res1 = pp.handle_webhook(self.alice_token, body, sig, operation_key="wh_idemp")
        res2 = pp.handle_webhook(self.alice_token, body, sig, operation_key="wh_idemp")
        self.assertEqual(res1, res2)
        # same event_id different body should fail
        body_dict2 = {
            "event_id": "evt_idemp",
            "event_type": "captured",
            "provider_order_id": info["provider_order_id"],
            "amount_paise": 50000,
            "currency": "INR",
            "occurred_at": cur,
        }
        body2 = json.dumps(body_dict2, separators=(",", ":")).encode()
        sig2 = _razorpay_signature(self.secret_rzp, body2)
        with self.assertRaises(ProviderError):
            pp.handle_webhook(self.alice_token, body2, sig2, operation_key="wh_idemp2")
        # same body different operation_key after already processed should fail per our duplicate check
        with self.assertRaises(ProviderError):
            pp.handle_webhook(self.alice_token, body, sig, operation_key="wh_other")

    def test_concurrent_same_key(self):
        results = []
        errors = []

        def attempt():
            try:
                oid = self.rzp.create_order(self.alice_token, 0, operation_key="concurrent")
                results.append(oid)
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=attempt) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(len(results), 5)
        self.assertEqual(len(set(results)), 1)
        self.assertEqual(len(errors), 0)

    def test_live_blocked(self):
        with self.assertRaises(ProviderError):
            ProviderConfig(provider="razorpay", mode="live", webhook_secret=self.secret_rzp)
        with self.assertRaises(ProviderError):
            ProviderConfig(provider="stripe", mode="live", webhook_secret=self.secret_stripe)

    def test_isolation(self):
        oid_alice = self.rzp.create_order(self.alice_token, 0, operation_key="k1")
        oid_bob = PaymentProviders(self.auth, self.state, self.cfg_rzp).create_order(
            self.bob_token, 0, operation_key="k1"
        )
        self.assertNotEqual(oid_alice, oid_bob)
        self.assertEqual(len(self.rzp.list(self.alice_token)), 1)
        self.assertEqual(
            len(PaymentProviders(self.auth, self.state, self.cfg_rzp).list(self.bob_token)), 1
        )
        info = self.rzp.get(self.alice_token, oid_alice)
        cur = int(self.auth.clock())
        body_dict = {
            "event_id": "evt_iso",
            "event_type": "authorized",
            "provider_order_id": info["provider_order_id"],
            "amount_paise": 0,
            "currency": "INR",
            "occurred_at": cur,
        }
        body = json.dumps(body_dict, separators=(",", ":")).encode()
        sig = _razorpay_signature(self.secret_rzp, body)
        with self.assertRaises(AccessDenied):
            PaymentProviders(self.auth, self.state, self.cfg_rzp).handle_webhook(
                self.bob_token, body, sig, operation_key="wh_iso"
            )

    def test_invalid_inputs(self):
        with self.assertRaises(ProviderError):
            self.rzp.create_order(self.alice_token, -1, operation_key="k1")
        with self.assertRaises(ProviderError):
            self.rzp.create_order(self.alice_token, 1_000_000_000_001, operation_key="k1")
        with self.assertRaises(ProviderError):
            self.rzp.get(self.alice_token, "nothex")
        with self.assertRaises(ProviderError):
            self.rzp.list(self.alice_token, status="invalid")
        with self.assertRaises(ProviderError):
            self.rzp.list(self.alice_token, limit=0)
        with self.assertRaises(ProviderError):
            ProviderConfig(provider="invalid", mode="sandbox", webhook_secret=self.secret_rzp)
        with self.assertRaises(ProviderError):
            ProviderConfig(provider="razorpay", mode="sandbox", webhook_secret=b"short")

    def test_history_tamper(self):
        oid, pp, _ = self._order(op="k1")
        info = pp.get(self.alice_token, oid)
        cur = int(self.auth.clock())
        body_dict = {
            "event_id": "evt_tamper",
            "event_type": "authorized",
            "provider_order_id": info["provider_order_id"],
            "amount_paise": 50000,
            "currency": "INR",
            "occurred_at": cur,
        }
        body = json.dumps(body_dict, separators=(",", ":")).encode()
        sig = _razorpay_signature(self.secret_rzp, body)
        pp.handle_webhook(self.alice_token, body, sig, operation_key="wh1")
        # Tamper status directly
        with self.state.transaction() as conn:
            conn.execute("UPDATE payment_provider_orders SET status='failed' WHERE id=?", (oid,))
        with self.assertRaises(StateStorageError):
            pp.get(self.alice_token, oid)

    def test_dashboard_and_recovery(self):
        oid, pp, _ = self._order(op="k1")
        info = pp.get(self.alice_token, oid)
        cur = int(self.auth.clock())
        body_dict = {
            "event_id": "evt_dash",
            "event_type": "authorized",
            "provider_order_id": info["provider_order_id"],
            "amount_paise": 50000,
            "currency": "INR",
            "occurred_at": cur,
        }
        body = json.dumps(body_dict, separators=(",", ":")).encode()
        sig = _razorpay_signature(self.secret_rzp, body)
        pp.handle_webhook(self.alice_token, body, sig, operation_key="wh1")
        metrics = DashboardMetrics(self.auth, self.state).get(self.alice_token)
        self.assertIn("provider_orders", metrics)
        self.assertEqual(metrics["provider_orders"]["count"], 1)
        self.assertIn("provider_events", metrics)
        cand_path = self.directory / "cand.sqlite3"
        self.state.backup_to(cand_path, confirmed=True)
        cand = StateStorage(cand_path)
        comp = StateRecoveryComparison(self.auth, self.state, cand)
        rec = comp.compare_records(self.alice_token)
        self.assertEqual(rec["tables"]["payment_provider_orders"]["matching"], 1)
        snap = comp.compare_snapshot(self.alice_token)
        self.assertIn("provider_events", snap)

    def test_reconcile(self):
        oid, pp, _ = self._order(op="k1")
        pp.get(self.alice_token, oid)
        rec = pp.reconcile(self.alice_token, oid)
        self.assertEqual(rec["order"]["id"], oid)
        self.assertEqual(rec["event_count"], 0)

    def test_migration_v20_to_v21(self):
        tmp2 = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(tmp2.cleanup)
        dir2 = Path(tmp2.name) / "priv"
        if os.name == "nt":
            from ai_earn.windows_security import create_private_directory

            create_private_directory(dir2)
        else:
            dir2.mkdir(mode=0o700)
        auth_path2 = dir2 / "auth.sqlite3"
        state_path2 = dir2 / "state.sqlite3"
        auth2 = AuthService(AuthStorage(auth_path2))
        auth2.register("alice", "disposable test password")
        token2 = auth2.login("alice", "disposable test password")
        state2 = StateStorage(state_path2, create=True, confirmed=True)
        for v in range(2, 21):
            getattr(state2, f"migrate_to_v{v}")(dir2 / f"before-v{v}.sqlite3", confirmed=True)
        with state2.transaction() as conn:
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 20)
        backup = dir2 / "before-v21.sqlite3"
        state2.migrate_to_v21(backup, confirmed=True)
        with state2.transaction() as conn:
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 21)
        pp2 = PaymentProviders(auth2, state2, self.cfg_rzp)
        oid = pp2.create_order(token2, 12345, operation_key="k1")
        self.assertEqual(pp2.get(token2, oid)["status"], "created")

    def test_version_guard(self):
        tmp2 = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(tmp2.cleanup)
        dir2 = Path(tmp2.name) / "priv"
        if os.name == "nt":
            from ai_earn.windows_security import create_private_directory

            create_private_directory(dir2)
        else:
            dir2.mkdir(mode=0o700)
        auth_path2 = dir2 / "auth.sqlite3"
        state_path2 = dir2 / "state.sqlite3"
        auth2 = AuthService(AuthStorage(auth_path2))
        auth2.register("alice", "disposable test password")
        token2 = auth2.login("alice", "disposable test password")
        state2 = StateStorage(state_path2, create=True, confirmed=True)
        for v in range(2, 21):
            getattr(state2, f"migrate_to_v{v}")(dir2 / f"before-v{v}.sqlite3", confirmed=True)
        pp2 = PaymentProviders(auth2, state2, self.cfg_rzp)
        with self.assertRaises(StateStorageError):
            pp2.create_order(token2, 0, operation_key="k1")

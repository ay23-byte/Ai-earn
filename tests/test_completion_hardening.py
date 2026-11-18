"""Completion tests: OAuth refresh renewal (P2-2), reporting reconciliation
(P1-3), and automated dashboard accessibility checks (P4)."""

from __future__ import annotations

import os
import re
import tempfile
import unittest
import urllib.parse
from pathlib import Path
from unittest.mock import patch

from ai_earn.auth import AuthService
from ai_earn.auth_storage import AuthStorage
from ai_earn.connectors.auth import OAuthFlow
from ai_earn.connectors.base import AuthError
from ai_earn.finance import PaymentRecords
from ai_earn.ledger import Ledger
from ai_earn.reporting_reconciliation import ReportingReconciliation
from ai_earn.state_storage import LATEST_SCHEMA_VERSION, StateStorage


class FakeResponse:
    def __init__(self, payload: bytes, status: int = 200):
        self.payload, self.status = payload, status

    def getcode(self):
        return self.status

    def read(self, limit=-1):
        return self.payload if limit < 0 else self.payload[:limit]

    def close(self):
        pass


class RefreshTokenTests(unittest.TestCase):
    def test_refresh_token_renewal_and_revocation(self):
        requests = []

        def opener(request, timeout):
            requests.append(request)
            form = urllib.parse.parse_qs(request.data.decode("ascii"))
            if form.get("grant_type") == ["refresh_token"]:
                self.assertEqual(form["refresh_token"], ["refresh-1"])
                return FakeResponse(b'{"access_token":"token-2","token_type":"Bearer","expires_in":3600}')
            return FakeResponse(b'{"access_token":"token-1","token_type":"Bearer","expires_in":1,"refresh_token":"refresh-1"}')

        flow = OAuthFlow(client_id="c", redirect_uri="http://127.0.0.1/cb", opener=opener)
        state = urllib.parse.parse_qs(urllib.parse.urlsplit(flow.begin_authorization()).query)["state"][0]
        with patch("ai_earn.connectors.auth._time.monotonic", return_value=100.0):
            flow.exchange_code(code="code", state=state)
            self.assertTrue(flow.has_refresh_token)
        with patch("ai_earn.connectors.auth._time.monotonic", return_value=200.0):
            self.assertFalse(flow.connected)
            # Bearer access auto-renews from the refresh token.
            self.assertEqual(flow._bearer_token(), "token-2")
            self.assertTrue(flow.connected)
        flow.revoke()
        self.assertFalse(flow.has_refresh_token)
        with self.assertRaises(AuthError):
            flow.refresh_access_token()
        self.assertEqual(len(requests), 3)  # exchange, refresh, revoke


class ReportingReconciliationTests(unittest.TestCase):
    def test_discrepancy_detection(self):
        tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(tmp.cleanup)
        private = Path(tmp.name) / "priv"
        if os.name == "nt":
            from ai_earn.windows_security import create_private_directory

            create_private_directory(private)
        else:
            private.mkdir(mode=0o700)
        with patch("ai_earn.auth.PASSWORD_ITERATIONS", 1000):
            auth = AuthService(AuthStorage(private / "auth.sqlite3", create=True))
            auth.register("alice", "disposable test password")
            token = auth.login("alice", "disposable test password")
            state = StateStorage(private / "state.sqlite3", create=True, confirmed=True)
            for v in range(2, LATEST_SCHEMA_VERSION + 1):
                getattr(state, f"migrate_to_v{v}")(private / f"b{v}.sqlite3", confirmed=True)
            auth = AuthService(AuthStorage(private / "auth.sqlite3"))
            state = StateStorage(private / "state.sqlite3")
            payments = PaymentRecords(auth, state)
            pid = payments.create(token, 50000, "manual", "UTR-1", operation_key="p1")
            payments.mark_received(token, pid, operation_key="p2")
            payments.mark_settled(token, pid, verified=1, operation_key="p3")
            recon = ReportingReconciliation(auth, state)
            result = recon.reconcile(token)
            self.assertFalse(result["consistent"])
            self.assertEqual(result["discrepancies"][0]["difference_paise"], 50000)
            Ledger(auth, state).add(token, "income", 50000, note="settled", operation_key="l1")
            self.assertTrue(recon.reconcile(token)["consistent"])


class DashboardAccessibilityTests(unittest.TestCase):
    PAGES = ("index.html", "app.html", "connected.html")

    def test_pages_have_skip_link_labels_and_live_regions(self):
        root = Path(__file__).resolve().parents[1] / "dashboard"
        for page in self.PAGES:
            html = (root / page).read_text(encoding="utf-8")
            with self.subTest(page=page):
                self.assertIn('lang="en"', html)
                self.assertIn('name="viewport"', html)
                # every input has an associated label or aria-label
                for tag in re.findall(r"<input[^>]*>", html):
                    self.assertTrue('aria-label=' in tag or 'id=' in tag, tag)
                # interactive buttons are real buttons with text/aria-label
                for tag in re.findall(r"<button[^>]*>", html):
                    self.assertTrue('type="button"' in tag or 'type="submit"' in tag, tag)

    def test_snapshot_renderer_is_keyboard_and_screen_reader_friendly(self):
        root = Path(__file__).resolve().parents[1] / "dashboard"
        html = (root / "connected.html").read_text(encoding="utf-8")
        self.assertIn('aria-live="polite"', html)
        self.assertIn('role="status"', html)
        js = (root / "snapshot.js").read_text(encoding="utf-8")
        self.assertIn("AiEarnSnapshot", js)
        self.assertIn("textContent", js)  # no innerHTML injection of snapshot data


if __name__ == "__main__":
    unittest.main()


class PostgresAdapterTests(unittest.TestCase):
    def test_adapter_binds_owner_and_applies_rls_with_fake_driver(self):
        from ai_earn.postgres_adapter import PostgresAdapter

        executed = []

        class FakeCursor:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def execute(self, sql, params=None):
                executed.append((sql, params))

            def fetchone(self):
                return ("a" * 32,)

        class FakeConn:
            def transaction(self):
                import contextlib

                return contextlib.nullcontext()

            def cursor(self):
                return FakeCursor()

            def commit(self):
                executed.append(("COMMIT", None))

            def rollback(self):
                executed.append(("ROLLBACK", None))

            def close(self):
                executed.append(("CLOSE", None))

        adapter = PostgresAdapter(dsn="postgresql://unused", connection_factory=lambda dsn: FakeConn())
        self.assertTrue(adapter.healthcheck("a" * 32))
        adapter.apply_rls("a" * 32, ["payment_records"])
        sqls = [sql for sql, _ in executed]
        self.assertTrue(any("set_config('app.owner_id'" in sql for sql in sqls))
        self.assertTrue(any("CREATE POLICY owner_isolation ON payment_records" in sql for sql in sqls))
        with self.assertRaises(ValueError):
            adapter.healthcheck("not-an-owner")


class LiveProviderCheckTests(unittest.TestCase):
    def test_dry_run_reports_missing_config_without_network(self):
        import io
        from contextlib import redirect_stdout

        from tools.live_provider_check import main

        with patch.dict(os.environ, {}, clear=True):
            out = io.StringIO()
            with redirect_stdout(out):
                code = main([])
            self.assertEqual(code, 2)
            self.assertIn("no provider was contacted", out.getvalue())


class LiveProviderValidationToolTests(unittest.TestCase):
    def test_live_drive_validation_uses_callback_exchange_list_and_revoke(self):
        from tools.live_provider_check import run_live_validation

        class FakeDrive:
            connector_id = "drive"

            def __init__(self):
                self.calls = []

            def begin_authorization(self, *, owner_id):
                self.calls.append("begin")
                return "https://accounts.google.com/disposable"

            def wait_for_authorization_callback(self, *, timeout):
                self.calls.append("callback")
                return "code", "state"

            def exchange_authorization_code(self, *, code, state, owner_id):
                self.calls.append("exchange")

            def list(self, *, limit, owner_id):
                self.calls.append("list")
                return [{"title": "private-name-not-printed"}, {"title": "second"}]

            def revoke(self, *, owner_id):
                self.calls.append("revoke")

        connector = FakeDrive()
        result = run_live_validation(service="drive", owner_id="a" * 32, connector=connector)
        self.assertEqual(result, {"service": "drive", "items_returned": 2, "status": "validated"})
        self.assertEqual(connector.calls, ["begin", "callback", "exchange", "list", "revoke"])

    def test_live_github_validation_is_read_only_search(self):
        from tools.live_provider_check import run_live_validation

        class FakeGitHub:
            def search(self, *, query, limit, owner_id):
                return [{"title": "Issue"}]

        result = run_live_validation(service="github", owner_id="b" * 32, connector=FakeGitHub())
        self.assertEqual(result["items_returned"], 1)

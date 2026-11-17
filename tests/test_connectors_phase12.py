"""Phase 12 — reusable connector framework: 10 capabilities per connector, optional least-privilege, manual fallback, never expose tokens."""

import unittest
from unittest.mock import patch
import tempfile
import os
from pathlib import Path

from ai_earn.connectors import (
    ConnectorError, AuthError, ThrottleError, PermissionError,
    Throttle, RetryPolicy, validate_api_key, redact_config,
    normalize_gmail, normalize_drive, normalize_github,
    BaseAPIClient, ConnectionState, check_connection_status,
    normalize_error, is_transient,
    GmailConnector, DriveConnector, GitHubConnector,
    CalendarConnector, MarketplaceConnector, PaymentConnector,
)
from ai_earn.connectors.registry import available_connectors, create_connector, connection_status_all, manual_fallback_message
from ai_earn.connectors.scopes import LEAST_PRIVILEGE_SCOPES, validate_scopes_for, least_for
from ai_earn.auth_storage import AuthStorage
from ai_earn.auth import AuthService
from ai_earn.state_storage import StateStorage
from ai_earn.opportunities import Opportunities, ManualConnector


@unittest.skipUnless(os.name in {"posix", "nt"}, "requires private filesystem")
class ConnectorCapabilitiesTests(unittest.TestCase):
    def setUp(self):
        self.key = "a" * 32
        self.owner = "a" * 32

    def test_each_connector_provides_10_capabilities(self):
        # 1. Authentication handling (validate_api_key)
        # 2. Credential validation (validate_config)
        # 3. API client (BaseAPIClient request)
        # 4. Request throttling (Throttle)
        # 5. Error normalization (normalize_error)
        # 6. Retry handling (RetryPolicy)
        # 7. Data normalization (normalize_*)
        # 8. Logging (redacted logs, _log_event)
        # 9. Connection status (connection_status / check_connection_status)
        # 10. Permission checks (check_permission)
        connectors = [
            GmailConnector(api_key=self.key),
            DriveConnector(api_key=self.key),
            GitHubConnector(api_key=self.key),
            CalendarConnector(api_key=self.key),
            MarketplaceConnector(api_key=self.key),
            PaymentConnector(api_key=self.key),
        ]
        for conn in connectors:
            with self.subTest(connector=conn.connector_id):
                # 1 auth: key validated
                self.assertEqual(validate_api_key(self.key), self.key.encode())
                # 2 credential validation
                conn.validate_config()
                self.assertEqual(conn.validate_config(), None)
                # 3 API client
                client = BaseAPIClient(connector=conn, throttle=Throttle(limit=100))
                res = client.request(owner_id=self.owner, operation="fetch", params={"limit": 1})
                self.assertIsInstance(res, list)
                self.assertTrue(len(res) >= 1)
                # logs redacted
                logs = client.logs()
                self.assertTrue(any(l["event"] == "request" for l in logs))
                self.assertNotIn(self.key, str(logs))
                # 4 throttling per-owner
                th = Throttle(limit=1, window_seconds=60)
                c2 = GmailConnector(api_key=self.key, throttle=th)
                c2.fetch(limit=1, owner_id=self.owner)
                with self.assertRaises(ConnectorError):
                    c2.fetch(limit=1, owner_id=self.owner)
                th.reset()
                # 5 error normalization
                err = normalize_error(ValueError("some 429 quota exceeded"))
                self.assertIsInstance(err, ThrottleError)
                self.assertIsInstance(normalize_error(ConnectorError("bad")), ConnectorError)
                # 6 retry
                rp = RetryPolicy(max_retries=2)
                self.assertAlmostEqual(rp.delay_for(1), 0.2)
                self.assertTrue(is_transient(ConnectorError("transient network")))
                self.assertFalse(is_transient(ConnectorError("Invalid API key")))
                # 7 data normalization already via fetch
                for item in res:
                    self.assertIn("title", item)
                    self.assertIn("source_url", item)
                    self.assertIn("budget_paise", item)
                # 8 logging never token
                red = redact_config({"api_key": self.key, "other": "x"})
                self.assertEqual(red["api_key"], "***redacted***")
                self.assertNotIn(self.key, str(red))
                # ensure connector fetch never returns token
                for item in conn.fetch(limit=1):
                    self.assertNotIn(self.key, str(item))
                    self.assertNotIn("api_key", str(item).lower())
                # 9 connection status
                status = conn.connection_status()
                self.assertEqual(status["connector_id"], conn.connector_id)
                self.assertIn(status["status"], {"connected", "revoked", "error", "disconnected"})
                self.assertFalse("api_key" in str(status).lower() or self.key in str(status))
                status2 = check_connection_status(conn)
                self.assertEqual(status2["connector_id"], conn.connector_id)
                # 10 permission checks
                conn.check_permission(self.owner)
                with self.assertRaises(PermissionError):
                    conn.check_permission("invalid")
                # revoked permission denied
                conn2 = GmailConnector(api_key=self.key)
                conn2.revoke()
                with self.assertRaises(PermissionError):
                    conn2.check_permission(self.owner)
                with self.assertRaises(ConnectorError):
                    conn2.fetch(limit=1, owner_id=self.owner)
                self.assertEqual(conn2.connection_status()["status"], "revoked")

    def test_optional_integrations_least_privilege(self):
        # each integration has minimal scopes, not overly broad
        for kind in ["gmail", "drive", "github", "calendar", "marketplace", "payment"]:
            conn = create_connector(kind, api_key=self.key)
            scopes = conn.scopes
            self.assertTrue(len(scopes) >= 1)
            self.assertTrue(all(s in LEAST_PRIVILEGE_SCOPES.get(conn.connector_id, scopes) for s in scopes))
            # validate scopes doesn't exceed least
            validate_scopes_for(conn.connector_id, scopes)
            with self.assertRaises(ConnectorError):
                validate_scopes_for(conn.connector_id, scopes + ["admin.all"])
            # scopes must be small (least)
            self.assertLessEqual(len(scopes), 2)

    def test_never_expose_tokens_in_logs_or_responses(self):
        for Conn in [GmailConnector, DriveConnector, GitHubConnector, CalendarConnector, MarketplaceConnector, PaymentConnector]:
            conn = Conn(api_key=self.key)
            # error messages never contain key
            try:
                validate_api_key("short")
            except ConnectorError as exc:
                self.assertNotIn("short", str(exc))
                self.assertNotIn(self.key, str(exc))
            # fetch result never contains key
            res = conn.fetch(limit=1)
            self.assertNotIn(self.key, json_str := str(res))
            # logs redacted
            client = BaseAPIClient(connector=conn)
            client.request(owner_id=self.owner, operation="fetch", params={"limit": 1})
            logs = str(client.logs())
            self.assertNotIn(self.key, logs)
            # exceptions redacted
            try:
                raise ValueError(f"failed with token {self.key}")
            except Exception as e:
                norm = normalize_error(e)
                self.assertNotIn(self.key, str(norm))

    def test_request_throttling_per_owner_isolation(self):
        th = Throttle(limit=2, window_seconds=60)
        c = GmailConnector(api_key=self.key, throttle=th)
        owner_a = "a" * 32
        owner_b = "b" * 32
        c.fetch(limit=1, owner_id=owner_a)
        c.fetch(limit=1, owner_id=owner_a)
        with self.assertRaises(ConnectorError):
            c.fetch(limit=1, owner_id=owner_a)
        # other owner not throttled
        c.fetch(limit=1, owner_id=owner_b)
        c.fetch(limit=1, owner_id=owner_b)

    def test_retry_handling_transient_vs_auth(self):
        rp = RetryPolicy(max_retries=3, base_delay=0.1, max_delay=5.0)
        # transient retries
        self.assertTrue(rp.should_retry(0, ConnectorError("transient timeout")))
        self.assertTrue(rp.should_retry(1, ConnectorError("network error")))
        # auth/invalid/rate limit not retried
        self.assertFalse(rp.should_retry(0, ConnectorError("Invalid API key")))
        self.assertFalse(rp.should_retry(0, ConnectorError("Rate limit exceeded")))
        self.assertFalse(rp.should_retry(0, AuthError("auth failed")))
        # max retries
        self.assertFalse(rp.should_retry(3, ConnectorError("transient")))
        # BaseAPIClient retry loop with transient
        call_count = 0
        def flaky(**kwargs):
            nonlocal call_count
            call_count += 1
            if call_count < 2:
                raise ConnectorError("transient glitch")
            return [{"title": "Recovered", "source_url": "https://example.com/a", "budget_paise": 0, "deadline_at": 0, "risk_level": "unknown", "skill_tags": ""}]
        conn = GmailConnector(api_key=self.key)
        client = BaseAPIClient(connector=conn, throttle=Throttle(limit=100), retry=RetryPolicy(max_retries=3))
        res = client.request(owner_id=self.owner, operation="flaky", params={}, handler=flaky)
        self.assertEqual(res[0]["title"], "Recovered")
        self.assertEqual(call_count, 2)

    def test_data_normalization_bounded(self):
        # valid
        g = normalize_gmail({"subject": "Hello world", "link": "https://example.com/a"})
        self.assertEqual(g["title"], "Hello world")
        # extra field rejected
        with self.assertRaises(ConnectorError):
            normalize_gmail({"subject": "H", "link": "https://example.com/a", "extra": 1})
        # invalid budget
        with self.assertRaises(ConnectorError):
            normalize_gmail({"subject": "H", "link": "https://example.com/a", "budget_paise": -1})
        # invalid url
        with self.assertRaises(ConnectorError):
            normalize_gmail({"subject": "H", "link": "not-a-url"})

    def test_connection_status_and_revocation(self):
        for Conn in [GmailConnector, CalendarConnector, MarketplaceConnector]:
            conn = Conn(api_key=self.key)
            st = conn.connection_status()
            self.assertEqual(st["status"], "connected")
            self.assertTrue(st["connected"])
            self.assertIn("scopes", st)
            # after revoke
            conn.revoke()
            st2 = conn.connection_status()
            self.assertEqual(st2["status"], "revoked")
            self.assertFalse(st2["connected"])
            with self.assertRaises(ConnectorError):
                conn.fetch(limit=1)
            # registry helper
            self.assertEqual(check_connection_status(conn)["status"], "revoked")

    def test_registry_optional_and_manual_fallback(self):
        kinds = available_connectors()
        self.assertIn("gmail", kinds)
        self.assertIn("calendar", kinds)
        self.assertIn("marketplace", kinds)
        self.assertIn("payment", kinds)
        # create each
        for kind in ["gmail", "drive", "github", "calendar", "marketplace", "payment"]:
            conn = create_connector(kind, api_key=self.key)
            self.assertIsNotNone(conn)
            # connection_status_all
            statuses = connection_status_all([conn])
            self.assertEqual(len(statuses), 1)
            self.assertEqual(statuses[0]["connector_id"], conn.connector_id)
        # manual fallback message
        msg = manual_fallback_message("upwork")
        self.assertIn("manual", msg.lower())
        # manual connector always works
        m = ManualConnector()
        self.assertEqual(m.fetch(limit=5), [])
        # revoked connector fallback to manual
        conn = create_connector("gmail", api_key=self.key)
        conn.revoke()
        with self.assertRaises(ConnectorError):
            conn.fetch(limit=1)
        # manual still ok
        self.assertEqual(ManualConnector().fetch(limit=1), [])

    def test_potential_integrations_mentioned(self):
        # Payment providers, gmail, drive, github, calendar, upwork/fiverr must be creatable
        for kind, api in [("gmail", GmailConnector), ("drive", DriveConnector), ("github", GitHubConnector), ("calendar", CalendarConnector), ("marketplace", MarketplaceConnector), ("payment", PaymentConnector)]:
            conn = api(api_key=self.key)
            self.assertEqual(conn.validate_config(), None)
            res = conn.fetch(limit=1)
            self.assertIsInstance(res, list)

    def test_integration_with_opportunities_and_manual_fallback(self):
        tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(tmp.cleanup)
        directory = Path(tmp.name) / "private"
        if os.name == "nt":
            from ai_earn.windows_security import create_private_directory
            create_private_directory(directory)
        else:
            directory.mkdir(mode=0o700)
        auth_path = directory / "auth.sqlite3"
        state_path = directory / "state.sqlite3"
        with patch("ai_earn.auth.PASSWORD_ITERATIONS", 1000):
            auth = AuthService(AuthStorage(auth_path))
            auth.register("alice", "disposable test password")
            token = auth.login("alice", "disposable test password")
            state = StateStorage(state_path, create=True, confirmed=True)
            for v in range(2, 23):
                getattr(state, f"migrate_to_v{v}")(directory / f"before-v{v}.sqlite3", confirmed=True)
            opp = Opportunities(AuthService(AuthStorage(auth_path)), StateStorage(state_path))
            # calendar import
            cal = CalendarConnector(api_key=self.key, items=[
                {"summary": "Cal opp test", "htmlLink": "https://calendar.google.com/event?eid=1", "budget_paise": 1000, "skill_tags": "meeting"},
            ])
            res = opp.import_from_connector(token, cal, limit=5, operation_key_prefix="cal_sync")
            self.assertEqual(len(res), 1)
            # marketplace import
            mp = MarketplaceConnector(api_key=self.key, items=[
                {"title": "Upwork gig test", "url": "https://www.upwork.com/jobs/~1", "budget_paise": 2000, "skill_tags": "web"},
            ])
            res2 = opp.import_from_connector(token, mp, limit=5, operation_key_prefix="mp_sync")
            self.assertEqual(len(res2), 1)
            # payment import
            pay = PaymentConnector(api_key=self.key)
            res3 = opp.import_from_connector(token, pay, limit=5, operation_key_prefix="pay_sync")
            self.assertEqual(len(res3), 1)
            # revoked => manual fallback still zero
            cal.revoke()
            with self.assertRaises(Exception):
                opp.import_from_connector(token, cal, limit=5, operation_key_prefix="cal_sync2")
            m = ManualConnector()
            res4 = opp.import_from_connector(token, m, limit=5, operation_key_prefix="manual_sync")
            self.assertEqual(len(res4), 0)

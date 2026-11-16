"""Tests for connectors framework — auth, throttle, retry, normalize, stubs, manual fallback."""

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ai_earn.auth_storage import AuthStorage
from ai_earn.auth import AuthService
from ai_earn.state_storage import StateStorage
from ai_earn.connectors import (
    ConnectorError,
    Throttle,
    RetryPolicy,
    validate_api_key,
    redact_config,
    normalize_gmail,
    normalize_drive,
    normalize_github,
    GmailConnector,
    DriveConnector,
    GitHubConnector,
)
from ai_earn.connectors.base import _connector_id
from ai_earn.opportunities import Opportunities, ManualConnector


@unittest.skipUnless(os.name in {"posix", "nt"}, "requires private filesystem")
class ConnectorFrameworkTests(unittest.TestCase):
    def test_connector_id_validation(self):
        self.assertEqual(_connector_id("gmail-stub"), "gmail-stub")
        with self.assertRaises(ConnectorError):
            _connector_id("manual")
        with self.assertRaises(ConnectorError):
            _connector_id("bad id!")
        with self.assertRaises(ConnectorError):
            _connector_id("x"*65)

    def test_auth_validation_and_redaction(self):
        key = "a"*32
        self.assertEqual(validate_api_key(key), key.encode())
        self.assertEqual(validate_api_key(b"b"*32), b"b"*32)
        with self.assertRaises(ConnectorError):
            validate_api_key("short")
        with self.assertRaises(ConnectorError):
            validate_api_key("a"*31)
        with self.assertRaises(ConnectorError):
            validate_api_key("a"*129)
        with self.assertRaises(ConnectorError):
            validate_api_key("a"*32 + "\x00")
        with self.assertRaises(ConnectorError):
            validate_api_key("a "*32)  # space
        red = redact_config({"api_key": "secret1234567890123456789012345678", "other": "x"})
        self.assertEqual(red["api_key"], "***redacted***")
        self.assertEqual(red["other"], "x")
        self.assertNotIn("secret", str(red))

    def test_throttle_per_owner_isolation(self):
        th = Throttle(limit=2, window_seconds=60)
        owner_a = "a"*32
        owner_b = "b"*32
        th.check(owner_a, "gmail-stub")
        th.check(owner_a, "gmail-stub")
        with self.assertRaises(ConnectorError):
            th.check(owner_a, "gmail-stub")
        # different owner not affected
        th.check(owner_b, "gmail-stub")
        th.check(owner_b, "gmail-stub")
        with self.assertRaises(ConnectorError):
            th.check(owner_b, "gmail-stub")
        # different connector not affected
        th.check(owner_a, "drive-stub")
        # reset
        th.reset()
        th.check(owner_a, "gmail-stub")

    def test_retry_policy(self):
        rp = RetryPolicy(max_retries=2, base_delay=0.1, max_delay=1.0)
        self.assertAlmostEqual(rp.delay_for(0), 0.1)
        self.assertAlmostEqual(rp.delay_for(1), 0.2)
        self.assertAlmostEqual(rp.delay_for(2), 0.4)
        with self.assertRaises(ConnectorError):
            rp.delay_for(3)
        self.assertTrue(rp.should_retry(0, ConnectorError("transient")))
        self.assertFalse(rp.should_retry(0, ConnectorError("Invalid API key")))
        self.assertFalse(rp.should_retry(0, ConnectorError("Rate limit exceeded")))
        self.assertFalse(rp.should_retry(2, ConnectorError("transient")))
        with self.assertRaises(ConnectorError):
            RetryPolicy(max_retries=10)
        with self.assertRaises(ConnectorError):
            RetryPolicy(base_delay=-1)

    def test_normalize_gmail_drive_github(self):
        # valid
        g = normalize_gmail({"id": "1", "subject": "Hello", "link": "https://example.com/a", "budget_paise": 1000})
        self.assertEqual(g["title"], "Hello")
        # extra field
        with self.assertRaises(ConnectorError):
            normalize_gmail({"id": "1", "subject": "H", "link": "https://example.com/a", "extra": 1})
        # missing
        with self.assertRaises(ConnectorError):
            normalize_gmail({"id": "1", "subject": "H"})
        # invalid budget
        with self.assertRaises(ConnectorError):
            normalize_gmail({"id": "1", "subject": "H", "link": "https://example.com/a", "budget_paise": -1})
        # drive
        d = normalize_drive({"id": "1", "name": "File", "webViewLink": "https://drive.google.com/file/d/1/view"})
        self.assertEqual(d["title"], "File")
        with self.assertRaises(ConnectorError):
            normalize_drive({"id": "1", "name": "File", "webViewLink": "https://drive.google.com/file/d/1/view", "extra": 1})
        # github
        gh = normalize_github({"id": "1", "title": "Issue", "html_url": "https://github.com/x/y/issues/1"})
        self.assertEqual(gh["title"], "Issue")
        with self.assertRaises(ConnectorError):
            normalize_github({"id": "1", "title": "Issue", "html_url": "https://github.com/x/y/issues/1", "labels": "notalist"})

    def test_gmail_stub_fetch_and_throttle_and_revoke(self):
        key = "k"*32
        th = Throttle(limit=1, window_seconds=60)
        gc = GmailConnector(api_key=key, throttle=th)
        gc.validate_config()
        owner = "a"*32
        res = gc.fetch(limit=1, owner_id=owner)
        self.assertEqual(len(res), 1)
        self.assertIn("title", res[0])
        # second fetch should be throttled
        with self.assertRaises(ConnectorError):
            gc.fetch(limit=1, owner_id=owner)
        # different owner ok
        res2 = gc.fetch(limit=1, owner_id="b"*32)
        self.assertEqual(len(res2), 1)
        # invalid limit
        with self.assertRaises(ConnectorError):
            gc.fetch(limit=0)
        # revoke
        gc.revoke()
        with self.assertRaises(ConnectorError):
            gc.validate_config()
        with self.assertRaises(ConnectorError):
            gc.fetch(limit=1)

    def test_drive_github_stubs(self):
        key = "k"*32
        dc = DriveConnector(api_key=key)
        dc.validate_config()
        self.assertEqual(len(dc.fetch(limit=2)), 2)
        gh = GitHubConnector(api_key=key)
        gh.validate_config()
        self.assertEqual(len(gh.fetch(limit=1)), 1)
        # invalid api key
        with self.assertRaises(ConnectorError):
            DriveConnector(api_key="short")
        with self.assertRaises(ConnectorError):
            GitHubConnector(api_key="short").validate_config()

    def test_manual_fallback_always_available(self):
        m = ManualConnector(connector_id="manual-fallback")
        self.assertEqual(m.fetch(limit=10), [])
        m2 = ManualConnector()
        self.assertEqual(m2.fetch(limit=1), [])
        with self.assertRaises(Exception):
            m.fetch(limit=0)

    def test_integration_with_opportunities_import(self):
        # Ensure stub connectors can be used via Opportunities.import_from_connector
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
            # Use stub connectors
            key = "k"*32
            gmail = GmailConnector(api_key=key, items=[
                {"id": "g1", "subject": "Gmail opp", "link": "https://example.com/g1", "budget_paise": 1000, "skill_tags": "python"},
            ])
            res = opp.import_from_connector(token, gmail, limit=5, operation_key_prefix="gmail_sync")
            self.assertEqual(len(res), 1)
            # idempotency same prefix should return same ids
            res2 = opp.import_from_connector(token, gmail, limit=5, operation_key_prefix="gmail_sync")
            self.assertEqual(res, res2)
            # different prefix creates new records
            res3 = opp.import_from_connector(token, gmail, limit=5, operation_key_prefix="gmail_sync2")
            self.assertEqual(len(res3), 1)
            self.assertNotEqual(res, res3)
            # manual fallback
            manual = ManualConnector()
            res4 = opp.import_from_connector(token, manual, limit=5, operation_key_prefix="manual_sync")
            self.assertEqual(len(res4), 0)

    def test_least_privilege_and_capped(self):
        # Ensure fetch limit capped 100
        key = "k"*32
        gc = GmailConnector(api_key=key, items=[{"id": "1", "subject": "T", "link": "https://example.com/1"}]*150)
        res = gc.fetch(limit=100)
        self.assertEqual(len(res), 100)
        with self.assertRaises(ConnectorError):
            gc.fetch(limit=101)
        # config validation no network
        gc2 = GmailConnector(api_key=key)
        gc2.validate_config()  # should not raise nor network

    def test_revocation_and_manual_fallback_after_revocation(self):
        key = "k"*32
        gc = GmailConnector(api_key=key)
        gc.revoke()
        with self.assertRaises(ConnectorError):
            gc.fetch(limit=1)
        # manual fallback still works
        m = ManualConnector()
        self.assertEqual(m.fetch(limit=5), [])

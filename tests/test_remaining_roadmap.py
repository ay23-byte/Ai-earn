"""Tests for P2-1/P2-3/P2-4/P3-1/P3-2/P5-1 implementations (disposable data)."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
import urllib.parse
from pathlib import Path
from unittest.mock import patch

from ai_earn.ai_provider_config import AIProviderConfigs
from ai_earn.auth import AuthService
from ai_earn.connectors.auth import OAuthFlow
from ai_earn.connectors.base import ConnectorError
from ai_earn.connectors.client import BaseAPIClient
from ai_earn.connectors.drive import DriveConnector
from ai_earn.connectors.github import GitHubConnector
from ai_earn.connectors.gmail import GmailConnector
from ai_earn.finance import PaymentRecords as Payments
from ai_earn.invoices import _pdf_bytes
from ai_earn.postgres_adapter import PostgresAdapterUnavailable, connect, rls_ddl_for, set_owner_sql
from ai_earn.service_workspace import CustomerRequests, Proposals
from ai_earn.services import Services
from ai_earn.settlement_reconciliation import SettlementError, SettlementReconciliation
from ai_earn.state_storage import StateStorage


def _setup(case: unittest.TestCase):
    from ai_earn.auth_storage import AuthStorage
    from ai_earn.state_storage import LATEST_SCHEMA_VERSION

    tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
    case.addCleanup(tmp.cleanup)
    private = Path(tmp.name) / "priv"
    if os.name == "nt":
        from ai_earn.windows_security import create_private_directory

        create_private_directory(private)
    else:
        private.mkdir(mode=0o700)
    auth_db, state_db = private / "auth.sqlite3", private / "state.sqlite3"
    patcher = patch("ai_earn.auth.PASSWORD_ITERATIONS", 1000)
    patcher.start()
    case.addCleanup(patcher.stop)
    auth = AuthService(AuthStorage(auth_db, create=True))
    state = StateStorage(state_db, create=True, confirmed=True)
    for v in range(2, LATEST_SCHEMA_VERSION + 1):
        getattr(state, f"migrate_to_v{v}")(private / f"b{v}.sqlite3", confirmed=True)
    auth.register("alice", "correct horse battery staple")
    token = auth.login("alice", "correct horse battery staple")
    # Fresh handles, mirroring other suites.
    auth = AuthService(AuthStorage(auth_db))
    state = StateStorage(state_db)
    return auth, state, token


class FakeResponse:
    def __init__(self, payload: bytes, status: int = 200):
        self.payload, self.status = payload, status

    def getcode(self):
        return self.status

    def read(self, limit=-1):
        return self.payload if limit < 0 else self.payload[:limit]

    def close(self):
        pass


class SettlementTests(unittest.TestCase):
    def test_csv_match_then_human_confirm_verifies_and_is_idempotent(self):
        auth, state, token = _setup(self)
        payments = Payments(auth, state)
        pid = payments.create(token, 50000, "manual", "UTR-1", operation_key="pay-create")
        payments.mark_received(token, pid, operation_key="pay-recv")
        payments.mark_settled(token, pid, operation_key="pay-settle")
        recon = SettlementReconciliation(auth, state)
        result = recon.reconcile_statement(token, "provider_ref,amount_paise\nUTR-1,50000\nUTR-X,1\n", operation_key="recon-1")
        self.assertEqual(result, {"matched": 1, "unmatched": 1, "replayed": False})
        self.assertEqual(recon.reconcile_statement(token, "provider_ref,amount_paise\nUTR-1,50000\nUTR-X,1\n", operation_key="recon-1")["replayed"], True)
        # Not verified until a human confirms.
        self.assertEqual(payments.get(token, pid)["verified"], 0)
        with self.assertRaises(SettlementError):
            recon.confirm_verified(token, pid, confirmed=False, operation_key="verify-1")
        recon.confirm_verified(token, pid, confirmed=True, operation_key="verify-1")
        recon.confirm_verified(token, pid, confirmed=True, operation_key="verify-1")
        self.assertEqual(payments.get(token, pid)["verified"], 1)
        self.assertEqual(recon.evidence(token, pid)["confirmed"], 1)


class ProposalAcceptanceTests(unittest.TestCase):
    def test_accept_for_request_binds_customer_and_service(self):
        auth, state, token = _setup(self)
        from ai_earn.customers import Customers

        customers = Customers(auth, state)
        cid = customers.create(token, "Acme", "acme@example.com", operation_key="cust-1")
        services = Services(auth, state)
        sid = services.create(token, "API work", "Scope", "Deliverable", 100000, 2, operation_key="svc-1")
        proposals = Proposals(auth, state)
        pid = proposals.create(token, sid, "Proposal", "Do it", 100000, 5, customer_id=cid, operation_key="prop-1")
        proposals.send(token, pid, operation_key="prop-send")
        requests = CustomerRequests(auth, state)
        rid = requests.create(token, sid, cid, "Please build", operation_key="req-1")
        self.assertEqual(proposals.accept_for_request(token, pid, rid, operation_key="accept-1"), pid)
        self.assertEqual(proposals.accept_for_request(token, pid, rid, operation_key="accept-1"), pid)
        self.assertEqual(proposals.get(token, pid)["status"], "accepted")
        self.assertEqual(requests.get(token, rid)["status"], "accepted")


class InvoicePdfTests(unittest.TestCase):
    def test_pdf_is_real_and_deterministic(self):
        a = _pdf_bytes("INV-1", "o" * 32, "a" * 32, 50000, tax_rate_bps=1800, tax_amount_paise=9000)
        b = _pdf_bytes("INV-1", "o" * 32, "a" * 32, 50000, tax_rate_bps=1800, tax_amount_paise=9000)
        self.assertEqual(a, b)
        self.assertTrue(a.startswith(b"%PDF-1.4"))
        self.assertTrue(a.rstrip().endswith(b"%%EOF"))
        self.assertIn(b"INV-1", a)


class AIProviderConfigTests(unittest.TestCase):
    def test_config_persists_hash_only_and_metering_totals(self):
        auth, state, token = _setup(self)
        configs = AIProviderConfigs(auth, state)
        result = configs.configure(token, provider="openai", model="gpt-test", base_url="https://api.openai.com/v1", api_key="k" * 40, operation_key="cfg-1")
        self.assertTrue(result["configured"])
        self.assertTrue(result["has_api_key"])
        self.assertNotIn("k" * 40, json.dumps(result))
        configs.record_usage(token, prompt_tokens=10, completion_tokens=5, operation_key="use-1")
        configs.record_usage(token, prompt_tokens=10, completion_tokens=5, operation_key="use-1")
        self.assertEqual(configs.usage_totals(token), {"calls": 1, "prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15})


class ApprovalFenceAndPostgresTests(unittest.TestCase):
    def test_send_requires_approval(self):
        connector = DriveConnector(api_key="k" * 32)
        client = BaseAPIClient(connector=connector)
        with self.assertRaises(ConnectorError):
            client.send(owner_id="a" * 32, operation="fetch", params={"limit": 1})
        self.assertEqual(len(client.send(owner_id="a" * 32, operation="fetch", params={"limit": 1}, approved=True)), 1)

    def test_postgres_poc_ddl_and_unavailable(self):
        ddl = rls_ddl_for(["payment_records"])
        self.assertIn("CREATE POLICY owner_isolation ON payment_records", ddl)
        self.assertIn("app.owner_id", set_owner_sql("a" * 32)[0])
        with self.assertRaises(PostgresAdapterUnavailable):
            connect("")


class ConnectorLiveTests(unittest.TestCase):
    def test_github_live_search_mocked_and_stub_default(self):
        def opener(request, timeout):
            self.assertIn("api.github.com/search/issues", request.full_url)
            return FakeResponse(json.dumps({"items": [{"id": 1, "title": "Fix bug", "html_url": "https://github.com/o/r/issues/1", "labels": [{"name": "bug"}]}]}).encode())

        connector = GitHubConnector(api_key="g" * 40, connector_id="github", owner_id="a" * 32, opener=opener)
        with patch.dict(os.environ, {"AI_EARN_CONNECTOR_LIVE": "1"}):
            rows = connector.search(query="is:issue is:open", limit=5, owner_id="a" * 32)
            self.assertEqual(rows[0]["title"], "Fix bug")
            with self.assertRaises(ConnectorError):
                connector.search(query="is:issue", limit=1, owner_id="b" * 32)
        with patch.dict(os.environ, {"AI_EARN_CONNECTOR_LIVE": "0"}):
            self.assertEqual(len(connector.fetch(limit=1)), 1)

    def test_drive_and_gmail_live_mocked(self):
        def opener(request, timeout):
            if request.full_url == "https://oauth2.googleapis.com/token":
                return FakeResponse(b'{"access_token":"t","token_type":"Bearer","expires_in":3600}')
            parsed = urllib.parse.urlsplit(request.full_url)
            if parsed.netloc == "www.googleapis.com":
                return FakeResponse(json.dumps({"files": [{"id": "d1", "name": "Brief", "webViewLink": "https://drive.google.com/file/d/d1/view"}]}).encode())
            if parsed.path == "/gmail/v1/users/me/messages":
                return FakeResponse(json.dumps({"messages": [{"id": "m1"}]}).encode())
            return FakeResponse(json.dumps({"id": "m1", "snippet": "hi", "payload": {"headers": [{"name": "Subject", "value": "Gig"}]}}).encode())

        owner = "a" * 32
        with patch.dict(os.environ, {"AI_EARN_CONNECTOR_LIVE": "1"}):
            drive_flow = OAuthFlow(client_id="c", redirect_uri="http://127.0.0.1/cb", opener=opener)
            drive = DriveConnector(connector_id="drive", oauth_flow=drive_flow, owner_id=owner)
            q = urllib.parse.parse_qs(urllib.parse.urlsplit(drive.begin_authorization(owner_id=owner)).query)
            drive.exchange_authorization_code(code="code", state=q["state"][0], owner_id=owner)
            self.assertEqual(drive.list(limit=1, owner_id=owner)[0]["title"], "Brief")

            gmail_flow = OAuthFlow(client_id="c", redirect_uri="http://127.0.0.1/cb", scopes=("gmail.readonly",), opener=opener)
            gmail = GmailConnector(connector_id="gmail", oauth_flow=gmail_flow, owner_id=owner)
            q = urllib.parse.parse_qs(urllib.parse.urlsplit(gmail.begin_authorization(owner_id=owner)).query)
            gmail.exchange_authorization_code(code="code", state=q["state"][0], owner_id=owner)
            self.assertEqual(gmail.search(limit=1, owner_id=owner)[0]["title"], "Gig")


if __name__ == "__main__":
    unittest.main()

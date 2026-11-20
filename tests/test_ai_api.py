import unittest

from ai_earn.ai_api import AIWorkspaceAPI


class FakeQueue:
    def __init__(self):
        self.calls = []

    def is_stopped(self, token):
        return False

    def list(self, token, **kwargs):
        return [{"id": "approval-1", "status": "pending", "effective_status": "pending"}]

    def get(self, token, ident):
        return {"id": ident, "status": "pending"}

    def preview(self, token, ident):
        return {"approval_id": ident, "requires_approval": True}

    def decide(self, token, ident, decision, **kwargs):
        self.calls.append(("decide", decision, kwargs))
        return ident

    def revoke(self, token, ident, **kwargs):
        self.calls.append(("revoke", ident))
        return ident

    def emergency_stop(self, token, **kwargs):
        self.calls.append(("stop", kwargs))
        return "stop-1"

    def emergency_clear(self, token, **kwargs):
        self.calls.append(("clear", kwargs))
        return "clear-1"


class FakeRegistry:
    def list(self):
        return [{"name": "draft_summary", "scope": "draft"}]


class FakeAssistant:
    def __init__(self):
        self.queue = FakeQueue()
        self.registry = FakeRegistry()

    def provider_info(self):
        return {"provider": "stub", "mode": "sandbox"}

    def check_provider_available(self):
        return True

    def chat(self, token, **kwargs):
        return {"output": "draft response", "verification": "draft_unverified"}

    def propose_tool(self, token, tool_name, args, **kwargs):
        return {
            "tool": tool_name,
            "scope": "consequential",
            "requires_approval": True,
            "approval_id": "approval-1",
        }


class AIWorkspaceAPITests(unittest.TestCase):
    def setUp(self):
        self.api = AIWorkspaceAPI(object(), object(), FakeAssistant())

    def test_status_and_approval_list(self):
        status, body = self.api.dispatch("GET", "/api/v1/ai/status", "session")
        self.assertEqual(status, 200)
        self.assertEqual(body["data"]["provider"]["provider"], "stub")
        status, body = self.api.dispatch(
            "GET", "/api/v1/ai/approvals", "session", query="limit=20"
        )
        self.assertEqual(status, 200)
        self.assertEqual(body["data"]["items"][0]["id"], "approval-1")

    def test_chat_is_draft_only_and_rejects_unknown_fields(self):
        status, body = self.api.dispatch(
            "POST", "/api/v1/ai/chat", "session", {"prompt": "Help me plan"}
        )
        self.assertEqual(status, 200)
        self.assertEqual(body["data"]["response"]["verification"], "draft_unverified")
        status, _ = self.api.dispatch(
            "POST", "/api/v1/ai/chat", "session", {"prompt": "Hi", "execute": True}
        )
        self.assertEqual(status, 400)

    def test_proposal_returns_approval_preview(self):
        status, body = self.api.dispatch(
            "POST",
            "/api/v1/ai/proposals",
            "session",
            {"tool_name": "publish_service", "args": {"service_id": "svc-1"}},
            operation_key="proposal-key-123",
        )
        self.assertEqual(status, 201)
        self.assertEqual(body["data"]["proposal"]["approval_id"], "approval-1")

    def test_decision_requires_explicit_confirmation(self):
        status, _ = self.api.dispatch(
            "POST",
            "/api/v1/ai/approvals/a/decision",
            "session",
            {"decision": "approved", "confirmed": False},
            operation_key="decision-key-123",
        )
        self.assertEqual(status, 400)
        status, body = self.api.dispatch(
            "POST",
            "/api/v1/ai/approvals/a/decision",
            "session",
            {"decision": "approved", "confirmed": True},
            operation_key="decision-key-123",
        )
        self.assertEqual(status, 200)
        self.assertEqual(body["data"]["decision"], "approved")

    def test_emergency_stop_requires_confirmation(self):
        status, _ = self.api.dispatch(
            "POST",
            "/api/v1/ai/emergency/stop",
            "session",
            {"confirmed": False},
            operation_key="stop-key-123",
        )
        self.assertEqual(status, 400)
        status, body = self.api.dispatch(
            "POST",
            "/api/v1/ai/emergency/stop",
            "session",
            {"confirmed": True, "reason": "Manual stop"},
            operation_key="stop-key-123",
        )
        self.assertEqual(status, 200)
        self.assertTrue(body["data"]["stopped"])


if __name__ == "__main__":
    unittest.main()

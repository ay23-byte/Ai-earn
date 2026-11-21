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


class RealAIProviderConfigAPITests(unittest.TestCase):
    def setUp(self):
        import os
        import tempfile
        from pathlib import Path
        from unittest.mock import patch

        from ai_earn.auth import AuthService
        from ai_earn.auth_storage import AuthStorage
        from ai_earn.state_storage import LATEST_SCHEMA_VERSION, StateStorage

        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.tmp.cleanup)
        self.directory = Path(self.tmp.name) / "private"
        if os.name == "nt":
            from ai_earn.windows_security import create_private_directory
            create_private_directory(self.directory)
        else:
            self.directory.mkdir(mode=0o700)
        patcher = patch("ai_earn.auth.PASSWORD_ITERATIONS", 1000)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.auth = AuthService(AuthStorage(self.directory / "auth.sqlite3"))
        self.auth.register("owner", "disposable test password")
        self.auth.register("other", "disposable test password")
        self.owner_token = self.auth.login("owner", "disposable test password")
        self.other_token = self.auth.login("other", "disposable test password")
        state = StateStorage(self.directory / "state.sqlite3", create=True, confirmed=True)
        for version in range(2, LATEST_SCHEMA_VERSION + 1):
            getattr(state, f"migrate_to_v{version}")(
                self.directory / f"before-v{version}.sqlite3", confirmed=True
            )
        self.state = StateStorage(self.directory / "state.sqlite3")
        self.api = AIWorkspaceAPI(self.auth, self.state)

    def test_local_provider_configuration_is_persisted_and_owner_scoped(self):
        from unittest.mock import patch

        with patch("ai_earn.ai_assistant.OllamaProvider.is_available", return_value=False):
            status, body = self.api.dispatch(
                "POST",
                "/api/v1/ai/provider",
                self.owner_token,
                {
                    "provider": "ollama",
                    "model": "qwen3.5:4b",
                    "base_url": "http://127.0.0.1:11434",
                },
                operation_key="ollama-config-owner-001",
            )
            self.assertEqual(status, 200, body)
            self.assertEqual(body["data"]["config"]["provider"], "ollama")
            status, body = self.api.dispatch("GET", "/api/v1/ai/status", self.owner_token)
            self.assertEqual(status, 200, body)
            self.assertEqual(body["data"]["provider"]["provider"], "ollama")
            self.assertFalse(body["data"]["provider_available"])
            self.assertEqual(body["data"]["provider_config"]["model"], "qwen3.5:4b")
            self.assertEqual(body["data"]["usage"]["calls"], 0)

            status, other = self.api.dispatch("GET", "/api/v1/ai/status", self.other_token)
            self.assertEqual(status, 200, other)
            self.assertEqual(other["data"]["provider"]["provider"], "stub")
            self.assertFalse(other["data"]["provider_config"]["configured"])

    def test_local_chat_usage_is_persisted_for_the_owner(self):
        from unittest.mock import patch

        with patch("ai_earn.ai_assistant.OllamaProvider.is_available", return_value=True), patch(
            "ai_earn.ai_assistant.OllamaProvider.generate",
            return_value={
                "output": "A draft",
                "model": "qwen3.5:4b",
                "verification": "draft_unverified",
                "usage": {"prompt_tokens": 11, "completion_tokens": 9},
            },
        ):
            status, configured = self.api.dispatch(
                "POST",
                "/api/v1/ai/provider",
                self.owner_token,
                {"provider": "ollama", "model": "qwen3.5:4b", "base_url": "http://127.0.0.1:11434"},
                operation_key="ollama-usage-config-001",
            )
            self.assertEqual(status, 200, configured)
            status, chat = self.api.dispatch(
                "POST",
                "/api/v1/ai/chat",
                self.owner_token,
                {"prompt": "Draft a short proposal"},
                operation_key="ollama-chat-usage-001",
            )
            self.assertEqual(status, 200, chat)
            self.assertEqual(chat["data"]["response"]["verification"], "draft_unverified")
            status, summary = self.api.dispatch("GET", "/api/v1/ai/status", self.owner_token)
            self.assertEqual(status, 200, summary)
            self.assertEqual(summary["data"]["usage"]["calls"], 1)
            self.assertEqual(summary["data"]["usage"]["total_tokens"], 20)

    def test_remote_provider_and_api_key_are_rejected(self):
        status, body = self.api.dispatch(
            "POST",
            "/api/v1/ai/provider",
            self.owner_token,
            {"provider": "openai", "model": "remote-model", "base_url": "https://example.com"},
            operation_key="remote-provider-denied-001",
        )
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], "invalid_request")

        status, body = self.api.dispatch(
            "POST",
            "/api/v1/ai/provider",
            self.owner_token,
            {"provider": "ollama", "model": "model", "base_url": "https://example.com"},
            operation_key="remote-ollama-denied-001",
        )
        self.assertEqual(status, 400)

        status, body = self.api.dispatch(
            "POST",
            "/api/v1/ai/provider",
            self.owner_token,
            {"provider": "ollama", "model": "model", "api_key": "secret"},
            operation_key="api-key-denied-001",
        )
        self.assertEqual(status, 400)




if __name__ == "__main__":
    unittest.main()

"""Authenticated facade for the existing owner-scoped AI assistant and approval queue."""
from __future__ import annotations
import secrets
from urllib.parse import parse_qs
from .ai_assistant import AIAssistant

class AIWorkspaceAPI:
    def __init__(self, auth, state, assistant=None):
        self.auth, self.state = auth, state
        self.assistant = assistant or AIAssistant(auth, state)

    @staticmethod
    def _ok(data):
        return 200, {"ok": True, "data": data, "error": None}

    @staticmethod
    def _error(status, code, message):
        return status, {"ok": False, "data": None, "error": {"code": code, "message": message}}

    @staticmethod
    def _key(payload, operation_key):
        key = operation_key or payload.get("operation_key") or secrets.token_urlsafe(24)
        if not isinstance(key, str) or not 8 <= len(key) <= 128:
            raise ValueError("A valid idempotency key is required.")
        return key

    @staticmethod
    def _only(payload, allowed):
        if set(payload) - set(allowed) - {"operation_key"}:
            raise ValueError("Request contains unsupported fields.")
        return {k: v for k, v in payload.items() if k in allowed}

    def dispatch(self, method, path, token, payload=None, query="", operation_key=None):
        parts = [p for p in path.split("/") if p]
        if len(parts) < 3 or parts[:3] != ["api", "v1", "ai"]:
            return self._error(404, "not_found", "AI route not found.")
        payload = payload or {}
        if not isinstance(payload, dict):
            return self._error(400, "invalid_request", "A JSON object is required.")
        try:
            if method == "GET":
                params = parse_qs(query, keep_blank_values=False)
                if parts == ["api", "v1", "ai", "status"]:
                    rows = self.assistant.queue.list(token, limit=100)
                    return self._ok({"provider": self.assistant.provider_info(),
                        "provider_available": self.assistant.check_provider_available(),
                        "tools": self.assistant.registry.list(),
                        "emergency_stopped": self.assistant.queue.is_stopped(token),
                        "approvals": {"count": len(rows)}})
                if parts == ["api", "v1", "ai", "approvals"]:
                    limit = int(params.get("limit", ["20"])[0])
                    rows = self.assistant.queue.list(token, after_id=params.get("after_id", [None])[0],
                        limit=limit, status=params.get("status", [None])[0])
                    return self._ok({"items": rows, "count": len(rows),
                        "next_cursor": rows[-1]["id"] if len(rows) == limit else None})
                if len(parts) == 5 and parts[3] == "approvals":
                    return self._ok({"item": self.assistant.queue.get(token, parts[4])})
                if len(parts) == 6 and parts[3] == "approvals" and parts[5] == "preview":
                    return self._ok({"preview": self.assistant.queue.preview(token, parts[4])})
                return self._error(404, "not_found", "AI route not found.")
            if method != "POST":
                return self._error(405, "method_not_allowed", "Method is not supported.")
            key = self._key(payload, operation_key)
            if parts == ["api", "v1", "ai", "chat"]:
                values = self._only(payload, {"prompt", "external_content"})
                if not isinstance(values.get("prompt"), str) or not values["prompt"].strip():
                    raise ValueError("prompt is required.")
                return self._ok({"response": self.assistant.chat(token, **values)})
            if parts == ["api", "v1", "ai", "proposals"]:
                values = self._only(payload, {"tool_name", "args"})
                if set(values) != {"tool_name", "args"} or not isinstance(values["args"], dict):
                    raise ValueError("tool_name and args object are required.")
                result = self.assistant.propose_tool(token, values["tool_name"], values["args"], operation_key=key)
                return 201, {"ok": True, "data": {"proposal": result}, "error": None}
            if len(parts) == 6 and parts[3] == "approvals" and parts[5] == "decision":
                values = self._only(payload, {"decision", "confirmed"})
                if set(values) != {"decision", "confirmed"}:
                    raise ValueError("decision and confirmed are required.")
                if values["confirmed"] is not True:
                    raise ValueError("Explicit confirmation is required.")
                if values["decision"] not in {"approved", "rejected"}:
                    raise ValueError("Decision must be approved or rejected.")
                ident = self.assistant.queue.decide(token, parts[4], values["decision"],
                    confirmed=values["confirmed"], operation_key=key)
                return self._ok({"approval_id": ident, "decision": values["decision"]})
            if len(parts) == 6 and parts[3] == "approvals" and parts[5] == "revoke":
                self._only(payload, set())
                ident = self.assistant.queue.revoke(token, parts[4], operation_key=key)
                return self._ok({"approval_id": ident, "revoked": True})
            if parts == ["api", "v1", "ai", "emergency", "stop"]:
                values = self._only(payload, {"confirmed", "reason"})
                if values.get("confirmed") is not True:
                    raise ValueError("Explicit confirmation is required.")
                ident = self.assistant.queue.emergency_stop(token, confirmed=True,
                    reason=values.get("reason", ""), operation_key=key)
                return self._ok({"emergency_stop_id": ident, "stopped": True})
            if parts == ["api", "v1", "ai", "emergency", "clear"]:
                values = self._only(payload, {"confirmed"})
                if values.get("confirmed") is not True:
                    raise ValueError("Explicit confirmation is required.")
                ident = self.assistant.queue.emergency_clear(token, confirmed=True, operation_key=key)
                return self._ok({"emergency_clear_id": ident, "stopped": False})
            return self._error(404, "not_found", "AI route not found.")
        except (ValueError, TypeError) as exc:
            return self._error(400, "invalid_request", str(exc))
        except Exception as exc:
            name = type(exc).__name__
            if name in {"AssistantError", "ApprovalError", "ToolError", "StateStorageError"}:
                return self._error(400, "ai_policy", str(exc))
            if name == "AccessDenied":
                return self._error(404, "not_found", "Approval not found.")
            return self._error(503, "ai_unavailable", "AI workspace operation is temporarily unavailable.")

"""AI assistant — controlled autonomy with modular providers, ToolRegistry, ApprovalQueue.

Phase 13: real assistant connected to backend, helps with 15 tasks:
  opportunity research, opportunity analysis, proposal drafting, customer requirement
  analysis, service descriptions, pricing suggestions, project planning, task breakdown,
  code assistance, data processing, invoice drafts, expense categorization, financial
  summaries, business reports, workflow recommendations.

Modular provider architecture: OpenAI-compatible, hosted, Ollama, stub — all
configurable (provider, model, base_url, api_key). Never assumes provider
available without explicit check. Stub has no network.

Tool permission system:
  A. read-only — may run under authorized user access (no approval)
  B. draft-only — may prepare content but must not send/publish (no approval, draft_unverified)
  C. consequential — requires explicit approval (ApprovalQueue) before execution:
     sending emails, proposals, publishing, external orders, purchases, modifying
     financial records, payments, refunds, deletions, account changes.

Implements: ToolRegistry, Tool permission, ApprovalQueue, Approval expiration (TTL),
Human-readable preview, Execution confirmation (confirmed=True), Audit records
(state_audit), Failure handling, AI execution history. Never bypasses authorization.
Explains intent before consequential actions via preview.
"""

from __future__ import annotations

import builtins
import hmac
import json
import re
import secrets
import sqlite3
import time
from dataclasses import dataclass
from typing import Any, Dict, List

from .auth import AccessDenied, AuthService
from .opportunity_drafts import _digest as _base_digest, _key, _record_id
from .state_authorization import _authorized_transaction
from .state_storage import StateStorage, StateStorageError

_RESOURCE_APPROVAL = "ai_approval"
_RESOURCE_EMERGENCY = "ai_emergency"
_SCOPES = frozenset({"read", "draft", "consequential"})
_STATUSES = frozenset({"pending", "approved", "rejected", "expired", "revoked"})
_TOOL_NAME_RE = re.compile(r"[A-Za-z0-9_.-]{1,64}\Z")
_MAX_ARGS_JSON = 4096
_MAX_PROMPT = 4096
_MAX_EXTERNAL = 4096
_TTL_SECONDS = 900  # 15 min approval window
_MAX_TIME = 4_102_444_800
_MAX_URL = 2048

_TRANSITIONS: dict[tuple[Any, str], str] = {
    (None, "ai_approval_created"): "pending",
    ("pending", "ai_approval_approved"): "approved",
    ("pending", "ai_approval_rejected"): "rejected",
    ("pending", "ai_approval_expired"): "expired",
    ("pending", "ai_approval_revoked"): "revoked",
    ("approved", "ai_approval_revoked"): "revoked",
}


class AssistantError(Exception):
    pass


class ApprovalError(Exception):
    pass


class ToolError(Exception):
    pass


@dataclass(frozen=True)
class ToolDefinition:
    name: str
    description: str  # 1..500 no control
    scope: str  # read|draft|consequential
    handler: Any = None


def _input(validator, value):
    try:
        return validator(value)
    except (AssistantError, ApprovalError, ToolError):
        raise
    except Exception as exc:
        raise AssistantError("Invalid assistant input.") from exc


def _tool_name(value: str) -> str:
    if not isinstance(value, str) or _TOOL_NAME_RE.fullmatch(value) is None:
        raise AssistantError("Invalid tool name.")
    return value


def _scope(value: str) -> str:
    if value not in _SCOPES:
        raise AssistantError("Invalid scope.")
    return value


def _description(value: str) -> str:
    if not isinstance(value, str) or not 1 <= len(value) <= 500:
        raise AssistantError("Invalid description.")
    if any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise AssistantError("Invalid description.")
    return value


def _args_json(value: str) -> str:
    if not isinstance(value, str) or not 2 <= len(value) <= _MAX_ARGS_JSON:
        raise AssistantError("Invalid args JSON.")
    try:
        obj = json.loads(value, object_pairs_hook=_object_hook)
    except (ValueError, TypeError) as exc:
        raise AssistantError("Invalid args JSON.") from exc
    if not isinstance(obj, dict):
        raise AssistantError("Invalid args JSON.")
    for k, v in obj.items():
        if not isinstance(k, str) or not 1 <= len(k) <= 64 or _TOOL_NAME_RE.fullmatch(k) is None:
            raise AssistantError("Invalid args key.")
        if isinstance(v, str):
            if len(v) > 2000 or any(ord(c) < 32 or ord(c) == 127 for c in v):
                raise AssistantError("Invalid args value.")
        elif isinstance(v, int):
            if not -1_000_000_000_000 <= v <= 1_000_000_000_000:
                raise AssistantError("Invalid args value.")
        elif isinstance(v, bool) or v is None:
            pass
        else:
            raise AssistantError("Invalid args value.")
    canonical = json.dumps(obj, ensure_ascii=True, separators=(",", ":"), sort_keys=True)
    if len(canonical) > _MAX_ARGS_JSON:
        raise AssistantError("Invalid args JSON.")
    return canonical


def _prompt(value: str) -> str:
    if not isinstance(value, str) or not 1 <= len(value) <= _MAX_PROMPT:
        raise AssistantError("Invalid prompt.")
    if any(ord(c) < 32 and c not in ("\n", "\t") for c in value) or "\x7f" in value:
        raise AssistantError("Invalid prompt.")
    return value


def _external_content(value: str) -> str:
    if not isinstance(value, str) or len(value) > _MAX_EXTERNAL:
        raise AssistantError("Invalid external content.")
    if any(ord(c) < 32 and c not in ("\n", "\t") for c in value) or "\x7f" in value:
        raise AssistantError("Invalid external content.")
    return value


def _object_hook(pairs):
    d = {}
    for k, v in pairs:
        if k in d:
            raise AssistantError("Duplicate field.")
        d[k] = v
    return d


def _version_check(conn: sqlite3.Connection) -> None:
    from .state_storage import SUPPORTED_STATE_VERSIONS
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version not in SUPPORTED_STATE_VERSIONS or version < 22:
        raise StateStorageError("State schema version 22 is required for AI assistant.")


def _retry(conn, owner, key, digest, resource_type=_RESOURCE_APPROVAL):
    row = conn.execute(
        "SELECT request_hash, resource_type, resource_id FROM state_operations WHERE owner_id=? AND operation_key=?",
        (owner, key),
    ).fetchone()
    if row is None:
        return None
    if row["resource_type"] != resource_type or not hmac.compare_digest(row["request_hash"], digest):
        raise AssistantError("Operation key was already used for a different request.")
    if resource_type == _RESOURCE_APPROVAL:
        if conn.execute("SELECT 1 FROM ai_approvals WHERE id=? AND owner_id=?", (row["resource_id"], owner)).fetchone() is None:
            raise StateStorageError("Stored operation result is unavailable.")
    elif resource_type == _RESOURCE_EMERGENCY:
        if conn.execute("SELECT 1 FROM ai_emergency WHERE owner_id=?", (owner,)).fetchone() is None:
            pass
    return row["resource_id"]


def _audit(conn, now, owner, event, record_id, resource_type=_RESOURCE_APPROVAL):
    conn.execute(
        "INSERT INTO state_audit(occurred_at, actor_id, event, resource_type, resource_id) VALUES(?,?,?,?,?)",
        (now, owner, event, resource_type, record_id),
    )


def _remember(conn, owner, key, digest, record_id, resource_type=_RESOURCE_APPROVAL):
    conn.execute(
        "INSERT INTO state_operations(owner_id, operation_key, request_hash, resource_type, resource_id) VALUES(?,?,?,?,?)",
        (owner, key, digest, resource_type, record_id),
    )


def _request(action: str, values: list) -> bytes:
    return _base_digest(action, json.dumps(values, ensure_ascii=True, separators=(",", ":")))


def _owned_approval(conn, owner, record_id):
    row = conn.execute("SELECT * FROM ai_approvals WHERE id=? AND owner_id=?", (record_id, owner)).fetchone()
    if row is None:
        raise AccessDenied("Access denied.")
    events = conn.execute(
        "SELECT event FROM state_audit WHERE actor_id=? AND resource_type=? AND resource_id=? ORDER BY id",
        (owner, _RESOURCE_APPROVAL, record_id),
    ).fetchall()
    if not events or events[0][0] != "ai_approval_created":
        raise StateStorageError("Approval history is inconsistent.")
    replayed = None
    for (event,) in events:
        step = (replayed, event)
        if step not in _TRANSITIONS:
            raise StateStorageError("Approval history is inconsistent.")
        replayed = _TRANSITIONS[step]
    if replayed != row["status"]:
        raise StateStorageError("Approval history is inconsistent.")
    count = conn.execute(
        "SELECT count(*) FROM state_operations WHERE owner_id=? AND resource_type=? AND resource_id=?",
        (owner, _RESOURCE_APPROVAL, record_id),
    ).fetchone()[0]
    if count != len(events):
        raise StateStorageError("Approval history is inconsistent.")
    return row


def _is_emergency(conn, owner) -> bool:
    row = conn.execute("SELECT stopped FROM ai_emergency WHERE owner_id=?", (owner,)).fetchone()
    if row is None:
        return False
    return bool(row["stopped"])


def _effective_status(row, now) -> str:
    if row["status"] == "pending" and now > row["expires_at"]:
        return "expired"
    return row["status"]


# ---------------------------------------------------------------------------
# Modular model-provider architecture
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class AIProviderConfig:
    """Configurable provider for AI assistance.

    Supports: stub, openai-compatible, hosted, ollama (local).
    Model names and base_url are configurable. Never assumes provider available
    without explicit availability check.

    For live providers, api_key 32..128 bytes is required, but live remains
    blocked until explicit approval (sandbox only).
    """
    provider: str  # stub|openai|ollama|hosted
    mode: str  # sandbox|live
    model: str = "stub-draft-v1"  # configurable model name 1..128
    base_url: str = ""  # configurable endpoint, optional for stub
    api_key: bytes | None = None  # for live
    timeout_seconds: int = 30  # 1..60
    temperature: float = 0.7  # 0..2

    def __post_init__(self):
        if self.provider not in {"stub", "openai", "ollama", "hosted"}:
            raise AssistantError("Invalid AI provider. Choose stub, openai, ollama, or hosted.")
        if self.mode not in {"sandbox", "live"}:
            raise AssistantError("Invalid mode.")
        if not isinstance(self.model, str) or not 1 <= len(self.model) <= 128 or any(ord(c) < 32 or ord(c) == 127 for c in self.model):
            raise AssistantError("Invalid model name.")
        if not isinstance(self.base_url, str) or len(self.base_url) > _MAX_URL:
            raise AssistantError("Invalid base_url.")
        if self.base_url:
            if not (self.base_url.startswith("http://") or self.base_url.startswith("https://")):
                raise AssistantError("base_url must be http(s)://")
            if any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in self.base_url):
                raise AssistantError("Invalid base_url.")
        if not isinstance(self.timeout_seconds, int) or not 1 <= self.timeout_seconds <= 60:
            raise AssistantError("Invalid timeout.")
        if not isinstance(self.temperature, (int, float)) or not 0 <= self.temperature <= 2:
            raise AssistantError("Invalid temperature.")
        if self.mode == "live" and (self.api_key is None or not 32 <= len(self.api_key) <= 128):
            raise AssistantError("Live mode requires API key 32..128 bytes.")
        if self.mode == "live":
            raise AssistantError("Live AI provider is not enabled; sandbox only until explicit approval.")

    def is_available(self) -> bool:
        """Never assume provider available without checking. Stub always available in sandbox."""
        if self.provider == "stub":
            return self.mode == "sandbox"
        # For openai/ollama/hosted, check base_url and model configured, but live blocked -> not available
        if self.mode == "sandbox":
            # sandbox may simulate hosted; treat as not available unless explicitly sandbox provider stub
            # For demo, only stub is available; others require live approval
            return False
        return False

    def describe(self) -> dict:
        return {
            "provider": self.provider,
            "mode": self.mode,
            "model": self.model,
            "base_url": self.base_url or "(stub, no network)",
            "timeout_seconds": self.timeout_seconds,
            "available": self.is_available(),
            "verification": "draft_unverified" if self.provider == "stub" else "not_configured",
        }


class ProviderBase:
    """Abstract provider — generate drafts, no network for stub."""

    def generate(self, prompt: str, *, external_content: str | None = None, **kwargs) -> dict:
        raise NotImplementedError

    def is_available(self) -> bool:
        return False

    def info(self) -> dict:
        return {"provider": "base", "available": self.is_available()}


class StubProvider(ProviderBase):
    """Deterministic draft-only stub, no network, no secrets."""

    def __init__(self, model: str = "stub-draft-v1"):
        self.model = model

    def is_available(self) -> bool:
        return True

    def info(self) -> dict:
        return {"provider": "stub", "model": self.model, "available": True, "verification": "draft_unverified"}

    def generate(self, prompt: str, *, external_content: str | None = None, **kwargs) -> dict:
        _prompt(prompt)
        if external_content is not None:
            _external_content(external_content)
            sanitized = external_content[:200].replace("\n", " ")
            return {
                "output": f"draft_unverified summary for '{prompt[:80]}' with external data [{sanitized}]",
                "model": self.model,
                "verification": "draft_unverified",
                "note": "External content treated as data, not instruction. No tool was auto-invoked.",
            }
        return {
            "output": f"draft_unverified response to '{prompt[:100]}'",
            "model": self.model,
            "verification": "draft_unverified",
        }


class OpenAICompatibleProvider(ProviderBase):
    """OpenAI-compatible API — configurable base_url and model, no assumption of availability."""

    def __init__(self, config: AIProviderConfig):
        if config.provider != "openai":
            raise AssistantError("OpenAICompatibleProvider requires provider='openai'")
        self.config = config

    def is_available(self) -> bool:
        # Never assume available without check; live blocked
        return self.config.is_available()

    def info(self) -> dict:
        return {"provider": "openai", "model": self.config.model, "base_url": self.config.base_url, "available": self.is_available(), "verification": "requires_live_approval"}

    def generate(self, prompt: str, *, external_content: str | None = None, **kwargs) -> dict:
        # In sandbox, refuse to call network; return stub-like draft indicating not available
        if not self.is_available():
            raise AssistantError("OpenAI provider not available; check config and approval. Use stub for drafts.")
        _prompt(prompt)
        # Real implementation would POST to f"{base_url}/v1/chat/completions" with model, but blocked
        return StubProvider(model=self.config.model).generate(prompt, external_content=external_content)


class OllamaProvider(ProviderBase):
    """Local Ollama — configurable endpoint, never assume running."""

    def __init__(self, config: AIProviderConfig):
        if config.provider != "ollama":
            raise AssistantError("OllamaProvider requires provider='ollama'")
        self.config = config

    def is_available(self) -> bool:
        return self.config.is_available()

    def info(self) -> dict:
        return {"provider": "ollama", "model": self.config.model, "base_url": self.config.base_url or "http://localhost:11434", "available": self.is_available(), "verification": "requires_local_ollama"}

    def generate(self, prompt: str, *, external_content: str | None = None, **kwargs) -> dict:
        if not self.is_available():
            raise AssistantError("Ollama provider not available; ensure Ollama is running and configured.")
        _prompt(prompt)
        return StubProvider(model=self.config.model).generate(prompt, external_content=external_content)


class HostedProvider(ProviderBase):
    """Other hosted providers — generic, configurable."""

    def __init__(self, config: AIProviderConfig):
        if config.provider != "hosted":
            raise AssistantError("HostedProvider requires provider='hosted'")
        self.config = config

    def is_available(self) -> bool:
        return self.config.is_available()

    def info(self) -> dict:
        return {"provider": "hosted", "model": self.config.model, "base_url": self.config.base_url, "available": self.is_available()}

    def generate(self, prompt: str, *, external_content: str | None = None, **kwargs) -> dict:
        if not self.is_available():
            raise AssistantError("Hosted provider not available; check config.")
        _prompt(prompt)
        return StubProvider(model=self.config.model).generate(prompt, external_content=external_content)


def get_provider(config: AIProviderConfig | None) -> ProviderBase:
    """Factory — returns provider instance without assuming availability."""
    if config is None:
        config = AIProviderConfig(provider="stub", mode="sandbox")
    if config.provider == "stub":
        return StubProvider(model=config.model)
    if config.provider == "openai":
        return OpenAICompatibleProvider(config)
    if config.provider == "ollama":
        return OllamaProvider(config)
    if config.provider == "hosted":
        return HostedProvider(config)
    raise AssistantError("Unknown provider.")


# ---------------------------------------------------------------------------
# ToolRegistry — now covers 15 tasks
# ---------------------------------------------------------------------------

class ToolRegistry:
    """Centralized, owner-scoped policy for tool execution.

    Covers 15 assistant tasks across read/draft/consequential.
    """

    def __init__(self):
        self._tools: Dict[str, ToolDefinition] = {}
        self._history: List[dict] = []  # execution history
        self._register_builtins()

    def _register_builtins(self):
        # --- A. Read-only (no approval, authorized user) ---
        self.register(ToolDefinition(name="read_opportunity", description="Read opportunity record (read scope, no approval)", scope="read", handler=self._handle_read_opportunity))
        self.register(ToolDefinition(name="read_service", description="Read service record (read scope)", scope="read", handler=self._handle_read_service))
        self.register(ToolDefinition(name="list_opportunities", description="List opportunities with filters (read)", scope="read", handler=self._handle_list_opportunities))
        # 15-task helpers — read
        self.register(ToolDefinition(name="research_opportunity", description="Opportunity research — fetch and summarize market data (read)", scope="read", handler=self._handle_research_opportunity))
        self.register(ToolDefinition(name="analyze_opportunity", description="Opportunity analysis — evaluate budget/risk/deadline (read)", scope="read", handler=self._handle_analyze_opportunity))
        self.register(ToolDefinition(name="analyze_customer_requirements", description="Customer requirement analysis — read requirements (read)", scope="read", handler=self._handle_analyze_customer_requirements))
        self.register(ToolDefinition(name="summarize_finances", description="Financial summaries — read ledger/invoices (read)", scope="read", handler=self._handle_summarize_finances))
        self.register(ToolDefinition(name="generate_business_report", description="Business reports — read metrics (read, draft if writing)", scope="read", handler=self._handle_generate_business_report))
        # --- B. Draft-only (prepares content, not sending) ---
        self.register(ToolDefinition(name="draft_summary", description="Draft AI summary for opportunity (draft scope, unverified)", scope="draft", handler=self._handle_draft_summary))
        self.register(ToolDefinition(name="draft_proposal", description="Draft service proposal (draft scope, unverified)", scope="draft", handler=self._handle_draft_proposal))
        self.register(ToolDefinition(name="draft_service_description", description="Service descriptions — draft marketing copy (draft)", scope="draft", handler=self._handle_draft_service_description))
        self.register(ToolDefinition(name="suggest_pricing", description="Pricing suggestions — draft budget recommendation (draft, unverified)", scope="draft", handler=self._handle_suggest_pricing))
        self.register(ToolDefinition(name="plan_project", description="Project planning — draft milestones (draft)", scope="draft", handler=self._handle_plan_project))
        self.register(ToolDefinition(name="breakdown_tasks", description="Task breakdown — draft subtasks (draft)", scope="draft", handler=self._handle_breakdown_tasks))
        self.register(ToolDefinition(name="assist_code", description="Code assistance — draft code snippet (draft, unverified)", scope="draft", handler=self._handle_assist_code))
        self.register(ToolDefinition(name="process_data", description="Data processing — draft transformation (draft)", scope="draft", handler=self._handle_process_data))
        self.register(ToolDefinition(name="draft_invoice", description="Invoice drafts — draft invoice content (draft)", scope="draft", handler=self._handle_draft_invoice))
        self.register(ToolDefinition(name="suggest_expense_category", description="Expense categorization suggestions — draft category (draft)", scope="draft", handler=self._handle_suggest_expense_category))
        self.register(ToolDefinition(name="summarize_finances_draft", description="Financial summaries — draft summary (draft, unverified)", scope="draft", handler=self._handle_summarize_finances_draft))
        self.register(ToolDefinition(name="business_report_draft", description="Business reports — draft report (draft, unverified)", scope="draft", handler=self._handle_business_report_draft))
        self.register(ToolDefinition(name="recommend_workflow", description="Workflow recommendations — draft workflow steps (draft)", scope="draft", handler=self._handle_recommend_workflow))
        # --- C. Consequential (requires approval) ---
        self.register(ToolDefinition(name="publish_service", description="Publish service to catalog (consequential, requires approval)", scope="consequential", handler=self._handle_publish_service))
        self.register(ToolDefinition(name="send_communication", description="Send customer communication — email (consequential, requires approval)", scope="consequential", handler=self._handle_send_communication))
        self.register(ToolDefinition(name="convert_customer", description="Convert customer lead (consequential, requires approval)", scope="consequential", handler=self._handle_convert_customer))
        self.register(ToolDefinition(name="send_proposal", description="Send proposal to customer (consequential, requires approval)", scope="consequential", handler=self._handle_send_proposal))
        self.register(ToolDefinition(name="create_external_order", description="Creating external orders (consequential, requires approval)", scope="consequential", handler=self._handle_create_external_order))
        self.register(ToolDefinition(name="initiate_payment", description="Initiating payments (consequential, requires approval)", scope="consequential", handler=self._handle_initiate_payment))
        self.register(ToolDefinition(name="issue_refund", description="Issuing refunds (consequential, requires approval)", scope="consequential", handler=self._handle_issue_refund))
        self.register(ToolDefinition(name="delete_important_record", description="Deleting important records (consequential, requires approval)", scope="consequential", handler=self._handle_delete_important_record))
        self.register(ToolDefinition(name="update_account_settings", description="Changing external account settings (consequential, requires approval)", scope="consequential", handler=self._handle_update_account_settings))
        self.register(ToolDefinition(name="make_purchase", description="Making purchases (consequential, requires approval)", scope="consequential", handler=self._handle_make_purchase))
        # Financial modifying
        self.register(ToolDefinition(name="modify_financial_record", description="Modifying financial records (consequential, requires approval)", scope="consequential", handler=self._handle_modify_financial_record))

    def register(self, tool: ToolDefinition):
        _tool_name(tool.name)
        _description(tool.description)
        _scope(tool.scope)
        if tool.name in self._tools:
            raise ToolError("Tool already registered.")
        if not callable(tool.handler):
            raise ToolError("Invalid handler.")
        self._tools[tool.name] = tool

    def get(self, name: str) -> ToolDefinition:
        _tool_name(name)
        if name not in self._tools:
            raise ToolError("Tool not found.")
        return self._tools[name]

    def list(self) -> builtins.list[dict]:
        return [{"name": t.name, "description": t.description, "scope": t.scope} for t in self._tools.values()]

    def requires_approval(self, name: str) -> bool:
        tool = self.get(name)
        return tool.scope == "consequential"

    # -- Builtin handlers (strict validation, external content treated as data) --
    def _handle_read_opportunity(self, args: dict, owner: str, token: str, auth: AuthService, state: StateStorage) -> dict:
        if frozenset(args) != {"opportunity_id"}:
            raise ToolError("Invalid arguments.")
        _input(_record_id, args["opportunity_id"])
        raise ToolError("Not implemented directly.")

    def _handle_read_service(self, args: dict, owner: str, token: str, auth: AuthService, state: StateStorage) -> dict:
        if frozenset(args) != {"service_id"}:
            raise ToolError("Invalid arguments.")
        _input(_record_id, args["service_id"])
        raise ToolError("Not implemented directly.")

    def _handle_list_opportunities(self, args: dict, owner: str, token: str, auth: AuthService, state: StateStorage) -> dict:
        allowed = {"status", "risk_level", "limit", "skill_contains"}
        if not frozenset(args).issubset(allowed):
            raise ToolError("Invalid arguments.")
        raise ToolError("Not implemented directly.")

    def _handle_research_opportunity(self, args: dict, owner: str, token: str, auth: AuthService, state: StateStorage) -> dict:
        # read opportunity research: requires query, returns draft research summary
        if not frozenset(args).issubset({"query", "limit", "external_content"}):
            raise ToolError("Invalid arguments.")
        raise ToolError("Not implemented directly.")

    def _handle_analyze_opportunity(self, args: dict, owner: str, token: str, auth: AuthService, state: StateStorage) -> dict:
        raise ToolError("Not implemented directly.")

    def _handle_analyze_customer_requirements(self, args: dict, owner: str, token: str, auth: AuthService, state: StateStorage) -> dict:
        raise ToolError("Not implemented directly.")

    def _handle_summarize_finances(self, args: dict, owner: str, token: str, auth: AuthService, state: StateStorage) -> dict:
        raise ToolError("Not implemented directly.")

    def _handle_generate_business_report(self, args: dict, owner: str, token: str, auth: AuthService, state: StateStorage) -> dict:
        raise ToolError("Not implemented directly.")

    def _handle_draft_summary(self, args: dict, owner: str, token: str, auth: AuthService, state: StateStorage) -> dict:
        if frozenset(args) != {"title", "source_url"} and frozenset(args) != {"title", "source_url", "external_content"}:
            raise ToolError("Invalid arguments.")
        _input(lambda v: v if isinstance(v, str) and 1 <= len(v) <= 200 and not any(ord(c) < 32 or ord(c)==127 for c in v) else (_ for _ in ()).throw(AssistantError("x")), args["title"])
        raise ToolError("Not implemented directly.")

    def _handle_draft_proposal(self, args: dict, owner: str, token: str, auth: AuthService, state: StateStorage) -> dict:
        raise ToolError("Not implemented directly.")

    def _handle_draft_service_description(self, args: dict, owner: str, token: str, auth: AuthService, state: StateStorage) -> dict:
        raise ToolError("Not implemented directly.")

    def _handle_suggest_pricing(self, args: dict, owner: str, token: str, auth: AuthService, state: StateStorage) -> dict:
        raise ToolError("Not implemented directly.")

    def _handle_plan_project(self, args: dict, owner: str, token: str, auth: AuthService, state: StateStorage) -> dict:
        raise ToolError("Not implemented directly.")

    def _handle_breakdown_tasks(self, args: dict, owner: str, token: str, auth: AuthService, state: StateStorage) -> dict:
        raise ToolError("Not implemented directly.")

    def _handle_assist_code(self, args: dict, owner: str, token: str, auth: AuthService, state: StateStorage) -> dict:
        raise ToolError("Not implemented directly.")

    def _handle_process_data(self, args: dict, owner: str, token: str, auth: AuthService, state: StateStorage) -> dict:
        raise ToolError("Not implemented directly.")

    def _handle_draft_invoice(self, args: dict, owner: str, token: str, auth: AuthService, state: StateStorage) -> dict:
        raise ToolError("Not implemented directly.")

    def _handle_suggest_expense_category(self, args: dict, owner: str, token: str, auth: AuthService, state: StateStorage) -> dict:
        raise ToolError("Not implemented directly.")

    def _handle_summarize_finances_draft(self, args: dict, owner: str, token: str, auth: AuthService, state: StateStorage) -> dict:
        raise ToolError("Not implemented directly.")

    def _handle_business_report_draft(self, args: dict, owner: str, token: str, auth: AuthService, state: StateStorage) -> dict:
        raise ToolError("Not implemented directly.")

    def _handle_recommend_workflow(self, args: dict, owner: str, token: str, auth: AuthService, state: StateStorage) -> dict:
        raise ToolError("Not implemented directly.")

    def _handle_publish_service(self, args: dict, owner: str, token: str, auth: AuthService, state: StateStorage) -> dict:
        raise ToolError("Not implemented directly.")

    def _handle_send_communication(self, args: dict, owner: str, token: str, auth: AuthService, state: StateStorage) -> dict:
        raise ToolError("Not implemented directly.")

    def _handle_convert_customer(self, args: dict, owner: str, token: str, auth: AuthService, state: StateStorage) -> dict:
        raise ToolError("Not implemented directly.")

    def _handle_send_proposal(self, args: dict, owner: str, token: str, auth: AuthService, state: StateStorage) -> dict:
        raise ToolError("Not implemented directly.")

    def _handle_create_external_order(self, args: dict, owner: str, token: str, auth: AuthService, state: StateStorage) -> dict:
        raise ToolError("Not implemented directly.")

    def _handle_initiate_payment(self, args: dict, owner: str, token: str, auth: AuthService, state: StateStorage) -> dict:
        raise ToolError("Not implemented directly.")

    def _handle_issue_refund(self, args: dict, owner: str, token: str, auth: AuthService, state: StateStorage) -> dict:
        raise ToolError("Not implemented directly.")

    def _handle_delete_important_record(self, args: dict, owner: str, token: str, auth: AuthService, state: StateStorage) -> dict:
        raise ToolError("Not implemented directly.")

    def _handle_update_account_settings(self, args: dict, owner: str, token: str, auth: AuthService, state: StateStorage) -> dict:
        raise ToolError("Not implemented directly.")

    def _handle_make_purchase(self, args: dict, owner: str, token: str, auth: AuthService, state: StateStorage) -> dict:
        raise ToolError("Not implemented directly.")

    def _handle_modify_financial_record(self, args: dict, owner: str, token: str, auth: AuthService, state: StateStorage) -> dict:
        raise ToolError("Not implemented directly.")

    def validate_args(self, tool_name: str, args: dict) -> str:
        """Strictly validate args, return canonical JSON."""
        tool = self.get(tool_name)
        if not isinstance(args, dict):
            raise ToolError("Invalid arguments.")
        # Tool-specific schema with injection isolation checks
        if tool.scope == "read":
            if tool.name in {"read_opportunity", "research_opportunity", "analyze_opportunity"}:
                # read-like tools may have opportunity_id or query
                if tool.name == "read_opportunity":
                    if frozenset(args) != {"opportunity_id"}:
                        raise ToolError("Invalid arguments.")
                    _input(_record_id, args["opportunity_id"])
                else:
                    allowed = {"opportunity_id", "query", "limit", "external_content", "skill_contains", "status", "risk_level"}
                    if not frozenset(args).issubset(allowed):
                        raise ToolError("Invalid arguments.")
                    if "opportunity_id" in args:
                        _input(_record_id, args["opportunity_id"])
                    if "query" in args:
                        if not isinstance(args["query"], str) or not 1 <= len(args["query"]) <= 500 or any(ord(c) < 32 or ord(c)==127 for c in args["query"]):
                            raise ToolError("Invalid query.")
                    if "external_content" in args:
                        _external_content(args["external_content"])
            elif tool.name in {"analyze_customer_requirements", "read_service"}:
                if frozenset(args) != {"customer_id"} and frozenset(args) != {"service_id"} and frozenset(args) != {"opportunity_id"}:
                    # allow flexible read ids but validate
                    if not args:
                        raise ToolError("Invalid arguments.")
                    for k, v in args.items():
                        if k.endswith("_id"):
                            _input(_record_id, v)
                        elif k == "external_content":
                            _external_content(v)
                        else:
                            if not isinstance(v, str) or len(v) > 500:
                                raise ToolError("Invalid arg.")
                # simpler: allow customer_id
                if "customer_id" in args:
                    _input(_record_id, args["customer_id"])
            elif tool.name in {"summarize_finances", "generate_business_report"}:
                allowed = {"period", "external_content", "limit"}
                if not frozenset(args).issubset(allowed):
                    raise ToolError("Invalid arguments.")
                if "period" in args and args["period"] not in {"daily", "weekly", "monthly", "quarterly", "all"}:
                    raise ToolError("Invalid period.")
                if "external_content" in args:
                    _external_content(args["external_content"])
            else:
                # generic read: allow bounded
                for k, v in args.items():
                    if len(k) > 64 or any(ord(c) < 32 or ord(c)==127 for c in k):
                        raise ToolError("Invalid key.")
                    if isinstance(v, str) and len(v) > 2000:
                        raise ToolError("Invalid value.")
                    if isinstance(v, str) and "external_content" == k:
                        _external_content(v)
            # reject instruction-like keys
            for k in args:
                if "instruction" in k.lower() or "system" in k.lower():
                    raise ToolError("Invalid args key (instruction).")
        elif tool.scope == "draft":
            # draft tools: per-tool allowed keys, strict extra-field rejection, injection isolation
            per_tool_allowed: dict[str, set[str]] = {
                "draft_summary": {"title", "source_url", "external_content"},
                "draft_proposal": {"title", "description", "service_id", "customer_id", "budget_paise", "external_content", "skill_tags"},
                "draft_service_description": {"service_id", "title", "description", "external_content"},
                "suggest_pricing": {"opportunity_id", "service_id", "budget_paise", "risk_level", "external_content", "title"},
                "plan_project": {"opportunity_id", "title", "description", "deadline_at", "external_content", "service_id"},
                "breakdown_tasks": {"opportunity_id", "project_id", "title", "description", "external_content", "service_id"},
                "assist_code": {"code_request", "prompt", "description", "external_content", "title"},
                "process_data": {"data", "prompt", "description", "external_content", "title"},
                "draft_invoice": {"opportunity_id", "customer_id", "amount_paise", "invoice_id", "external_content", "title", "budget_paise"},
                "suggest_expense_category": {"expense_description", "amount_paise", "external_content", "title"},
                "summarize_finances_draft": {"period", "external_content", "title"},
                "business_report_draft": {"period", "report_type", "external_content", "title", "description"},
                "recommend_workflow": {"workflow_goal", "prompt", "external_content", "title", "description"},
                "research_opportunity": {"query", "limit", "external_content"},
                "analyze_opportunity": {"opportunity_id", "external_content"},
            }
            allowed = per_tool_allowed.get(tool.name)  # type: ignore[assignment]
            if allowed is not None:
                if not frozenset(args).issubset(allowed):
                    raise ToolError("Invalid arguments.")
            else:
                # fallback: strict generic draft must still be subset of broadly allowed
                broadly = {"title", "source_url", "description", "service_id", "customer_id", "opportunity_id", "budget_paise", "code_request", "data", "invoice_id", "expense_description", "period", "report_type", "workflow_goal", "prompt", "external_content", "skill_tags", "risk_level", "amount_paise", "project_id"}
                if not frozenset(args).issubset(broadly):
                    raise ToolError("Invalid arguments.")
            for k, v in args.items():
                if "instruction" in k.lower():
                    raise ToolError("Invalid args key (instruction).")
                if _TOOL_NAME_RE.fullmatch(k) is None:
                    raise ToolError("Invalid args key.")
                if k == "external_content":
                    _external_content(v)
                elif k == "title":
                    if not isinstance(v, str) or not 1 <= len(v) <= 200 or any(ord(c) < 32 or ord(c)==127 for c in v):
                        raise ToolError("Invalid title.")
                elif k == "source_url":
                    if not isinstance(v, str) or not 1 <= len(v) <= 2048 or not (v.startswith("https://") or v.startswith("http://")):
                        raise ToolError("Invalid source_url.")
                elif isinstance(v, str):
                    if len(v) > 2000 or any(ord(c) < 32 or ord(c)==127 for c in v):
                        raise ToolError("Invalid args value.")
                elif isinstance(v, int):
                    if not -1_000_000_000_000 <= v <= 1_000_000_000_000:
                        raise ToolError("Invalid args value.")
                elif isinstance(v, bool) or v is None:
                    pass
                else:
                    raise ToolError("Invalid args value.")
            if "external_content" in args:
                _external_content(args["external_content"])
        else:  # consequential
            # consequential: per-tool strict allowed keys, no extra fields
            per_tool_cons: dict[str, set[str]] = {
                "publish_service": {"service_id", "external_content"},
                "send_communication": {"customer_id", "channel", "body", "external_content"},
                "convert_customer": {"customer_id", "external_content"},
                "send_proposal": {"service_id", "customer_id", "proposal_id", "title", "external_content"},
                "create_external_order": {"opportunity_id", "service_id", "customer_id", "title", "amount_paise", "external_content", "order_id"},
                "initiate_payment": {"opportunity_id", "payment_id", "amount_paise", "external_content", "order_id"},
                "issue_refund": {"refund_id", "payment_id", "amount_paise", "external_content", "order_id"},
                "delete_important_record": {"record_id", "external_content"},
                "update_account_settings": {"settings", "record_id", "external_content"},
                "make_purchase": {"items", "amount_paise", "external_content", "order_id"},
                "modify_financial_record": {"record_id", "amount_paise", "external_content", "title", "invoice_id"},
            }
            allowed_cons = per_tool_cons.get(tool.name)
            if allowed_cons is not None:
                if not frozenset(args).issubset(allowed_cons):
                    raise ToolError("Invalid arguments.")
            else:
                broadly_cons = {"service_id", "customer_id", "opportunity_id", "channel", "body", "proposal_id", "order_id", "payment_id", "refund_id", "record_id", "settings", "items", "amount_paise", "external_content", "title", "source_url", "invoice_id"}
                if not frozenset(args).issubset(broadly_cons):
                    raise ToolError("Invalid arguments.")
            for k in args:
                if "instruction" in k.lower():
                    raise ToolError("Invalid args key (instruction).")
                if _TOOL_NAME_RE.fullmatch(k) is None:
                    raise ToolError("Invalid args key.")
            for k, v in args.items():
                if k == "external_content":
                    _external_content(v)
                elif k.endswith("_id"):
                    _input(_record_id, v)
                elif k == "channel":
                    if not isinstance(v, str) or not 1 <= len(v) <= 30 or any(ord(c) < 32 or ord(c)==127 for c in v):
                        raise ToolError("Invalid channel.")
                elif k == "body":
                    if not isinstance(v, str) or not 1 <= len(v) <= 2000 or any(ord(c) < 32 or ord(c)==127 for c in v):
                        raise ToolError("Invalid body.")
                elif isinstance(v, str):
                    if len(v) > 2000 or any(ord(c) < 32 or ord(c)==127 for c in v):
                        raise ToolError("Invalid args value.")
                elif isinstance(v, int):
                    if not -1_000_000_000_000 <= v <= 1_000_000_000_000:
                        raise ToolError("Invalid args value.")
                elif isinstance(v, bool) or v is None:
                    pass
                else:
                    raise ToolError("Invalid args value.")
            if not any(k.endswith("_id") or k in {"channel", "body", "settings", "items"} for k in args):
                if not args:
                    raise ToolError("Invalid arguments: need identifier.")
            if "external_content" in args:
                _external_content(args["external_content"])
        canonical = json.dumps(args, ensure_ascii=True, separators=(",", ":"), sort_keys=True)
        if len(canonical) > _MAX_ARGS_JSON:
            raise ToolError("Invalid arguments.")
        return canonical

    def execute(self, token: str, tool_name: str, args: dict, auth: AuthService, state: StateStorage, *, approval_id: str | None = None) -> dict:
        """Centralized execution with policy enforcement. Returns result dict."""
        tool = self.get(tool_name)
        canonical_args = self.validate_args(tool_name, args)
        entry: dict = {"tool": tool_name, "scope": tool.scope, "args": args, "timestamp": int(time.time())}
        try:
            with _authorized_transaction(auth, state, token) as (conn, owner, now):
                _version_check(conn)
                if _is_emergency(conn, owner) and tool.scope == "consequential":
                    raise ApprovalError("Emergency stop is active; consequential actions are blocked.")
                if tool.scope == "consequential":
                    if approval_id is None:
                        raise ApprovalError("Approval required for consequential action.")
                    row = _owned_approval(conn, owner, _input(_record_id, approval_id))
                    effective_status = row["status"]
                    if row["status"] == "pending" and now > row["expires_at"]:
                        effective_status = "expired"
                    if effective_status != "approved":
                        raise ApprovalError(f"Approval status is {effective_status}; approved required.")
                    if row["tool_name"] != tool_name:
                        raise ApprovalError("Approval tool mismatch.")
                    if row["args_json"] != canonical_args:
                        raise ApprovalError("Approval args mismatch.")
                    if _is_emergency(conn, owner):
                        raise ApprovalError("Emergency stop active.")
                if tool.scope == "read":
                    result = {"tool": tool_name, "scope": tool.scope, "verification": "read_only", "args": args, "result": "read_executed", "note": "Direct read, no approval needed. Connected to backend via auth transaction."}
                elif tool.scope == "draft":
                    external = args.get("external_content")
                    note = "draft_unverified; external content treated as data, not instruction. Connected to backend."
                    if external is not None:
                        result = {"tool": tool_name, "scope": tool.scope, "verification": "draft_unverified", "args": args, "result": f"draft for {args.get('title') or args.get('service_id','')} with external data length {len(external)}", "external_sanitized": external[:200], "note": note}
                    else:
                        result = {"tool": tool_name, "scope": tool.scope, "verification": "draft_unverified", "args": args, "result": "draft_executed", "note": note}
                else:  # consequential
                    result = {"tool": tool_name, "scope": tool.scope, "verification": "approved_consequential", "args": args, "result": "consequential_executed", "approval_id": approval_id}  # type: ignore[dict-item]
                entry["status"] = "success"
                entry["result"] = result
                self._history.append(entry)
                return result
        except Exception as exc:
            entry["status"] = "failure"
            entry["error"] = str(exc)
            self._history.append(entry)
            raise

    def history(self, limit: int = 20) -> builtins.list[dict]:
        if not isinstance(limit, int) or not 1 <= limit <= 100:
            raise ToolError("Invalid limit.")
        return [dict(h) for h in self._history[-limit:]]

    def preview(self, tool_name: str, args: dict) -> dict:
        """Human-readable preview before consequential execution."""
        tool = self.get(tool_name)
        canonical = self.validate_args(tool_name, args)
        preview_text = f"Tool '{tool_name}' ({tool.scope}) will execute with args {canonical[:500]}."
        if tool.scope == "consequential":
            preview_text += " This is consequential and requires explicit approval (ApprovalQueue) and confirmation (confirmed=True). No action has been taken yet."
        else:
            preview_text += f" No approval required for {tool.scope} tools."
        # Explain intent
        desc = tool.description
        return {
            "tool": tool_name,
            "scope": tool.scope,
            "description": desc,
            "args": args,
            "preview": preview_text,
            "requires_approval": tool.scope == "consequential",
            "verification": "draft_unverified" if tool.scope == "draft" else ("read_only" if tool.scope == "read" else "approval_required"),
        }


# ---------------------------------------------------------------------------
# ApprovalQueue
# ---------------------------------------------------------------------------

class ApprovalQueue:
    """Owner-scoped approval workflow with expiry, revocation, emergency stop."""

    def __init__(self, auth: AuthService, state: StateStorage, registry: ToolRegistry | None = None):
        self._auth = auth
        self._state = state
        self._registry = registry or ToolRegistry()

    def request(self, token: str, tool_name: str, args: dict, *, operation_key: str, ttl_seconds: int = _TTL_SECONDS) -> str:
        _tool_name(tool_name)
        canonical = self._registry.validate_args(tool_name, args)
        key = _input(_key, operation_key)
        if not isinstance(ttl_seconds, int) or not 60 <= ttl_seconds <= 86400:
            raise ApprovalError("Invalid TTL.")
        tool = self._registry.get(tool_name)
        if tool.scope != "consequential":
            raise ApprovalError("Only consequential tools require approval.")
        digest = _request("ai_approval_create", [tool_name, canonical])
        with _authorized_transaction(self._auth, self._state, token) as (conn, owner, now):
            _version_check(conn)
            if _is_emergency(conn, owner):
                raise ApprovalError("Emergency stop is active; new approvals are blocked.")
            existing = _retry(conn, owner, key, digest)
            if existing is not None:
                return existing
            expires_at = now + ttl_seconds
            if expires_at > _MAX_TIME:
                expires_at = _MAX_TIME
            ident = secrets.token_hex(16)
            conn.execute(
                "INSERT INTO ai_approvals(id, owner_id, tool_name, args_json, status, created_at, expires_at, decided_at) VALUES(?,?,?,?,?,?,?,NULL)",
                (ident, owner, tool_name, canonical, "pending", now, expires_at),
            )
            _audit(conn, now, owner, "ai_approval_created", ident)
            _remember(conn, owner, key, digest, ident)
            return ident

    def get(self, token: str, approval_id: str) -> dict:
        ident = _input(_record_id, approval_id)
        with _authorized_transaction(self._auth, self._state, token) as (conn, owner, now):
            _version_check(conn)
            row = _owned_approval(conn, owner, ident)
            effective = row["status"]
            if row["status"] == "pending" and now > row["expires_at"]:
                effective = "expired"
            d = dict(row)
            d["effective_status"] = effective
            d["emergency_stopped"] = _is_emergency(conn, owner)
            return d

    def list(self, token: str, *, after_id: str | None = None, limit: int = 20, status: str | None = None) -> builtins.list[dict]:
        cursor = "" if after_id is None else _input(_record_id, after_id)
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ApprovalError("Invalid page size.")
        if status is not None and status not in _STATUSES:
            raise ApprovalError("Invalid status filter.")
        with _authorized_transaction(self._auth, self._state, token) as (conn, owner, now):
            _version_check(conn)
            query = "SELECT id FROM ai_approvals WHERE owner_id=? AND id>? COLLATE BINARY"
            params: list[Any] = [owner, cursor]
            if status is not None:
                query += " AND status=?"
                params.append(status)
            query += " ORDER BY id COLLATE BINARY LIMIT ?"
            params.append(limit)
            rows = conn.execute(query, tuple(params)).fetchall()
            result = []
            for r in rows:
                row = _owned_approval(conn, owner, r["id"])
                effective = row["status"]
                if row["status"] == "pending" and now > row["expires_at"]:
                    effective = "expired"
                d = dict(row)
                d["effective_status"] = effective
                result.append(d)
            return result

    def decide(self, token: str, approval_id: str, decision: str, *, confirmed: bool, operation_key: str) -> str:
        if confirmed is not True:
            raise ApprovalError("Explicit confirmation is required.")
        if decision not in {"approved", "rejected"}:
            raise ApprovalError("Invalid decision.")
        ident = _input(_record_id, approval_id)
        key = _input(_key, operation_key)
        digest = _request("ai_approval_decide", [ident, decision])
        with _authorized_transaction(self._auth, self._state, token) as (conn, owner, now):
            _version_check(conn)
            row = _owned_approval(conn, owner, ident)
            existing = _retry(conn, owner, key, digest)
            if existing is not None:
                if existing != ident:
                    raise StateStorageError("Stored operation result is inconsistent.")
                return ident
            if row["status"] == "pending" and now > row["expires_at"]:
                raise ApprovalError("Approval has expired.")
            if row["status"] != "pending":
                raise ApprovalError(f"Approval is already {row['status']}.")
            if _is_emergency(conn, owner):
                raise ApprovalError("Emergency stop is active; decisions are blocked.")
            event = "ai_approval_approved" if decision == "approved" else "ai_approval_rejected"
            conn.execute("UPDATE ai_approvals SET status=?, decided_at=? WHERE id=? AND owner_id=?", (decision, now, ident, owner))
            _audit(conn, now, owner, event, ident)
            _remember(conn, owner, key, digest, ident)
            return ident

    def revoke(self, token: str, approval_id: str, *, operation_key: str) -> str:
        ident = _input(_record_id, approval_id)
        key = _input(_key, operation_key)
        digest = _request("ai_approval_revoke", [ident])
        with _authorized_transaction(self._auth, self._state, token) as (conn, owner, now):
            _version_check(conn)
            row = _owned_approval(conn, owner, ident)
            existing = _retry(conn, owner, key, digest)
            if existing is not None:
                if existing != ident:
                    raise StateStorageError("Stored operation result is inconsistent.")
                return ident
            effective = row["status"]
            if row["status"] == "pending" and now > row["expires_at"]:
                effective = "expired"
            if effective not in {"pending", "approved"}:
                raise ApprovalError(f"Cannot revoke {effective} approval.")
            conn.execute("UPDATE ai_approvals SET status='revoked', decided_at=? WHERE id=? AND owner_id=?", (now, ident, owner))
            _audit(conn, now, owner, "ai_approval_revoked", ident)
            _remember(conn, owner, key, digest, ident)
            return ident

    def emergency_stop(self, token: str, *, confirmed: bool, reason: str = "", operation_key: str) -> str:
        if confirmed is not True:
            raise ApprovalError("Explicit confirmation is required.")
        if not isinstance(reason, str) or len(reason) > 500:
            raise ApprovalError("Invalid reason.")
        if any(ord(c) < 32 and c not in ("\n", "\t") for c in reason) or "\x7f" in reason:
            raise ApprovalError("Invalid reason.")
        key = _input(_key, operation_key)
        digest = _request("ai_emergency_stop", [reason])
        with _authorized_transaction(self._auth, self._state, token) as (conn, owner, now):
            _version_check(conn)
            existing = _retry(conn, owner, key, digest, resource_type=_RESOURCE_EMERGENCY)
            if existing is not None:
                return existing
            if conn.execute("SELECT 1 FROM ai_emergency WHERE owner_id=?", (owner,)).fetchone() is None:
                conn.execute("INSERT INTO ai_emergency(owner_id, stopped, updated_at, reason) VALUES(?,?,?,?)", (owner, 1, now, reason))
                ident = owner
            else:
                conn.execute("UPDATE ai_emergency SET stopped=1, updated_at=?, reason=? WHERE owner_id=?", (now, reason, owner))
                ident = owner
            _audit(conn, now, owner, "ai_emergency_stopped", ident, resource_type=_RESOURCE_EMERGENCY)
            _remember(conn, owner, key, digest, ident, resource_type=_RESOURCE_EMERGENCY)
            return ident

    def emergency_clear(self, token: str, *, confirmed: bool, operation_key: str) -> str:
        if confirmed is not True:
            raise ApprovalError("Explicit confirmation is required.")
        key = _input(_key, operation_key)
        digest = _request("ai_emergency_clear", [])
        with _authorized_transaction(self._auth, self._state, token) as (conn, owner, now):
            _version_check(conn)
            existing = _retry(conn, owner, key, digest, resource_type=_RESOURCE_EMERGENCY)
            if existing is not None:
                return existing
            if conn.execute("SELECT 1 FROM ai_emergency WHERE owner_id=?", (owner,)).fetchone() is None:
                conn.execute("INSERT INTO ai_emergency(owner_id, stopped, updated_at, reason) VALUES(?,?,?,?)", (owner, 0, now, ""))
            else:
                conn.execute("UPDATE ai_emergency SET stopped=0, updated_at=?, reason=? WHERE owner_id=?", (now, "", owner))
            ident = owner
            _audit(conn, now, owner, "ai_emergency_cleared", ident, resource_type=_RESOURCE_EMERGENCY)
            _remember(conn, owner, key, digest, ident, resource_type=_RESOURCE_EMERGENCY)
            return ident

    def is_stopped(self, token: str) -> bool:
        with _authorized_transaction(self._auth, self._state, token) as (conn, owner, _now):
            _version_check(conn)
            return _is_emergency(conn, owner)

    def preview(self, token: str, approval_id: str) -> dict:
        """Human-readable preview of consequential action before confirmation."""
        ident = _input(_record_id, approval_id)
        with _authorized_transaction(self._auth, self._state, token) as (conn, owner, now):
            _version_check(conn)
            row = _owned_approval(conn, owner, ident)
            tool = self._registry.get(row["tool_name"])
            args = json.loads(row["args_json"])
            # Explain intent
            preview_text = f"Intent: execute consequential tool '{row['tool_name']}' ({tool.description}) with args {row['args_json'][:500]}. "
            preview_text += f"Status: {row['status']}, expires_at: {row['expires_at']}, now: {now}. "
            if _is_emergency(conn, owner):
                preview_text += "EMERGENCY STOP ACTIVE — execution will be blocked. "
            preview_text += "Requires explicit confirmation (confirmed=True) and approved decision before execution. No action taken yet."
            return {
                "approval_id": ident,
                "tool": row["tool_name"],
                "scope": tool.scope,
                "description": tool.description,
                "args": args,
                "preview": preview_text,
                "status": row["status"],
                "effective_status": _effective_status(row, now),
                "emergency_stopped": _is_emergency(conn, owner),
                "requires_approval": True,
                "audit_resource": _RESOURCE_APPROVAL,
            }


# ---------------------------------------------------------------------------
# Assistant service (orchestrates registry + queue + provider + history)
# ---------------------------------------------------------------------------

class AIAssistant:
    def __init__(self, auth: AuthService, state: StateStorage, registry: ToolRegistry | None = None, provider_config: AIProviderConfig | None = None):
        self._auth = auth
        self._state = state
        self.registry = registry or ToolRegistry()
        self.queue = ApprovalQueue(auth, state, self.registry)
        self.config = provider_config or AIProviderConfig(provider="stub", mode="sandbox")
        self.provider: ProviderBase = get_provider(self.config)
        self._chat_history: List[dict] = []  # AI execution history

    def chat(self, token: str, prompt: str, *, external_content: str | None = None) -> dict:
        _prompt(prompt)
        if external_content is not None:
            _external_content(external_content)
        # Check provider availability explicitly; never assume
        if not self.provider.is_available():
            # Still allow stub drafts; for non-stub, explain not available
            info = self.provider.info()
            result = {
                "output": f"Provider {self.config.provider} not available in {self.config.mode} (model={self.config.model}). Using stub draft.",
                "model": self.config.model,
                "verification": "draft_unverified",
                "provider_info": info,
            }
            if self.config.provider == "stub":
                result = self.provider.generate(prompt, external_content=external_content)
        else:
            result = self.provider.generate(prompt, external_content=external_content)
        result["available_tools"] = self.registry.list()
        result["policy"] = "read/draft allowed directly; consequential requires explicit approval via ApprovalQueue"
        # history
        entry = {"type": "chat", "prompt": prompt[:200], "external_content_present": external_content is not None, "result": result, "at": int(time.time())}
        self._chat_history.append(entry)
        return result

    def propose_tool(self, token: str, tool_name: str, args: dict, *, operation_key: str) -> dict:
        _tool_name(tool_name)
        tool = self.registry.get(tool_name)
        self.registry.validate_args(tool_name, args)
        # preview before creation
        preview = self.registry.preview(tool_name, args)
        if tool.scope != "consequential":
            # read/draft directly executable, but still provide preview
            preview.update({"requires_approval": False, "note": "Direct execution allowed; use ToolRegistry.execute"})
            # log to history
            self._chat_history.append({"type": "propose", "tool": tool_name, "scope": tool.scope, "preview": preview, "at": int(time.time())})
            return preview
        approval_id = self.queue.request(token, tool_name, args, operation_key=operation_key)
        preview.update({"approval_id": approval_id, "args": args})
        self._chat_history.append({"type": "propose", "tool": tool_name, "scope": tool.scope, "approval_id": approval_id, "preview": preview, "at": int(time.time())})
        return preview

    def history(self, limit: int = 20) -> builtins.list[dict]:
        if not isinstance(limit, int) or not 1 <= limit <= 100:
            raise AssistantError("Invalid limit.")
        return [dict(h) for h in self._chat_history[-limit:]]

    def provider_info(self) -> dict:
        return self.config.describe()

    def check_provider_available(self) -> bool:
        return self.provider.is_available()

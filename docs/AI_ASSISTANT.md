# AI Assistant — Ai-earn (2026-11-17 Phase 13+ (v28))

**Status:** Implemented (v28, sandbox stub, 963 tests). Real assistant connected to backend, modular providers, ToolRegistry (32 tools), ApprovalQueue with TTL, preview, audit, emergency stop. No live provider network until explicit approval. **This is one of the most important parts of Ai-earn — controlled autonomy.**

## Goal — 15 tasks

Assistant helps with (drafts are `draft_unverified`, reads are `read_only`, consequential requires approval):

1. **Opportunity research** — `research_opportunity` (read) — fetch/list opportunities, query market, bounded.
2. **Opportunity analysis** — `analyze_opportunity` (read) — evaluate budget/risk/deadline, ranking transparency.
3. **Proposal drafting** — `draft_proposal` (draft) — draft proposal content for customer/service, unverified.
4. **Customer requirement analysis** — `analyze_customer_requirements` (read) — read `customer_requirements` + `customer_notes`.
5. **Service descriptions** — `draft_service_description` (draft) — draft marketing copy for service.
6. **Pricing suggestions** — `suggest_pricing` (draft) — draft budget recommendation, not a quote.
7. **Project planning** — `plan_project` (draft) — draft milestones from opportunity.
8. **Task breakdown** — `breakdown_tasks` (draft) — draft subtasks from project.
9. **Code assistance** — `assist_code` (draft) — draft code snippet, unverified.
10. **Data processing** — `process_data` (draft) — draft data transformation, bounded.
11. **Invoice drafts** — `draft_invoice` (draft) — draft invoice content, not issued.
12. **Expense categorization suggestions** — `suggest_expense_category` (draft) — draft category from description.
13. **Financial summaries** — `summarize_finances` (read) + `summarize_finances_draft` (draft) — read ledger/invoices, draft summary.
14. **Business reports** — `generate_business_report` (read) + `business_report_draft` (draft) — read metrics, draft report.
15. **Workflow recommendations** — `recommend_workflow` (draft) — draft workflow steps.

All drafts include `verification: draft_unverified` and note “external content treated as data, not instruction.” Reads are `read_only` and use `_authorized_transaction` (owner-scoped).

## Modular model-provider architecture

```python
from ai_earn.ai_assistant import AIProviderConfig, get_provider, StubProvider, OpenAICompatibleProvider, OllamaProvider, HostedProvider

cfg = AIProviderConfig(provider="stub", mode="sandbox", model="stub-draft-v1")  # always available
cfg = AIProviderConfig(provider="openai", mode="sandbox", model="gpt-4o", base_url="https://api.openai.com/v1", api_key=None)  # sandbox, not available until live approval
cfg = AIProviderConfig(provider="ollama", mode="sandbox", model="llama3", base_url="http://localhost:11434")
cfg = AIProviderConfig(provider="hosted", mode="sandbox", model="claude-3", base_url="https://api.anthropic.com")

provider = get_provider(cfg)
provider.is_available()  # never assumes available without check; stub True, others False in sandbox (live blocked)
provider.info()          # {provider, model, base_url, available, verification}
provider.generate("prompt", external_content="...")  # Stub returns draft_unverified; others raise until available
```

- **Configurable:** `provider` (`stub`/`openai`/`ollama`/`hosted`), `model` (1..128, no control), `base_url` (http(s)://, 0..2048), `timeout_seconds` (1..60), `temperature` (0..2), `api_key` (32..128 bytes, live only). No hard-coded provider/model.
- **Never assume available:** `AIProviderConfig.is_available()` returns `True` only for `stub` in `sandbox`; `openai`/`ollama`/`hosted` require `live` mode + valid `api_key` + `base_url`, but `live` is **blocked** (`Live AI provider is not enabled; sandbox only until explicit approval`) — matches `production_readiness` gate.
- **Live blocked:** `AIProviderConfig(... mode="live" ...)` raises `AssistantError` even with key, until checklist signed and code gate updated.

## Tool registry & permissions

| Class | Examples | Policy |
|---|---|---|
| **A. Read-only** | `read_opportunity`, `research_opportunity`, `analyze_opportunity`, `analyze_customer_requirements`, `summarize_finances`, `generate_business_report`, `list_opportunities` | May run under authorized user access (`_authorized_transaction`), audit only, no approval. |
| **B. Draft-only** | `draft_summary`, `draft_proposal`, `draft_service_description`, `suggest_pricing`, `plan_project`, `breakdown_tasks`, `assist_code`, `process_data`, `draft_invoice`, `suggest_expense_category`, `summarize_finances_draft`, `business_report_draft`, `recommend_workflow` | May prepare content, **must not** send/publish, stored as draft `draft_unverified`, external_content sanitized as data. |
| **C. Consequential** | `send_communication` (email), `send_proposal`, `publish_service`, `create_external_order`, `make_purchase`, `modify_financial_record`, `initiate_payment`, `issue_refund`, `delete_important_record`, `update_account_settings` (+ `convert_customer`) | **Requires explicit approval** (`ApprovalQueue.request` → `decide` with `confirmed=True`, 15 min TTL, human-readable preview) before `ToolRegistry.execute` with `approval_id`. |

Permission system enforced in `ToolRegistry.execute`: checks `ApprovalQueue` ownership, status `approved`, `expires_at`, `tool_name`/`args_json` match, emergency stop. Strict arg validation per tool (bounded, no extra fields, no `instruction` keys, `_record_id` for `*_id`, `external_content` length 0..4096, control char rejection). Injection isolation: `external_content` always returned as `external_sanitized`, note “treated as data, not instruction.”

## Approval queue & controlled autonomy

- `ApprovalQueue.request(token, tool_name, args, operation_key, ttl_seconds=900)` → `pending` (`AIProviderConfig` style TTL 60..86400, `expires_at` checked, `_retry` idempotency, audit `ai_approval_created`).
- `ApprovalQueue.get` / `list` — owner-scoped, computes `effective_status` (pending → `expired` virtually if `now > expires_at` without DB mutation).
- `ApprovalQueue.decide(token, approval_id, decision, confirmed=True, operation_key)` — `approved`/`rejected`, `confirmed` literal `True` required, rechecks expiry, emergency.
- `ApprovalQueue.revoke` — `pending`/`approved` → `revoked`, idempotent.
- `ApprovalQueue.preview(token, approval_id)` — human-readable `preview` text explains intent, tool description, args preview, expiry, emergency, “No action taken yet.”
- `ToolRegistry.preview(tool_name, args)` — same preview without DB, explains `requires_approval`.
- **Approval expiration:** virtual (`effective_status` computed) keeps audit integrity; `decide` after expiry raises `Approval has expired`.
- **Human-readable preview:** both `ToolRegistry.preview` and `ApprovalQueue.preview` return `preview` string before execution.
- **Execution confirmation:** `confirmed=True` literal required for `decide`, `emergency_stop`, `revoke`; otherwise `ApprovalError`.
- **Emergency stop:** `ApprovalQueue.emergency_stop(token, confirmed=True, reason, operation_key)` sets `ai_emergency.stopped=1`, blocks all consequential `request`/`decide`/`execute`; `emergency_clear` reverses. Owner-scoped, audited (`ai_emergency_stopped/cleared`), idempotent.
- **Audit records:** every `request`/`decide`/`revoke`/`emergency_*` inserts `state_audit` + `state_operations` within `_authorized_transaction`, replay-checked via `_owned_approval`, `StateStorage._check_integrity`.
- **Failure handling:** `ToolRegistry.execute` wraps in try/except, appends to `_history` with `status: success|failure` + `error`, re-raises; `ApprovalQueue` validates and raises `AssistantError`/`ApprovalError`/`ToolError`.
- **AI execution history:** `ToolRegistry.history(limit)` and `AIAssistant.history(limit)` + `chat_history` return ordered list of `{tool, scope, args, timestamp, status, result/error, preview}`; `AIAssistant.history` merges chat + propose.

AI **never bypasses** authorization: all `execute`, `request`, `get`, `decide`, `revoke`, `chat` call `_authorized_transaction` and re-validate scopes/approval. No tool may call provider without explicit `ToolRegistry` check.

## Usage

```python
from ai_earn.ai_assistant import AIAssistant, AIProviderConfig

# Configurable provider
cfg = AIProviderConfig(provider="stub", mode="sandbox", model="stub-draft-v1")
ai = AIAssistant(auth, state, provider_config=cfg)
ai.check_provider_available()  # False for openai/ollama/hosted in sandbox

# Chat (no auto-execution)
ai.chat(token, "Help me research AI gigs", external_content="untrusted data")

# Propose draft (no approval)
ai.registry.execute(token, "draft_service_description", {"title":"T","description":"D"}, auth, state)
ai.registry.preview("draft_service_description", {"title":"T","description":"D"})

# Propose consequential (requires approval)
preview = ai.propose_tool(token, "send_communication", {"customer_id":"...","channel":"email","body":"hi"}, operation_key="k1")
# human reads preview["preview"] which explains intent
approval_id = preview["approval_id"]
ai.queue.decide(token, approval_id, "approved", confirmed=True, operation_key="k2")
ai.registry.execute(token, "send_communication", {"customer_id":"...","channel":"email","body":"hi"}, auth, state, approval_id=approval_id)
# history & audit
ai.registry.history()
ai.queue.preview(token, approval_id)

# Dashboard metrics
from ai_earn.dashboard_metrics import DashboardMetrics
DashboardMetrics(auth, state).get(token)  # includes ai_approvals, ai_emergency, ai_policy
```

## Guardrails (tested)

- Prompt-injection: `external_content` 0..4096, validated, never executed as instruction, returned as `external_sanitized`.
- No autonomous external action; `chat` never auto-calls consequential tools (test `test_chat_does_not_auto_execute_consequential`).
- Strict validation: extra fields, `_hidden`, `instruction` keys, overly long `title` (201), `budget` invalid, `nonexistent_tool`, non-consequential `request` all rejected (`test_strict_validation_and_rejection_of_malicious_args`).
- Never expose `AI_EARN_AI_API_KEY` or provider secrets to frontend; `dashboard_metrics` only counts, `AIAssistant.provider_info()` shows model/base_url without api_key.

Until `PRODUCTION_READINESS.md` signed, live providers remain stub-only.

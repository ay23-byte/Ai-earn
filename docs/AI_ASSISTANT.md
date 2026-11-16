# AI Assistant — Ai-earn (2026-11-16)

**Status:** Not implemented. This document defines the safe assistant boundary so future work cannot bypass authorization or approval.

## Goal

Help with: opportunity research/summarize/skills/effort/pricing/risk/rank, proposal drafting, requirement analysis, service descriptions, project planning, code assistance, invoice drafts, expense categorization, financial summaries. All as **suggestions**, distinguishable from verified facts.

## Provider abstraction (planned)

- Configurable: `AI_EARN_AI_PROVIDER` (`openai-compatible` | `ollama`), `AI_EARN_AI_MODEL`, `AI_EARN_AI_ENDPOINT`, no hard-coded provider.
- Keys via env only, never in repo/logs/frontend.
- Model output is **untrusted data**; treat as draft.

## Tool registry & permissions (to be implemented in M12)

| Class | Examples | Policy |
|---|---|---|
| **A. Read-only** | `search_opportunities`, `read_customer`, `get_metrics` | May run under authorized user access, audit only |
| **B. Draft-only** | `draft_proposal`, `draft_invoice`, `summarize_requirements` | May prepare content, must **not** send/publish, stored as draft |
| **C. Consequential** | `send_email`, `send_proposal`, `publish_portfolio`, `create_order`, `initiate_payment`, `issue_invoice`, `modify_ledger`, `delete_record` | **Requires explicit approval** before execution |

## Approval queue (planned)

- `Approval {id, tool, args_preview, requested_at, expires_at: +15m, status: pending|approved|rejected|expired, actor_id}`
- Human-readable preview before consequential execution, `confirmed=True` required, audit record + `state_operations` binding, emergency stop (`POST /approvals/stop`).
- No tool may bypass `state_authorization` or `ApprovalQueue`.

## History & audit

- `ai_execution_history` (future table) with `prompt`, `tool`, `args`, `output`, `approval_id`, not tamper-evident. All consequential actions have corresponding `state_audit` entry.

## Guardrails (must be tested)

- Prompt-injection: treat external content (opportunity description, customer email) as data, never as authority; validate tool args; reject `tool` not in allowlist.
- No autonomous external action; no background worker without approval.
- Never expose `AI_EARN_AI_API_KEY` or provider response headers to frontend.

## Milestone

- **M12** after connectors (M11). Until then, AI may only be used offline (manual prompt) without tool calls. No code in `ai_earn/` may call a model before `ToolRegistry` + `ApprovalQueue` exist.

"""State schema migration v29; keep DDL and index definitions stable.

Adds: AI provider configuration (hash only, never a raw key), AI token-usage
metering, and settlement-reconciliation evidence for manual verification.
"""

import sqlite3
import time

_V29_AI_CONFIG_INDEX = "CREATE UNIQUE INDEX ai_provider_configs_owner_idx ON ai_provider_configs(owner_id)"
_V29_AI_USAGE_INDEX = "CREATE INDEX ai_token_usage_owner_idx ON ai_token_usage(owner_id, created_at)"
_V29_SETTLEMENT_INDEX = "CREATE UNIQUE INDEX settlement_evidence_payment_idx ON settlement_evidence(payment_id, owner_id)"

_V29_SCHEMA = {
    "ai_provider_configs": """CREATE TABLE ai_provider_configs (
    owner_id TEXT PRIMARY KEY NOT NULL CHECK(length(owner_id)=32 AND owner_id NOT GLOB '*[^0-9a-f]*'),
    provider TEXT NOT NULL CHECK(length(provider) BETWEEN 1 AND 32 AND provider NOT GLOB '*[^a-z0-9_.-]*'),
    model TEXT NOT NULL CHECK(length(model) BETWEEN 1 AND 128),
    base_url TEXT NOT NULL CHECK(length(base_url) BETWEEN 0 AND 512),
    api_key_hash BLOB CHECK(api_key_hash IS NULL OR length(api_key_hash)=32),
    created_at INTEGER NOT NULL CHECK(typeof(created_at)='integer' AND created_at >= 0),
    updated_at INTEGER NOT NULL CHECK(typeof(updated_at)='integer' AND updated_at >= created_at)
)""",
    "ai_token_usage": """CREATE TABLE ai_token_usage (
    id TEXT PRIMARY KEY NOT NULL CHECK(length(id)=32 AND id NOT GLOB '*[^0-9a-f]*'),
    owner_id TEXT NOT NULL CHECK(length(owner_id)=32 AND owner_id NOT GLOB '*[^0-9a-f]*'),
    provider TEXT NOT NULL CHECK(length(provider) BETWEEN 1 AND 32),
    model TEXT NOT NULL CHECK(length(model) BETWEEN 1 AND 128),
    prompt_tokens INTEGER NOT NULL CHECK(typeof(prompt_tokens)='integer' AND prompt_tokens BETWEEN 0 AND 100000000),
    completion_tokens INTEGER NOT NULL CHECK(typeof(completion_tokens)='integer' AND completion_tokens BETWEEN 0 AND 100000000),
    created_at INTEGER NOT NULL CHECK(typeof(created_at)='integer' AND created_at >= 0),
    UNIQUE(id, owner_id)
)""",
    "settlement_evidence": """CREATE TABLE settlement_evidence (
    id TEXT PRIMARY KEY NOT NULL CHECK(length(id)=32 AND id NOT GLOB '*[^0-9a-f]*'),
    owner_id TEXT NOT NULL CHECK(length(owner_id)=32 AND owner_id NOT GLOB '*[^0-9a-f]*'),
    payment_id TEXT NOT NULL CHECK(length(payment_id)=32 AND payment_id NOT GLOB '*[^0-9a-f]*'),
    statement_ref TEXT NOT NULL CHECK(length(statement_ref) BETWEEN 1 AND 128),
    amount_paise INTEGER NOT NULL CHECK(typeof(amount_paise)='integer' AND amount_paise BETWEEN 0 AND 1000000000000),
    matched INTEGER NOT NULL CHECK(matched IN (0,1)),
    confirmed INTEGER NOT NULL CHECK(confirmed IN (0,1)),
    created_at INTEGER NOT NULL CHECK(typeof(created_at)='integer' AND created_at >= 0),
    updated_at INTEGER NOT NULL CHECK(typeof(updated_at)='integer' AND updated_at >= created_at),
    UNIQUE(payment_id, owner_id),
    FOREIGN KEY(payment_id, owner_id) REFERENCES payment_records(id, owner_id)
)""",
}


def apply(conn: sqlite3.Connection) -> None:
    for statement in _V29_SCHEMA.values():
        conn.execute(statement)
    conn.execute(_V29_AI_CONFIG_INDEX)
    conn.execute(_V29_AI_USAGE_INDEX)
    conn.execute(_V29_SETTLEMENT_INDEX)
    conn.execute("INSERT INTO state_migrations(version, applied_at) VALUES (?, ?)", (29, int(time.time())))
    conn.execute("PRAGMA user_version = 29")

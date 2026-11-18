"""State schema migration v26; keep DDL and index definitions stable."""

import sqlite3
import time

_V26_IDEA_INDEX = "CREATE UNIQUE INDEX product_ideas_id_owner_idx ON product_ideas(id, owner_id)"

_V26_VALIDATION_INDEX = "CREATE INDEX product_validations_product_idx ON product_validations(product_id, owner_id)"

_V26_RESEARCH_INDEX = "CREATE INDEX market_research_product_idx ON market_research(product_id, owner_id)"

_V26_PRICING_INDEX = "CREATE INDEX pricing_suggestions_product_idx ON pricing_suggestions(product_id, owner_id)"

_V26_VERSION_INDEX = "CREATE INDEX product_versions_product_idx ON product_versions(product_id, owner_id)"

_V26_FILE_INDEX = "CREATE INDEX product_files_product_idx ON product_files(product_id, owner_id)"

_V26_SALES_INDEX = "CREATE INDEX product_sales_product_idx ON product_sales(product_id, owner_id)"

_V26_DELIVERABLE_INDEX = "CREATE INDEX product_deliverables_product_idx ON product_deliverables(product_id, owner_id)"

_V26_DOCS_INDEX = "CREATE INDEX product_documentation_product_idx ON product_documentation(product_id, owner_id)"

_V26_SCHEMA = {
    "product_ideas": """CREATE TABLE product_ideas (
    id TEXT PRIMARY KEY NOT NULL CHECK(length(id)=32 AND id NOT GLOB '*[^0-9a-f]*'),
    owner_id TEXT NOT NULL CHECK(length(owner_id)=32 AND owner_id NOT GLOB '*[^0-9a-f]*'),
    title TEXT NOT NULL CHECK(length(title) BETWEEN 1 AND 200 AND length(trim(title)) > 0),
    description TEXT NOT NULL CHECK(length(description) BETWEEN 0 AND 2000),
    status TEXT NOT NULL CHECK(status IN ('idea','validated','rejected','archived')),
    created_at INTEGER NOT NULL CHECK(typeof(created_at)='integer' AND created_at >= 0),
    updated_at INTEGER NOT NULL CHECK(typeof(updated_at)='integer' AND updated_at >= created_at)
)""",
    "product_validations": """CREATE TABLE product_validations (
    id TEXT PRIMARY KEY NOT NULL CHECK(length(id)=32 AND id NOT GLOB '*[^0-9a-f]*'),
    owner_id TEXT NOT NULL CHECK(length(owner_id)=32 AND owner_id NOT GLOB '*[^0-9a-f]*'),
    product_id TEXT NOT NULL CHECK(length(product_id)=32 AND product_id NOT GLOB '*[^0-9a-f]*'),
    criteria TEXT NOT NULL CHECK(length(criteria) BETWEEN 1 AND 500),
    result TEXT NOT NULL CHECK(result IN ('pass','fail','pending')),
    evidence TEXT NOT NULL CHECK(length(evidence) BETWEEN 0 AND 1000),
    created_at INTEGER NOT NULL CHECK(typeof(created_at)='integer' AND created_at >= 0),
    FOREIGN KEY(product_id, owner_id) REFERENCES products(id, owner_id)
)""",
    "market_research": """CREATE TABLE market_research (
    id TEXT PRIMARY KEY NOT NULL CHECK(length(id)=32 AND id NOT GLOB '*[^0-9a-f]*'),
    owner_id TEXT NOT NULL CHECK(length(owner_id)=32 AND owner_id NOT GLOB '*[^0-9a-f]*'),
    product_id TEXT NOT NULL CHECK(length(product_id)=32 AND product_id NOT GLOB '*[^0-9a-f]*'),
    query TEXT NOT NULL CHECK(length(query) BETWEEN 1 AND 500),
    findings TEXT NOT NULL CHECK(length(findings) BETWEEN 1 AND 5000),
    evidence_url TEXT NOT NULL CHECK(length(evidence_url) BETWEEN 0 AND 2048 AND (evidence_url='' OR evidence_url GLOB 'http://*' OR evidence_url GLOB 'https://*')),
    ai_generated INTEGER NOT NULL CHECK(ai_generated IN (0,1)),
    verification TEXT NOT NULL CHECK(verification IN ('unverified','verified')),
    created_at INTEGER NOT NULL CHECK(typeof(created_at)='integer' AND created_at >= 0),
    FOREIGN KEY(product_id, owner_id) REFERENCES products(id, owner_id)
)""",
    "pricing_suggestions": """CREATE TABLE pricing_suggestions (
    id TEXT PRIMARY KEY NOT NULL CHECK(length(id)=32 AND id NOT GLOB '*[^0-9a-f]*'),
    owner_id TEXT NOT NULL CHECK(length(owner_id)=32 AND owner_id NOT GLOB '*[^0-9a-f]*'),
    product_id TEXT NOT NULL CHECK(length(product_id)=32 AND product_id NOT GLOB '*[^0-9a-f]*'),
    price_paise INTEGER NOT NULL CHECK(typeof(price_paise)='integer' AND price_paise BETWEEN 0 AND 1000000000000),
    rationale TEXT NOT NULL CHECK(length(rationale) BETWEEN 1 AND 2000),
    ai_generated INTEGER NOT NULL CHECK(ai_generated IN (0,1)),
    evidence TEXT NOT NULL CHECK(length(evidence) BETWEEN 0 AND 1000),
    created_at INTEGER NOT NULL CHECK(typeof(created_at)='integer' AND created_at >= 0),
    FOREIGN KEY(product_id, owner_id) REFERENCES products(id, owner_id)
)""",
    "product_versions": """CREATE TABLE product_versions (
    id TEXT PRIMARY KEY NOT NULL CHECK(length(id)=32 AND id NOT GLOB '*[^0-9a-f]*'),
    owner_id TEXT NOT NULL CHECK(length(owner_id)=32 AND owner_id NOT GLOB '*[^0-9a-f]*'),
    product_id TEXT NOT NULL CHECK(length(product_id)=32 AND product_id NOT GLOB '*[^0-9a-f]*'),
    version TEXT NOT NULL CHECK(length(version) BETWEEN 1 AND 20 AND version NOT GLOB '*[^A-Za-z0-9._-]*'),
    notes TEXT NOT NULL CHECK(length(notes) BETWEEN 0 AND 2000),
    created_at INTEGER NOT NULL CHECK(typeof(created_at)='integer' AND created_at >= 0),
    FOREIGN KEY(product_id, owner_id) REFERENCES products(id, owner_id)
)""",
    "product_files": """CREATE TABLE product_files (
    id TEXT PRIMARY KEY NOT NULL CHECK(length(id)=32 AND id NOT GLOB '*[^0-9a-f]*'),
    owner_id TEXT NOT NULL CHECK(length(owner_id)=32 AND owner_id NOT GLOB '*[^0-9a-f]*'),
    product_id TEXT NOT NULL CHECK(length(product_id)=32 AND product_id NOT GLOB '*[^0-9a-f]*'),
    file_name TEXT NOT NULL CHECK(length(file_name) BETWEEN 1 AND 200 AND length(trim(file_name)) > 0),
    file_hash BLOB CHECK(file_hash IS NULL OR (typeof(file_hash)='blob' AND length(file_hash)=32)),
    file_size INTEGER NOT NULL CHECK(typeof(file_size)='integer' AND file_size BETWEEN 0 AND 104857600),
    mime_type TEXT NOT NULL CHECK(length(mime_type) BETWEEN 1 AND 100),
    created_at INTEGER NOT NULL CHECK(typeof(created_at)='integer' AND created_at >= 0),
    FOREIGN KEY(product_id, owner_id) REFERENCES products(id, owner_id)
)""",
    "product_sales": """CREATE TABLE product_sales (
    id TEXT PRIMARY KEY NOT NULL CHECK(length(id)=32 AND id NOT GLOB '*[^0-9a-f]*'),
    owner_id TEXT NOT NULL CHECK(length(owner_id)=32 AND owner_id NOT GLOB '*[^0-9a-f]*'),
    product_id TEXT NOT NULL CHECK(length(product_id)=32 AND product_id NOT GLOB '*[^0-9a-f]*'),
    quantity INTEGER NOT NULL CHECK(typeof(quantity)='integer' AND quantity BETWEEN 1 AND 1000000),
    amount_paise INTEGER NOT NULL CHECK(typeof(amount_paise)='integer' AND amount_paise BETWEEN 0 AND 1000000000000),
    currency TEXT NOT NULL CHECK(currency='INR'),
    sale_at INTEGER NOT NULL CHECK(typeof(sale_at)='integer' AND sale_at >= 0),
    evidence_url TEXT NOT NULL CHECK(length(evidence_url) BETWEEN 0 AND 2048 AND (evidence_url='' OR evidence_url GLOB 'http://*' OR evidence_url GLOB 'https://*')),
    verification TEXT NOT NULL CHECK(verification IN ('unverified','verified')),
    created_at INTEGER NOT NULL CHECK(typeof(created_at)='integer' AND created_at >= 0),
    FOREIGN KEY(product_id, owner_id) REFERENCES products(id, owner_id)
)""",
    "product_deliverables": """CREATE TABLE product_deliverables (
    id TEXT PRIMARY KEY NOT NULL CHECK(length(id)=32 AND id NOT GLOB '*[^0-9a-f]*'),
    owner_id TEXT NOT NULL CHECK(length(owner_id)=32 AND owner_id NOT GLOB '*[^0-9a-f]*'),
    product_id TEXT NOT NULL CHECK(length(product_id)=32 AND product_id NOT GLOB '*[^0-9a-f]*'),
    file_name TEXT NOT NULL CHECK(length(file_name) BETWEEN 1 AND 200 AND length(trim(file_name)) > 0),
    file_hash BLOB CHECK(file_hash IS NULL OR (typeof(file_hash)='blob' AND length(file_hash)=32)),
    file_size INTEGER NOT NULL CHECK(typeof(file_size)='integer' AND file_size BETWEEN 0 AND 104857600),
    mime_type TEXT NOT NULL CHECK(length(mime_type) BETWEEN 1 AND 100),
    created_at INTEGER NOT NULL CHECK(typeof(created_at)='integer' AND created_at >= 0),
    FOREIGN KEY(product_id, owner_id) REFERENCES products(id, owner_id)
)""",
    "product_documentation": """CREATE TABLE product_documentation (
    id TEXT PRIMARY KEY NOT NULL CHECK(length(id)=32 AND id NOT GLOB '*[^0-9a-f]*'),
    owner_id TEXT NOT NULL CHECK(length(owner_id)=32 AND owner_id NOT GLOB '*[^0-9a-f]*'),
    product_id TEXT NOT NULL CHECK(length(product_id)=32 AND product_id NOT GLOB '*[^0-9a-f]*'),
    title TEXT NOT NULL CHECK(length(title) BETWEEN 1 AND 200 AND length(trim(title)) > 0),
    content TEXT NOT NULL CHECK(length(content) BETWEEN 1 AND 10000),
    created_at INTEGER NOT NULL CHECK(typeof(created_at)='integer' AND created_at >= 0),
    updated_at INTEGER NOT NULL CHECK(typeof(updated_at)='integer' AND updated_at >= created_at),
    FOREIGN KEY(product_id, owner_id) REFERENCES products(id, owner_id)
)""",
}

def apply(conn: sqlite3.Connection) -> None:
    for statement in _V26_SCHEMA.values():
        conn.execute(statement)
    conn.execute(_V26_IDEA_INDEX)
    conn.execute(_V26_VALIDATION_INDEX)
    conn.execute(_V26_RESEARCH_INDEX)
    conn.execute(_V26_PRICING_INDEX)
    conn.execute(_V26_VERSION_INDEX)
    conn.execute(_V26_FILE_INDEX)
    conn.execute(_V26_SALES_INDEX)
    conn.execute(_V26_DELIVERABLE_INDEX)
    conn.execute(_V26_DOCS_INDEX)
    conn.execute("INSERT INTO state_migrations(version, applied_at) VALUES (?, ?)", (26, int(time.time())))
    conn.execute("PRAGMA user_version = 26")

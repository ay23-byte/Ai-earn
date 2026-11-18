"""State schema migration v28; keep DDL and index definitions stable."""

import sqlite3
import time

_V28_TAX_INDEX = "CREATE UNIQUE INDEX tax_configs_owner_idx ON tax_configs(owner_id)"

_V28_BILLING_ID_INDEX = "CREATE UNIQUE INDEX billing_profiles_id_owner_idx ON billing_profiles(id, owner_id)"

_V28_BILLING_CUSTOMER_INDEX = "CREATE INDEX billing_profiles_customer_idx ON billing_profiles(customer_id, owner_id)"

_V28_PAYMENT_ID_INDEX = "CREATE UNIQUE INDEX payment_records_id_owner_idx ON payment_records(id, owner_id)"

_V28_PAYMENT_INVOICE_INDEX = "CREATE INDEX payment_records_invoice_idx ON payment_records(invoice_id, owner_id)"

_V28_PAYMENT_STATUS_INDEX = "CREATE INDEX payment_records_status_idx ON payment_records(status, owner_id)"

_V28_EXPENSE_ID_INDEX = "CREATE UNIQUE INDEX expense_records_id_owner_idx ON expense_records(id, owner_id)"

_V28_REFUND_ID_INDEX = "CREATE UNIQUE INDEX refund_records_id_owner_idx ON refund_records(id, owner_id)"

_V28_REFUND_PAYMENT_INDEX = "CREATE INDEX refund_records_payment_idx ON refund_records(payment_id, owner_id)"

_V28_FEE_ID_INDEX = "CREATE UNIQUE INDEX fee_records_id_owner_idx ON fee_records(id, owner_id)"

_V28_FEE_PAYMENT_INDEX = "CREATE INDEX fee_records_payment_idx ON fee_records(payment_id, owner_id)"

_V28_SCHEMA = {
    "tax_configs": """CREATE TABLE tax_configs (
    owner_id TEXT PRIMARY KEY NOT NULL CHECK(length(owner_id)=32 AND owner_id NOT GLOB '*[^0-9a-f]*'),
    jurisdiction TEXT NOT NULL CHECK(length(jurisdiction) BETWEEN 1 AND 100 AND length(trim(jurisdiction)) > 0),
    rate_bps INTEGER NOT NULL CHECK(typeof(rate_bps)='integer' AND rate_bps BETWEEN 0 AND 10000),
    currency TEXT NOT NULL CHECK(currency='INR'),
    created_at INTEGER NOT NULL CHECK(typeof(created_at)='integer' AND created_at >= 0),
    updated_at INTEGER NOT NULL CHECK(typeof(updated_at)='integer' AND updated_at >= created_at)
)""",
    "billing_profiles": """CREATE TABLE billing_profiles (
    id TEXT PRIMARY KEY NOT NULL CHECK(length(id)=32 AND id NOT GLOB '*[^0-9a-f]*'),
    owner_id TEXT NOT NULL CHECK(length(owner_id)=32 AND owner_id NOT GLOB '*[^0-9a-f]*'),
    customer_id TEXT NOT NULL CHECK(length(customer_id)=32 AND customer_id NOT GLOB '*[^0-9a-f]*'),
    billing_name TEXT NOT NULL CHECK(length(billing_name) BETWEEN 1 AND 200 AND length(trim(billing_name)) > 0),
    billing_contact TEXT NOT NULL CHECK(length(billing_contact) BETWEEN 1 AND 500),
    billing_address TEXT NOT NULL CHECK(length(billing_address) BETWEEN 0 AND 1000),
    tax_id TEXT NOT NULL CHECK(length(tax_id) BETWEEN 0 AND 100),
    created_at INTEGER NOT NULL CHECK(typeof(created_at)='integer' AND created_at >= 0),
    updated_at INTEGER NOT NULL CHECK(typeof(updated_at)='integer' AND updated_at >= created_at),
    UNIQUE(customer_id, owner_id),
    FOREIGN KEY(customer_id, owner_id) REFERENCES customers(id, owner_id)
)""",
    "payment_records": """CREATE TABLE payment_records (
    id TEXT PRIMARY KEY NOT NULL CHECK(length(id)=32 AND id NOT GLOB '*[^0-9a-f]*'),
    owner_id TEXT NOT NULL CHECK(length(owner_id)=32 AND owner_id NOT GLOB '*[^0-9a-f]*'),
    invoice_id TEXT CHECK(invoice_id IS NULL OR (length(invoice_id)=32 AND invoice_id NOT GLOB '*[^0-9a-f]*')),
    order_id TEXT CHECK(order_id IS NULL OR (length(order_id)=32 AND order_id NOT GLOB '*[^0-9a-f]*')),
    amount_paise INTEGER NOT NULL CHECK(typeof(amount_paise)='integer' AND amount_paise BETWEEN 0 AND 1000000000000),
    currency TEXT NOT NULL CHECK(currency='INR'),
    status TEXT NOT NULL CHECK(status IN ('initiated','received','settled','failed','refunded')),
    provider TEXT NOT NULL CHECK(provider IN ('manual','razorpay','stripe','sandbox') AND length(provider) BETWEEN 1 AND 32),
    provider_ref TEXT NOT NULL CHECK(length(provider_ref) BETWEEN 0 AND 128),
    verified INTEGER NOT NULL CHECK(verified IN (0,1)),
    created_at INTEGER NOT NULL CHECK(typeof(created_at)='integer' AND created_at >= 0),
    updated_at INTEGER NOT NULL CHECK(typeof(updated_at)='integer' AND updated_at >= created_at),
    FOREIGN KEY(invoice_id, owner_id) REFERENCES invoices(id, owner_id)
)""",
    "expense_records": """CREATE TABLE expense_records (
    id TEXT PRIMARY KEY NOT NULL CHECK(length(id)=32 AND id NOT GLOB '*[^0-9a-f]*'),
    owner_id TEXT NOT NULL CHECK(length(owner_id)=32 AND owner_id NOT GLOB '*[^0-9a-f]*'),
    amount_paise INTEGER NOT NULL CHECK(typeof(amount_paise)='integer' AND amount_paise BETWEEN 0 AND 1000000000000),
    currency TEXT NOT NULL CHECK(currency='INR'),
    category TEXT NOT NULL CHECK(length(category) BETWEEN 1 AND 100 AND length(trim(category)) > 0),
    note TEXT NOT NULL CHECK(length(note) BETWEEN 0 AND 500),
    incurred_at INTEGER NOT NULL CHECK(typeof(incurred_at)='integer' AND incurred_at BETWEEN 0 AND 4102444800),
    verified INTEGER NOT NULL CHECK(verified IN (0,1)),
    created_at INTEGER NOT NULL CHECK(typeof(created_at)='integer' AND created_at >= 0),
    updated_at INTEGER NOT NULL CHECK(typeof(updated_at)='integer' AND updated_at >= created_at)
)""",
    "refund_records": """CREATE TABLE refund_records (
    id TEXT PRIMARY KEY NOT NULL CHECK(length(id)=32 AND id NOT GLOB '*[^0-9a-f]*'),
    owner_id TEXT NOT NULL CHECK(length(owner_id)=32 AND owner_id NOT GLOB '*[^0-9a-f]*'),
    payment_id TEXT NOT NULL CHECK(length(payment_id)=32 AND payment_id NOT GLOB '*[^0-9a-f]*'),
    amount_paise INTEGER NOT NULL CHECK(typeof(amount_paise)='integer' AND amount_paise BETWEEN 0 AND 1000000000000),
    currency TEXT NOT NULL CHECK(currency='INR'),
    reason TEXT NOT NULL CHECK(length(reason) BETWEEN 0 AND 500),
    verified INTEGER NOT NULL CHECK(verified IN (0,1)),
    created_at INTEGER NOT NULL CHECK(typeof(created_at)='integer' AND created_at >= 0),
    updated_at INTEGER NOT NULL CHECK(typeof(updated_at)='integer' AND updated_at >= created_at),
    FOREIGN KEY(payment_id, owner_id) REFERENCES payment_records(id, owner_id)
)""",
    "fee_records": """CREATE TABLE fee_records (
    id TEXT PRIMARY KEY NOT NULL CHECK(length(id)=32 AND id NOT GLOB '*[^0-9a-f]*'),
    owner_id TEXT NOT NULL CHECK(length(owner_id)=32 AND owner_id NOT GLOB '*[^0-9a-f]*'),
    payment_id TEXT CHECK(payment_id IS NULL OR (length(payment_id)=32 AND payment_id NOT GLOB '*[^0-9a-f]*')),
    amount_paise INTEGER NOT NULL CHECK(typeof(amount_paise)='integer' AND amount_paise BETWEEN 0 AND 1000000000000),
    currency TEXT NOT NULL CHECK(currency='INR'),
    fee_type TEXT NOT NULL CHECK(fee_type IN ('platform','gateway','other') AND length(fee_type) BETWEEN 1 AND 32),
    note TEXT NOT NULL CHECK(length(note) BETWEEN 0 AND 500),
    verified INTEGER NOT NULL CHECK(verified IN (0,1)),
    created_at INTEGER NOT NULL CHECK(typeof(created_at)='integer' AND created_at >= 0),
    updated_at INTEGER NOT NULL CHECK(typeof(updated_at)='integer' AND updated_at >= created_at),
    FOREIGN KEY(payment_id, owner_id) REFERENCES payment_records(id, owner_id)
)""",
}

def apply(conn: sqlite3.Connection) -> None:
    for statement in _V28_SCHEMA.values():
        conn.execute(statement)
    conn.execute(_V28_TAX_INDEX)
    conn.execute(_V28_BILLING_ID_INDEX)
    conn.execute(_V28_BILLING_CUSTOMER_INDEX)
    conn.execute(_V28_PAYMENT_ID_INDEX)
    conn.execute(_V28_PAYMENT_INVOICE_INDEX)
    conn.execute(_V28_PAYMENT_STATUS_INDEX)
    conn.execute(_V28_EXPENSE_ID_INDEX)
    conn.execute(_V28_REFUND_ID_INDEX)
    conn.execute(_V28_REFUND_PAYMENT_INDEX)
    conn.execute(_V28_FEE_ID_INDEX)
    conn.execute(_V28_FEE_PAYMENT_INDEX)
    # Alter invoices to add due_at, billing, tax columns (nullable for backward compat)
    conn.execute("ALTER TABLE invoices ADD COLUMN due_at INTEGER CHECK(due_at IS NULL OR (typeof(due_at)='integer' AND due_at BETWEEN 0 AND 4102444800))")
    conn.execute("ALTER TABLE invoices ADD COLUMN billing_name TEXT CHECK(billing_name IS NULL OR (length(billing_name) BETWEEN 1 AND 200 AND length(trim(billing_name)) > 0))")
    conn.execute("ALTER TABLE invoices ADD COLUMN billing_contact TEXT CHECK(billing_contact IS NULL OR (length(billing_contact) BETWEEN 1 AND 500))")
    conn.execute("ALTER TABLE invoices ADD COLUMN billing_address TEXT CHECK(billing_address IS NULL OR (length(billing_address) BETWEEN 0 AND 1000))")
    conn.execute("ALTER TABLE invoices ADD COLUMN tax_rate_bps INTEGER CHECK(tax_rate_bps IS NULL OR (typeof(tax_rate_bps)='integer' AND tax_rate_bps BETWEEN 0 AND 10000))")
    conn.execute("ALTER TABLE invoices ADD COLUMN tax_amount_paise INTEGER CHECK(tax_amount_paise IS NULL OR (typeof(tax_amount_paise)='integer' AND tax_amount_paise BETWEEN 0 AND 1000000000000))")
    # Alter customer_orders to add financial fields
    conn.execute("ALTER TABLE customer_orders ADD COLUMN due_at INTEGER CHECK(due_at IS NULL OR (typeof(due_at)='integer' AND due_at BETWEEN 0 AND 4102444800))")
    conn.execute("ALTER TABLE customer_orders ADD COLUMN billing_name TEXT CHECK(billing_name IS NULL OR (length(billing_name) BETWEEN 1 AND 200 AND length(trim(billing_name)) > 0))")
    conn.execute("ALTER TABLE customer_orders ADD COLUMN billing_address TEXT CHECK(billing_address IS NULL OR (length(billing_address) BETWEEN 0 AND 1000))")
    conn.execute("ALTER TABLE customer_orders ADD COLUMN tax_rate_bps INTEGER CHECK(tax_rate_bps IS NULL OR (typeof(tax_rate_bps)='integer' AND tax_rate_bps BETWEEN 0 AND 10000))")
    conn.execute("ALTER TABLE customer_orders ADD COLUMN tax_amount_paise INTEGER CHECK(tax_amount_paise IS NULL OR (typeof(tax_amount_paise)='integer' AND tax_amount_paise BETWEEN 0 AND 1000000000000))")
    conn.execute("INSERT INTO state_migrations(version, applied_at) VALUES (?, ?)", (28, int(time.time())))
    conn.execute("PRAGMA user_version = 28")

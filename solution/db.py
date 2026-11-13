from __future__ import annotations

import hashlib
import hmac
import os
import sqlite3
from pathlib import Path

DEFAULT_DB_PATH = os.environ.get("LEDGER_DB_PATH", "ledger.db")
# Deterministic pepper fallback for local/eval runs if PII_PEPPER env var is not set
PII_PEPPER = os.environ.get("PII_PEPPER", "agentic-universe-eval-pepper-2026").encode("utf-8")


def hash_pii(value: str) -> str:
    """Deterministic HMAC-SHA256 hash for PII matching/deduping without storing plaintext."""
    return hmac.new(PII_PEPPER, value.encode("utf-8"), hashlib.sha256).hexdigest()


def mask_phone(canonical_phone: str) -> str:
    """Format +91XXXXXXXXXX as +91******XXXX."""
    digits = canonical_phone[-4:]
    return f"+91******{digits}"


def get_connection(db_path: str | Path = DEFAULT_DB_PATH) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    conn.execute("PRAGMA foreign_keys=ON;")
    init_schema(conn)
    return conn


def init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS ingested_drops (
            drop_path TEXT PRIMARY KEY,
            ingested_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS dispositions_catalog (
            workflow_id TEXT NOT NULL,
            code TEXT NOT NULL,
            label TEXT NOT NULL,
            is_terminal INTEGER NOT NULL,
            is_success INTEGER NOT NULL,
            PRIMARY KEY (workflow_id, code)
        );

        -- PII protected at rest: customer_name stored only as HMAC hash;
        -- phone stored as HMAC hash (for deterministic joins) + masked form (+91******XXXX) for reports.
        CREATE TABLE IF NOT EXISTS leads (
            workflow_id TEXT NOT NULL,
            phone_hash TEXT NOT NULL,
            phone_masked TEXT NOT NULL,
            customer_name_hash TEXT NOT NULL,
            external_ref TEXT NOT NULL,
            batch_id TEXT NOT NULL,
            extra_fields_json TEXT NOT NULL,
            PRIMARY KEY (workflow_id, phone_hash)
        );

        CREATE TABLE IF NOT EXISTS rejections (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            drop_name TEXT NOT NULL,
            workflow_id TEXT NOT NULL,
            batch_id TEXT NOT NULL,
            row_number INTEGER NOT NULL,
            reason TEXT NOT NULL,
            UNIQUE (drop_name, workflow_id, batch_id, row_number)
        );

        -- Append-only interaction ledger (never updated or deleted)
        CREATE TABLE IF NOT EXISTS interaction_ledger (
            event_id TEXT PRIMARY KEY,
            event_type TEXT NOT NULL,
            workflow_id TEXT NOT NULL,
            call_id TEXT,
            phone_hash TEXT,
            occurred_at_utc TEXT NOT NULL,
            occurred_at_ist TEXT NOT NULL,
            ist_date TEXT NOT NULL,
            payload_json TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_ledger_wf_phone ON interaction_ledger (workflow_id, phone_hash);
        CREATE INDEX IF NOT EXISTS idx_ledger_call_id ON interaction_ledger (call_id);
        CREATE INDEX IF NOT EXISTS idx_ledger_type ON interaction_ledger (event_type);

        CREATE TABLE IF NOT EXISTS ingestion_metrics (
            metric_key TEXT PRIMARY KEY,
            metric_value INTEGER NOT NULL DEFAULT 0
        );
        """
    )
    conn.commit()

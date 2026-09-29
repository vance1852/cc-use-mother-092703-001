"""核燃料循环批次监管的 SQLite 模式和事务辅助。"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS chain_users (
    user_id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    role TEXT NOT NULL CHECK(role IN
        ('registrar','analyst','quality','logistics','recovery','auditor','admin')),
    active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS batches (
    batch_id TEXT PRIMARY KEY,
    material_type TEXT NOT NULL,
    quantity_text TEXT NOT NULL,
    unit TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'registered'
        CHECK(status IN ('registered','in_test','quarantined','released','disposed')),
    disposition_kind TEXT,
    current_declaration_id INTEGER,
    revision INTEGER NOT NULL DEFAULT 1,
    remaining_quantity_text TEXT NOT NULL,
    created_by TEXT NOT NULL REFERENCES chain_users(user_id),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_batches_status ON batches(status, material_type);

CREATE TABLE IF NOT EXISTS declarations (
    declaration_id INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id TEXT NOT NULL REFERENCES batches(batch_id),
    revision_no INTEGER NOT NULL,
    supersedes_id INTEGER REFERENCES declarations(declaration_id),
    source_json TEXT NOT NULL,
    composition_json TEXT NOT NULL,
    basis_doc TEXT NOT NULL,
    content_sha256 TEXT NOT NULL,
    declared_by TEXT NOT NULL REFERENCES chain_users(user_id),
    declared_at TEXT NOT NULL,
    UNIQUE(batch_id, revision_no)
);

CREATE TABLE IF NOT EXISTS lineage_edges (
    edge_id INTEGER PRIMARY KEY AUTOINCREMENT,
    parent_id TEXT NOT NULL REFERENCES batches(batch_id),
    child_id TEXT NOT NULL REFERENCES batches(batch_id),
    operation_kind TEXT NOT NULL CHECK(operation_kind IN ('split','merge')),
    operation_id TEXT NOT NULL,
    quantity_text TEXT NOT NULL,
    declaration_id INTEGER REFERENCES declarations(declaration_id),
    declaration_revision INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    CHECK(parent_id <> child_id)
);

CREATE INDEX IF NOT EXISTS idx_edges_parent ON lineage_edges(parent_id, edge_id);
CREATE INDEX IF NOT EXISTS idx_edges_child ON lineage_edges(child_id, edge_id);

CREATE TABLE IF NOT EXISTS split_operations (
    split_id TEXT PRIMARY KEY,
    parent_id TEXT NOT NULL REFERENCES batches(batch_id),
    idempotency_key TEXT NOT NULL UNIQUE,
    request_sha256 TEXT NOT NULL,
    created_by TEXT NOT NULL REFERENCES chain_users(user_id),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS merge_operations (
    merge_id TEXT PRIMARY KEY,
    child_id TEXT NOT NULL REFERENCES batches(batch_id),
    idempotency_key TEXT NOT NULL UNIQUE,
    request_sha256 TEXT NOT NULL,
    created_by TEXT NOT NULL REFERENCES chain_users(user_id),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS tests (
    test_id INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id TEXT NOT NULL REFERENCES batches(batch_id),
    method TEXT NOT NULL,
    instrument TEXT NOT NULL,
    results_json TEXT NOT NULL,
    verdict TEXT NOT NULL CHECK(verdict IN ('conforming','nonconforming','inconclusive')),
    basis_doc TEXT NOT NULL,
    content_sha256 TEXT NOT NULL,
    idempotency_key TEXT UNIQUE,
    recorded_by TEXT NOT NULL REFERENCES chain_users(user_id),
    recorded_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_tests_batch ON tests(batch_id, test_id);

CREATE TABLE IF NOT EXISTS decisions (
    decision_id INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id TEXT NOT NULL REFERENCES batches(batch_id),
    decision TEXT NOT NULL CHECK(decision IN ('release','quarantine','reject')),
    reason TEXT NOT NULL,
    test_id INTEGER REFERENCES tests(test_id),
    decided_by TEXT NOT NULL REFERENCES chain_users(user_id),
    decided_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_decisions_batch ON decisions(batch_id, decision_id);

CREATE TABLE IF NOT EXISTS custody_events (
    custody_id INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id TEXT NOT NULL REFERENCES batches(batch_id),
    handoff_type TEXT NOT NULL CHECK(handoff_type IN ('ship','receive','return')),
    from_party TEXT NOT NULL,
    to_party TEXT NOT NULL,
    document_ref TEXT NOT NULL,
    observed_quantity_text TEXT NOT NULL,
    note TEXT NOT NULL DEFAULT '',
    idempotency_key TEXT UNIQUE,
    handled_by TEXT NOT NULL REFERENCES chain_users(user_id),
    occurred_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_custody_batch ON custody_events(batch_id, custody_id);

CREATE TABLE IF NOT EXISTS dispositions (
    disposition_id INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id TEXT NOT NULL UNIQUE REFERENCES batches(batch_id),
    kind TEXT NOT NULL CHECK(kind IN ('recovery','discard','final_storage')),
    quantity_text TEXT NOT NULL,
    method TEXT NOT NULL,
    facility TEXT NOT NULL,
    document_ref TEXT NOT NULL,
    idempotency_key TEXT NOT NULL UNIQUE,
    operated_by TEXT NOT NULL REFERENCES chain_users(user_id),
    operated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS manifest_imports (
    import_id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_ref TEXT NOT NULL,
    idempotency_key TEXT NOT NULL UNIQUE,
    content_sha256 TEXT NOT NULL,
    summary_json TEXT NOT NULL,
    imported_by TEXT NOT NULL REFERENCES chain_users(user_id),
    imported_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS idempotency_keys (
    scope TEXT NOT NULL,
    key TEXT NOT NULL,
    request_sha256 TEXT NOT NULL,
    response_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(scope, key)
);

CREATE TABLE IF NOT EXISTS audit_events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    entity_type TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    previous_hash TEXT NOT NULL,
    event_hash TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_audit_entity ON audit_events(entity_type, entity_id, event_id);
"""


def connect(path: str | Path) -> sqlite3.Connection:
    # ThreadingHTTPServer 会在请求线程中复用该连接；所有写操作都在
    # BEGIN IMMEDIATE 短事务内并由 busy_timeout 串行化。
    connection = sqlite3.connect(
        str(path), isolation_level=None, timeout=10, check_same_thread=False
    )
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA busy_timeout=5000")
    initialize(connection)
    return connection


def initialize(connection: sqlite3.Connection) -> None:
    connection.executescript(SCHEMA)


@contextmanager
def transaction(connection: sqlite3.Connection, *, immediate: bool = False) -> Iterator[None]:
    connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
    try:
        yield
    except BaseException:
        connection.rollback()
        raise
    else:
        connection.commit()

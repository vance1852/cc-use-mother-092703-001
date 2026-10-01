"""核燃料循环批次监管的 SQLite 模式与事务辅助。"""

from __future__ import annotations

import contextlib
import sqlite3
from collections.abc import Iterator
from pathlib import Path


SCHEMA_VERSION = 1

SCHEMA_SQL = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS schema_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS fuel_users (
    user_id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('operator', 'analyst', 'quality', 'custodian', 'auditor')),
    active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
    created_at TEXT NOT NULL
);

-- 批次主档：status 是唯一的当前状态，所有历史状态都在决策/交接/处置表中留痕。
CREATE TABLE IF NOT EXISTS batches (
    batch_id TEXT PRIMARY KEY,
    material_type TEXT NOT NULL,
    quantity TEXT NOT NULL,
    unit TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN (
        'declared', 'released', 'quarantined', 'in_transit', 'exhausted', 'disposed'
    )),
    declaration_version INTEGER NOT NULL DEFAULT 1,
    current_transfer_id TEXT,
    status_reason TEXT,
    created_by TEXT NOT NULL REFERENCES fuel_users(user_id),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

-- 来源与成分声明只追加：更正产生新版本，旧版本原样保留。
-- 凭证与批次的一一对应由 source_registry 单独保证，更正不触碰该表。
CREATE TABLE IF NOT EXISTS source_registry (
    source_type TEXT NOT NULL,
    source_reference TEXT NOT NULL,
    batch_id TEXT NOT NULL UNIQUE REFERENCES batches(batch_id),
    registered_at TEXT NOT NULL,
    PRIMARY KEY (source_type, source_reference)
);

CREATE TABLE IF NOT EXISTS source_declarations (
    declaration_id INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id TEXT NOT NULL REFERENCES batches(batch_id),
    version INTEGER NOT NULL CHECK (version >= 1),
    source_type TEXT NOT NULL CHECK (source_type IN (
        'mining', 'milling', 'conversion', 'enrichment', 'fabrication',
        'recovery', 'derived', 'imported'
    )),
    source_reference TEXT NOT NULL,
    supplier TEXT NOT NULL,
    origin_doc_ref TEXT NOT NULL,
    material_type TEXT NOT NULL,
    quantity TEXT NOT NULL,
    unit TEXT NOT NULL,
    components_json TEXT NOT NULL,
    remarks TEXT,
    received_at TEXT NOT NULL,
    basis_sha256 TEXT NOT NULL CHECK (length(basis_sha256) = 64),
    declared_by TEXT NOT NULL REFERENCES fuel_users(user_id),
    declared_at TEXT NOT NULL,
    supersedes_id INTEGER REFERENCES source_declarations(declaration_id),
    UNIQUE (batch_id, version)
);

-- 分批/合批作业；输入输出构成批次谱系 DAG。
CREATE TABLE IF NOT EXISTS material_transforms (
    transform_id TEXT PRIMARY KEY,
    kind TEXT NOT NULL CHECK (kind IN ('split', 'merge')),
    note TEXT,
    created_by TEXT NOT NULL REFERENCES fuel_users(user_id),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS transform_inputs (
    transform_id TEXT NOT NULL REFERENCES material_transforms(transform_id),
    batch_id TEXT NOT NULL REFERENCES batches(batch_id),
    quantity TEXT NOT NULL,
    unit TEXT NOT NULL,
    PRIMARY KEY (transform_id, batch_id)
);

CREATE TABLE IF NOT EXISTS transform_outputs (
    transform_id TEXT NOT NULL REFERENCES material_transforms(transform_id),
    batch_id TEXT NOT NULL REFERENCES batches(batch_id),
    ordinal INTEGER NOT NULL CHECK (ordinal >= 1),
    quantity TEXT NOT NULL,
    unit TEXT NOT NULL,
    PRIMARY KEY (transform_id, batch_id),
    UNIQUE (transform_id, ordinal)
);

-- 检测复核记录只追加；记录所依据的声明版本与全部祖先声明版本，
-- 任何上游更正后旧检测不再具备放行依据资格。
CREATE TABLE IF NOT EXISTS inspections (
    test_id INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id TEXT NOT NULL REFERENCES batches(batch_id),
    sequence_no INTEGER NOT NULL,
    lab TEXT NOT NULL,
    test_type TEXT NOT NULL,
    method TEXT NOT NULL,
    basis_declaration_version INTEGER NOT NULL,
    lineage_basis_json TEXT,
    sampled_at TEXT NOT NULL,
    tested_at TEXT NOT NULL,
    results_json TEXT NOT NULL,
    limits_json TEXT NOT NULL,
    verdict TEXT NOT NULL CHECK (verdict IN ('pass', 'fail', 'inconclusive')),
    conclusion TEXT,
    basis_sha256 TEXT NOT NULL CHECK (length(basis_sha256) = 64),
    tested_by TEXT NOT NULL REFERENCES fuel_users(user_id),
    recorded_at TEXT NOT NULL,
    UNIQUE (batch_id, sequence_no)
);

-- 有权人员的放行/隔离决定只追加；lineage_basis_json 固化放行时刻全部祖先的声明版本。
CREATE TABLE IF NOT EXISTS release_decisions (
    decision_id INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id TEXT NOT NULL REFERENCES batches(batch_id),
    sequence_no INTEGER NOT NULL,
    decision TEXT NOT NULL CHECK (decision IN ('release', 'quarantine')),
    reason TEXT NOT NULL,
    basis_test_id INTEGER REFERENCES inspections(test_id),
    basis_declaration_version INTEGER NOT NULL,
    lineage_basis_json TEXT,
    decided_by TEXT NOT NULL REFERENCES fuel_users(user_id),
    decided_at TEXT NOT NULL,
    UNIQUE (batch_id, sequence_no)
);

-- 每次运输交接一行；prior_status 在发运时固化，接收时不得借此提升隔离状态。
CREATE TABLE IF NOT EXISTS transfers (
    transfer_id TEXT PRIMARY KEY,
    batch_id TEXT NOT NULL REFERENCES batches(batch_id),
    from_party TEXT NOT NULL,
    to_party TEXT NOT NULL,
    from_location TEXT NOT NULL,
    to_location TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('dispatched', 'received')),
    prior_status TEXT NOT NULL CHECK (prior_status IN ('released', 'quarantined')),
    dispatched_by TEXT NOT NULL REFERENCES fuel_users(user_id),
    dispatched_at TEXT NOT NULL,
    manifest_ref TEXT,
    received_by TEXT REFERENCES fuel_users(user_id),
    received_at TEXT,
    receive_note TEXT
);

-- 不可逆处置终态记录。
CREATE TABLE IF NOT EXISTS disposals (
    disposal_id INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id TEXT NOT NULL UNIQUE REFERENCES batches(batch_id),
    method TEXT NOT NULL,
    authority_doc_ref TEXT NOT NULL,
    reason TEXT NOT NULL,
    witness TEXT NOT NULL REFERENCES fuel_users(user_id),
    disposed_by TEXT NOT NULL REFERENCES fuel_users(user_id),
    disposed_at TEXT NOT NULL
);

-- 导入幂等：同一键重放原响应，绝不重新执行副作用。
CREATE TABLE IF NOT EXISTS idempotency_keys (
    scope TEXT NOT NULL,
    key TEXT NOT NULL,
    request_sha256 TEXT NOT NULL CHECK (length(request_sha256) = 64),
    response_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (scope, key)
);

-- 全局哈希链审计事件。
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

REQUIRED_TABLES = frozenset({
    "schema_meta", "fuel_users", "batches", "source_registry", "source_declarations",
    "material_transforms", "transform_inputs", "transform_outputs",
    "inspections", "release_decisions", "transfers", "disposals",
    "idempotency_keys", "audit_events",
})


def connect(path: str | Path = ":memory:", *, check_same_thread: bool = True) -> sqlite3.Connection:
    """打开连接并启用外键与显式事务模式。

    HTTP 服务在工作线程间共享同一连接时传 check_same_thread=False，
    并由应用层锁串行化全部请求。
    """

    connection = sqlite3.connect(
        str(path), isolation_level=None, check_same_thread=check_same_thread
    )
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA busy_timeout = 5000")
    return connection


@contextlib.contextmanager
def transaction(connection: sqlite3.Connection, *, immediate: bool = False) -> Iterator[None]:
    """显式事务；异常时保证回滚。"""

    connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
    try:
        yield
    except BaseException:
        connection.rollback()
        raise
    else:
        connection.commit()


def initialize(connection: sqlite3.Connection) -> None:
    """初始化全部表结构，重复执行不改变已有数据。"""

    connection.executescript(SCHEMA_SQL)
    with transaction(connection, immediate=True):
        connection.execute(
            "INSERT INTO schema_meta(key, value) VALUES('schema_version', ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (str(SCHEMA_VERSION),),
        )


def inspect_schema(connection: sqlite3.Connection) -> dict[str, object]:
    """返回适合机器检查的数据库结构摘要。"""

    table_rows = connection.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
    ).fetchall()
    tables = tuple(row["name"] for row in table_rows)
    version_row = connection.execute(
        "SELECT value FROM schema_meta WHERE key='schema_version'"
    ).fetchone()
    return {
        "tables": tables,
        "missing_tables": sorted(REQUIRED_TABLES - set(tables)),
        "schema_version": None if version_row is None else version_row["value"],
        "foreign_keys": bool(connection.execute("PRAGMA foreign_keys").fetchone()[0]),
    }

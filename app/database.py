"""SQLite 连接、建表与初始化。"""
from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from collections.abc import Iterator

from . import config

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

-- 产线（首次登记批次时自动建立）
CREATE TABLE IF NOT EXISTS lines (
    code        TEXT PRIMARY KEY,
    created_at  TEXT NOT NULL
);

-- 原料批号主数据
CREATE TABLE IF NOT EXISTS material_lots (
    lot_no        TEXT PRIMARY KEY,
    material_name TEXT NOT NULL,
    allergens     TEXT NOT NULL DEFAULT '[]',   -- JSON: 规范代码数组
    created_at    TEXT NOT NULL
);

-- 生产批次（按产线内 seq 表示产线登记/生产顺序）
CREATE TABLE IF NOT EXISTS batches (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    line_code          TEXT NOT NULL REFERENCES lines(code),
    batch_no           TEXT NOT NULL,
    seq                INTEGER NOT NULL,
    declared_allergens TEXT NOT NULL DEFAULT '[]', -- JSON: 成品声明过敏原
    cleaning           TEXT NOT NULL DEFAULT '[]', -- JSON: 清洁核验确认清除的过敏原
    created_at         TEXT NOT NULL,
    UNIQUE(line_code, batch_no)
);

-- 批次使用的原料批号（顺序保留）
CREATE TABLE IF NOT EXISTS batch_materials (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id    INTEGER NOT NULL REFERENCES batches(id) ON DELETE CASCADE,
    position    INTEGER NOT NULL,
    material_lot TEXT NOT NULL REFERENCES material_lots(lot_no),
    UNIQUE(batch_id, position)
);

-- 裁决历史版本：每次计算/重算追加一行，永不覆盖
CREATE TABLE IF NOT EXISTS verdict_versions (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id             INTEGER NOT NULL REFERENCES batches(id) ON DELETE CASCADE,
    version              INTEGER NOT NULL,
    incoming_residual    TEXT NOT NULL DEFAULT '[]', -- JSON: 本批开始前上批残留
    cleaning_covered     TEXT NOT NULL DEFAULT '[]', -- JSON: 本批清洁实际命中的上批残留
    remaining_carryover  TEXT NOT NULL DEFAULT '[]', -- JSON: 清洁后仍残留
    material_allergens   TEXT NOT NULL DEFAULT '[]', -- JSON: 本批原料过敏原并集
    actual_allergens     TEXT NOT NULL DEFAULT '[]', -- JSON: 实际可能含有
    undeclared_allergens TEXT NOT NULL DEFAULT '[]', -- JSON: 未声明项
    sources              TEXT NOT NULL DEFAULT '{}', -- JSON: allergen -> 来源批号/残留
    decision             TEXT NOT NULL,              -- RELEASE / WITHHELD
    reason               TEXT NOT NULL,
    triggered_by         TEXT NOT NULL,              -- REGISTER / CORRECTION / RECASCADE / MATERIAL_CORRECTION
    correction_id        INTEGER,                    -- 关联 correction_events.id
    material_correction_id INTEGER,                  -- 关联 material_correction_events.id
    created_at           TEXT NOT NULL,
    UNIQUE(batch_id, version)
);

-- 更正事件审计（批次级：原料批号引用/声明/清洁）
CREATE TABLE IF NOT EXISTS correction_events (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id      INTEGER NOT NULL REFERENCES batches(id),
    changed_fields TEXT NOT NULL DEFAULT '[]',       -- JSON: 被更正字段名
    old_values    TEXT NOT NULL DEFAULT '{}',        -- JSON: 更正前值
    new_values    TEXT NOT NULL DEFAULT '{}',        -- JSON: 更正后值
    recalculated  TEXT NOT NULL DEFAULT '[]',        -- JSON: 重算批次 id 列表
    created_at    TEXT NOT NULL
);

-- 原料批号主数据更正审计（过敏原/名称更正，可能影响多条产线）
CREATE TABLE IF NOT EXISTS material_correction_events (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    lot_no         TEXT NOT NULL REFERENCES material_lots(lot_no),
    changed_fields TEXT NOT NULL DEFAULT '[]',       -- JSON: 被更正字段名
    old_values     TEXT NOT NULL DEFAULT '{}',       -- JSON: 更正前值
    new_values     TEXT NOT NULL DEFAULT '{}',       -- JSON: 更正后值
    recalculated   TEXT NOT NULL DEFAULT '[]',       -- JSON: 重算批次 id 列表（跨产线）
    created_at     TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_batches_line_seq ON batches(line_code, seq);
CREATE INDEX IF NOT EXISTS idx_bmat_batch ON batch_materials(batch_id);
CREATE INDEX IF NOT EXISTS idx_verdicts_batch_ver ON verdict_versions(batch_id, version DESC);
"""


def connect(db_path: str | None = None) -> sqlite3.Connection:
    """打开一个到 SQLite 的连接（行工厂为 dict）。"""
    path = db_path or config.DB_PATH
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """写事务上下文：异常回滚，成功提交。

    使用 BEGIN IMMEDIATE 立即获取写锁，避免写-写竞争。
    """
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def init_db(db_path: str | None = None) -> None:
    """创建数据目录并执行建表脚本（幂等）。"""
    path = db_path or config.DB_PATH
    parent = os.path.dirname(os.path.abspath(path))
    if parent:
        os.makedirs(parent, exist_ok=True)
    conn = connect(path)
    try:
        # executescript 自动处理多条语句；先确保 WAL 与外键
        conn.executescript(SCHEMA)
        # 轻量迁移：为旧库补上新列（IF NOT EXISTS 的建表不会改已有表）
        existing_cols = {
            r["name"]
            for r in conn.execute("PRAGMA table_info(verdict_versions)").fetchall()
        }
        if "material_correction_id" not in existing_cols:
            conn.execute(
                "ALTER TABLE verdict_versions ADD COLUMN material_correction_id INTEGER"
            )
        conn.commit()
    finally:
        conn.close()

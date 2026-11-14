"""SQLite 持久化层。

单进程 API 使用短连接 + WAL：每次操作独立连接，配合线程内
``threading.RLock``（每个上传一把锁）即可保证同一上传串行化；
不同上传之间由 SQLite 事务本身保证一致。
"""
from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from .config import settings

SCHEMA = """
CREATE TABLE IF NOT EXISTS uploads (
    upload_id          TEXT PRIMARY KEY,
    sample_id          TEXT NOT NULL,
    filename           TEXT NOT NULL,
    object_key         TEXT NOT NULL,
    total_bytes        INTEGER NOT NULL,
    content_sha256     TEXT,
    multipart_upload_id TEXT,
    status             TEXT NOT NULL CHECK (status IN
                         ('open','completing','completed','aborted','invalid')),
    width              INTEGER,
    height             INTEGER,
    pages              INTEGER,
    dtype              TEXT,
    axes               TEXT,
    error_message      TEXT,
    created_at         TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    updated_at         TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

CREATE TABLE IF NOT EXISTS parts (
    upload_id    TEXT NOT NULL REFERENCES uploads(upload_id),
    part_number  INTEGER NOT NULL,
    size         INTEGER NOT NULL,
    sha256       TEXT NOT NULL,
    etag         TEXT NOT NULL,
    PRIMARY KEY (upload_id, part_number)
);

CREATE TABLE IF NOT EXISTS crops (
    crop_id      TEXT PRIMARY KEY,
    upload_id    TEXT NOT NULL REFERENCES uploads(upload_id),
    object_key   TEXT NOT NULL,
    filename     TEXT NOT NULL,
    t_index      INTEGER NOT NULL,
    z_index      INTEGER NOT NULL,
    c_index      INTEGER NOT NULL,
    x            INTEGER NOT NULL,
    y            INTEGER NOT NULL,
    width        INTEGER NOT NULL,
    height       INTEGER NOT NULL,
    dtype        TEXT NOT NULL,
    size_bytes   INTEGER NOT NULL,
    sha256       TEXT NOT NULL,
    created_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    UNIQUE (upload_id, t_index, z_index, c_index, x, y, width, height)
);

CREATE INDEX IF NOT EXISTS idx_uploads_sample ON uploads(sample_id);
CREATE INDEX IF NOT EXISTS idx_uploads_status ON uploads(status);
CREATE INDEX IF NOT EXISTS idx_crops_upload ON crops(upload_id);
"""


def init_db(db_path: Path | None = None) -> None:
    path = db_path or settings.db_path
    path.parent.mkdir(parents=True, exist_ok=True)
    with connect(path) as conn:
        conn.executescript(SCHEMA)


@contextmanager
def connect(db_path: Path | None = None) -> Iterator[sqlite3.Connection]:
    conn = sqlite3.connect(str(db_path or settings.db_path), timeout=30)
    try:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=30000")
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def row_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(row) if row is not None else None


def get_upload(conn: sqlite3.Connection, upload_id: str) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM uploads WHERE upload_id = ?", (upload_id,)).fetchone()
    return row_to_dict(row)


def list_parts(conn: sqlite3.Connection, upload_id: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT part_number, size, sha256, etag FROM parts WHERE upload_id = ? ORDER BY part_number",
        (upload_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def upsert_part(
    conn: sqlite3.Connection,
    upload_id: str,
    part_number: int,
    size: int,
    sha256: str,
    etag: str,
) -> None:
    conn.execute(
        """
        INSERT INTO parts (upload_id, part_number, size, sha256, etag)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(upload_id, part_number) DO UPDATE SET
            size=excluded.size, sha256=excluded.sha256, etag=excluded.etag
        """,
        (upload_id, part_number, size, sha256, etag),
    )


def touch_upload(conn: sqlite3.Connection, upload_id: str) -> None:
    conn.execute(
        "UPDATE uploads SET updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE upload_id=?",
        (upload_id,),
    )


# --------------------------------------------------------------------- 裁片
def insert_crop_if_absent(conn: sqlite3.Connection, row: dict[str, Any]) -> bool:
    """按 (upload_id, 裁剪参数) 唯一约束幂等写入；返回是否真正插入。"""
    cur = conn.execute(
        """
        INSERT OR IGNORE INTO crops
            (crop_id, upload_id, object_key, filename,
             t_index, z_index, c_index, x, y, width, height,
             dtype, size_bytes, sha256)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (row["crop_id"], row["upload_id"], row["object_key"], row["filename"],
         row["t_index"], row["z_index"], row["c_index"],
         row["x"], row["y"], row["width"], row["height"],
         row["dtype"], row["size_bytes"], row["sha256"]),
    )
    return cur.rowcount == 1


def get_crop_by_params(
    conn: sqlite3.Connection,
    upload_id: str,
    t: int, z: int, c: int,
    x: int, y: int, width: int, height: int,
) -> dict[str, Any] | None:
    row = conn.execute(
        """
        SELECT * FROM crops
        WHERE upload_id=? AND t_index=? AND z_index=? AND c_index=?
          AND x=? AND y=? AND width=? AND height=?
        """,
        (upload_id, t, z, c, x, y, width, height),
    ).fetchone()
    return row_to_dict(row)


def get_crop(conn: sqlite3.Connection, crop_id: str) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM crops WHERE crop_id=?", (crop_id,)).fetchone()
    return row_to_dict(row)


def list_crops(conn: sqlite3.Connection, upload_id: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT * FROM crops WHERE upload_id=? ORDER BY created_at, crop_id",
        (upload_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def compare_and_set_status(
    conn: sqlite3.Connection,
    upload_id: str,
    expected: tuple[str, ...],
    new_status: str,
) -> int:
    """CAS 状态流转，返回受影响行数（0 表示已被并发操作抢先）。"""
    placeholders = ",".join("?" for _ in expected)
    sql = (
        "UPDATE uploads SET status=?, "
        "updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') "
        f"WHERE upload_id=? AND status IN ({placeholders})"
    )
    cur = conn.execute(sql, (new_status, upload_id, *expected))
    return cur.rowcount

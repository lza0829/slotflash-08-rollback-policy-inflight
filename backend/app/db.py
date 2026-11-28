from __future__ import annotations
import sqlite3
from contextlib import contextmanager
from app.config import DB_PATH


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=5.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    # 拧档与收口可能并发；给写事务留等锁窗口，避免落成半截状态。
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


@contextmanager
def db():
    conn = connect()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


@contextmanager
def write_tx():
    """写侧统一走 BEGIN IMMEDIATE：拧档同步在途单、收口落冻结档都在同一笔事务里收束。

    任一步失败即整笔回滚，不留「半截脏跟随」，也不会出现拨杆已拧、在途单却没跟上。
    """
    conn = connect()
    conn.isolation_level = None  # 手动控制事务边界
    try:
        conn.execute("BEGIN IMMEDIATE")
        yield conn
        conn.execute("COMMIT")
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except Exception:
            pass
        raise
    finally:
        conn.close()


def _ensure_column(conn: sqlite3.Connection, table: str, column: str, decl: str) -> None:
    cols = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
    if column not in cols:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")


def _migrate(conn: sqlite3.Connection) -> None:
    # 现档版本号：每次拧档 +1，供在途单与跟随痕对齐/重放。
    _ensure_column(conn, "rollback_policy", "version", "INTEGER NOT NULL DEFAULT 0")
    # 单内回退档：在途时随现档同步；收口当刻冻结，事后拧档不得改写。
    _ensure_column(conn, "flash_orders", "policy_mode", "TEXT")
    _ensure_column(conn, "flash_orders", "policy_hold_sec", "INTEGER")
    _ensure_column(conn, "flash_orders", "policy_version", "INTEGER NOT NULL DEFAULT 0")
    _ensure_column(conn, "flash_orders", "policy_frozen_at", "TEXT")


def init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS firmwares (
          id INTEGER PRIMARY KEY,
          name TEXT NOT NULL,
          version TEXT NOT NULL,
          digest TEXT NOT NULL,
          verify_state TEXT NOT NULL,
          size_kb INTEGER NOT NULL DEFAULT 0,
          ready_at TEXT
        );
        CREATE TABLE IF NOT EXISTS boards (
          id INTEGER PRIMARY KEY,
          name TEXT NOT NULL,
          active_slot TEXT NOT NULL,
          watchdog_sec INTEGER NOT NULL DEFAULT 30
        );
        CREATE TABLE IF NOT EXISTS slots (
          id INTEGER PRIMARY KEY,
          board_id INTEGER NOT NULL,
          slot_key TEXT NOT NULL,
          role TEXT NOT NULL,
          firmware_id INTEGER,
          write_state TEXT NOT NULL,
          boot_color TEXT NOT NULL DEFAULT '灰',
          FOREIGN KEY(board_id) REFERENCES boards(id),
          FOREIGN KEY(firmware_id) REFERENCES firmwares(id),
          UNIQUE(board_id, slot_key)
        );
        CREATE TABLE IF NOT EXISTS flash_orders (
          id INTEGER PRIMARY KEY,
          board_id INTEGER NOT NULL,
          target_slot TEXT NOT NULL,
          firmware_id INTEGER NOT NULL,
          status TEXT NOT NULL,
          note TEXT,
          created_at TEXT NOT NULL,
          policy_mode TEXT,
          policy_hold_sec INTEGER,
          policy_version INTEGER NOT NULL DEFAULT 0,
          policy_frozen_at TEXT,
          FOREIGN KEY(board_id) REFERENCES boards(id),
          FOREIGN KEY(firmware_id) REFERENCES firmwares(id)
        );
        CREATE TABLE IF NOT EXISTS boot_chain (
          id INTEGER PRIMARY KEY,
          board_id INTEGER NOT NULL UNIQUE,
          pending_slot TEXT,
          color TEXT NOT NULL,
          fail_streak INTEGER NOT NULL DEFAULT 0,
          updated_at TEXT,
          FOREIGN KEY(board_id) REFERENCES boards(id)
        );
        CREATE TABLE IF NOT EXISTS rollback_policy (
          id INTEGER PRIMARY KEY CHECK (id = 1),
          mode TEXT NOT NULL,
          hold_sec INTEGER NOT NULL DEFAULT 20,
          version INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS policy_flips (
          id INTEGER PRIMARY KEY,
          from_mode TEXT,
          to_mode TEXT NOT NULL,
          from_hold_sec INTEGER,
          to_hold_sec INTEGER,
          version_before INTEGER NOT NULL DEFAULT 0,
          version_after INTEGER NOT NULL DEFAULT 0,
          affected_orders INTEGER NOT NULL DEFAULT 0,
          at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS policy_follows (
          id INTEGER PRIMARY KEY,
          flip_id INTEGER,
          order_id INTEGER NOT NULL,
          from_mode TEXT,
          to_mode TEXT,
          outcome TEXT NOT NULL,
          at TEXT NOT NULL,
          FOREIGN KEY(flip_id) REFERENCES policy_flips(id),
          FOREIGN KEY(order_id) REFERENCES flash_orders(id)
        );
        CREATE TABLE IF NOT EXISTS events (
          id INTEGER PRIMARY KEY,
          kind TEXT NOT NULL,
          ref TEXT,
          detail TEXT NOT NULL,
          at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS settings (
          key TEXT PRIMARY KEY,
          value TEXT NOT NULL
        );
        """
    )
    _migrate(conn)

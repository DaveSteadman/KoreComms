"""SQLite data-access layer for KoreComms.

Schema:
  interfaces        - external channel configurations (OAuth tokens, etc.)
  conversations     - routing table: links an interface to a KoreConversation ID
  external_messages - thin deduplication and reply-anchoring records
  activity_log      - operational audit trail

KoreComms does NOT store message content; that lives in KoreConversation.
Each public function creates its own connection so it is safe to call from
any thread. WAL mode is enabled for better read concurrency.
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Generator

from app.config import cfg

_DB_PATH: Path | None = None


def get_db_path() -> Path:
    global _DB_PATH
    if _DB_PATH is None:
        data_dir = Path(cfg["data_dir"])
        data_dir.mkdir(parents=True, exist_ok=True)
        _DB_PATH = data_dir / "korecomms.db"
    return _DB_PATH


@contextmanager
def get_db() -> Generator[sqlite3.Connection, None, None]:
    conn = sqlite3.connect(get_db_path(), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

_SCHEMA = """
CREATE TABLE IF NOT EXISTS config (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS interfaces (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    type        TEXT NOT NULL,
    name        TEXT NOT NULL,
    config_json TEXT NOT NULL DEFAULT '{}',
    enabled     INTEGER NOT NULL DEFAULT 1,
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS conversations (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    interface_id       INTEGER NOT NULL REFERENCES interfaces(id) ON DELETE CASCADE,
    conversation_name  TEXT,
    kc_conversation_id INTEGER,
    external_thread_id TEXT,
    subject            TEXT,
    created_at         TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS external_messages (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id      INTEGER NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    external_message_id  TEXT NOT NULL,
    direction            TEXT NOT NULL CHECK(direction IN ('inbound','outbound')),
    sender_display       TEXT NOT NULL DEFAULT '',
    received_at          TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS activity_log (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    action    TEXT NOT NULL,
    detail    TEXT,
    logged_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_convs_iface        ON conversations(interface_id);
CREATE INDEX IF NOT EXISTS idx_convs_kc_id        ON conversations(kc_conversation_id);
CREATE INDEX IF NOT EXISTS idx_convs_thread       ON conversations(external_thread_id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_convs_name  ON conversations(conversation_name);
CREATE UNIQUE INDEX IF NOT EXISTS idx_ext_msg_id  ON external_messages(external_message_id);
CREATE INDEX IF NOT EXISTS idx_ext_msg_conv       ON external_messages(conversation_id, direction);
"""


def init_db() -> None:
    """Create tables, run migrations, and seed the permanent Manual interface."""
    with get_db() as conn:
        # Migration: add kc_conversation_id to conversations if this is an existing DB.
        conv_cols = {r[1] for r in conn.execute("PRAGMA table_info(conversations)")}
        if conv_cols and "conversation_name" not in conv_cols:
            conn.execute("ALTER TABLE conversations ADD COLUMN conversation_name TEXT")
        if conv_cols and "kc_conversation_id" not in conv_cols:
            conn.execute("ALTER TABLE conversations ADD COLUMN kc_conversation_id INTEGER")
        if conv_cols and "subject" not in conv_cols:
            conn.execute("ALTER TABLE conversations ADD COLUMN subject TEXT")
        conn.executescript(_SCHEMA)
        conn.execute(
            "UPDATE conversations "
            "SET conversation_name = COALESCE(NULLIF(external_thread_id, ''), 'kccomms:' || id) "
            "WHERE conversation_name IS NULL OR conversation_name = ''"
        )
        # Ensure the Manual interface always exists.
        row = conn.execute("SELECT id FROM interfaces WHERE type='manual' LIMIT 1").fetchone()
        if row is None:
            conn.execute(
                "INSERT INTO interfaces (type, name, config_json, enabled, created_at) "
                "VALUES ('manual', 'Manual', '{}', 1, ?)",
                (_now(),),
            )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _row_to_dict(row: sqlite3.Row | None) -> dict | None:
    return dict(row) if row else None


# ---------------------------------------------------------------------------
# Config table
# ---------------------------------------------------------------------------

def config_get(key: str, default: str | None = None) -> str | None:
    with get_db() as conn:
        row = conn.execute("SELECT value FROM config WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def config_set(key: str, value: str) -> None:
    with get_db() as conn:
        conn.execute(
            "INSERT INTO config(key,value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )


# ---------------------------------------------------------------------------
# Interfaces
# ---------------------------------------------------------------------------

def interface_list() -> list[dict]:
    with get_db() as conn:
        rows = conn.execute("SELECT * FROM interfaces ORDER BY id").fetchall()
    return [dict(r) for r in rows]


def interface_get(iface_id: int) -> dict | None:
    with get_db() as conn:
        row = conn.execute("SELECT * FROM interfaces WHERE id=?", (iface_id,)).fetchone()
    return _row_to_dict(row)


def interface_get_manual() -> dict:
    with get_db() as conn:
        row = conn.execute("SELECT * FROM interfaces WHERE type='manual' LIMIT 1").fetchone()
    return dict(row)


def interface_create(type_: str, name: str, config_json: dict) -> int:
    with get_db() as conn:
        cur = conn.execute(
            "INSERT INTO interfaces (type, name, config_json, enabled, created_at) "
            "VALUES (?,?,?,1,?)",
            (type_, name, json.dumps(config_json), _now()),
        )
    return cur.lastrowid  # type: ignore[return-value]


def interface_update(iface_id: int, name: str, config_json: dict, enabled: bool) -> None:
    with get_db() as conn:
        conn.execute(
            "UPDATE interfaces SET name=?, config_json=?, enabled=? WHERE id=?",
            (name, json.dumps(config_json), int(enabled), iface_id),
        )


def interface_delete(iface_id: int) -> None:
    with get_db() as conn:
        conn.execute("DELETE FROM interfaces WHERE id=? AND type != 'manual'", (iface_id,))


# ---------------------------------------------------------------------------
# Conversations
# ---------------------------------------------------------------------------

def conversation_list(limit: int = 100, offset: int = 0) -> list[dict]:
    with get_db() as conn:
        rows = conn.execute(
            "SELECT c.*, i.name AS interface_name, i.type AS interface_type "
            "FROM conversations c "
            "JOIN interfaces i ON i.id = c.interface_id "
            "ORDER BY c.id DESC LIMIT ? OFFSET ?",
            (limit, offset),
        ).fetchall()
    return [dict(r) for r in rows]


def conversation_create(
    interface_id:       int,
    kc_conversation_id: int | None = None,
    external_thread_id: str | None = None,
    subject:            str | None = None,
    conversation_name:  str | None = None,
) -> int:
    with get_db() as conn:
        cur = conn.execute(
            "INSERT INTO conversations "
            "(interface_id, conversation_name, kc_conversation_id, external_thread_id, subject, created_at) "
            "VALUES (?,?,?,?,?,?)",
            (interface_id, conversation_name, kc_conversation_id, external_thread_id, subject, _now()),
        )
        if not conversation_name:
            conversation_name = f"kccomms:{cur.lastrowid}"
            conn.execute(
                "UPDATE conversations SET conversation_name=? WHERE id=?",
                (conversation_name, cur.lastrowid),
            )
    return cur.lastrowid  # type: ignore[return-value]


def conversation_get(conv_id: int) -> dict | None:
    with get_db() as conn:
        row = conn.execute(
            "SELECT c.*, i.name AS interface_name, i.type AS interface_type "
            "FROM conversations c JOIN interfaces i ON i.id = c.interface_id "
            "WHERE c.id=?",
            (conv_id,),
        ).fetchone()
    return _row_to_dict(row)


def conversation_set_kc_id(conv_id: int, kc_conversation_id: int | None) -> None:
    with get_db() as conn:
        conn.execute(
            "UPDATE conversations SET kc_conversation_id=? WHERE id=?",
            (kc_conversation_id, conv_id),
        )


def conversation_set_name(conv_id: int, conversation_name: str) -> None:
    with get_db() as conn:
        conn.execute(
            "UPDATE conversations SET conversation_name=? WHERE id=?",
            (conversation_name, conv_id),
        )


def conversation_get_by_external_thread(external_thread_id: str) -> dict | None:
    with get_db() as conn:
        row = conn.execute(
            "SELECT c.*, i.name AS interface_name, i.type AS interface_type "
            "FROM conversations c JOIN interfaces i ON i.id = c.interface_id "
            "WHERE c.external_thread_id=? LIMIT 1",
            (external_thread_id,),
        ).fetchone()
    return _row_to_dict(row)


def conversation_get_by_name(conversation_name: str) -> dict | None:
    with get_db() as conn:
        row = conn.execute(
            "SELECT c.*, i.name AS interface_name, i.type AS interface_type "
            "FROM conversations c JOIN interfaces i ON i.id = c.interface_id "
            "WHERE c.conversation_name=? LIMIT 1",
            (conversation_name,),
        ).fetchone()
    return _row_to_dict(row)


def conversation_get_by_kc_id(kc_conversation_id: int) -> dict | None:
    with get_db() as conn:
        row = conn.execute(
            "SELECT c.*, i.name AS interface_name, i.type AS interface_type "
            "FROM conversations c JOIN interfaces i ON i.id = c.interface_id "
            "WHERE c.kc_conversation_id=? LIMIT 1",
            (kc_conversation_id,),
        ).fetchone()
    return _row_to_dict(row)


def conversation_list_with_kc_id() -> list[dict]:
    """Return all routing conversations that have a linked KC conversation ID."""
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM conversations WHERE kc_conversation_id IS NOT NULL"
        ).fetchall()
    return [dict(r) for r in rows]


def conversation_delete(conv_id: int) -> None:
    with get_db() as conn:
        conn.execute("DELETE FROM conversations WHERE id=?", (conv_id,))


# ---------------------------------------------------------------------------
# External messages — deduplication and reply anchoring
# ---------------------------------------------------------------------------

def external_message_exists(external_message_id: str) -> bool:
    with get_db() as conn:
        row = conn.execute(
            "SELECT 1 FROM external_messages WHERE external_message_id=? LIMIT 1",
            (external_message_id,),
        ).fetchone()
    return row is not None


def external_message_create(
    conversation_id:     int,
    external_message_id: str,
    direction:           str,
    sender_display:      str = "",
) -> int:
    with get_db() as conn:
        cur = conn.execute(
            "INSERT OR IGNORE INTO external_messages "
            "(conversation_id, external_message_id, direction, sender_display, received_at) "
            "VALUES (?,?,?,?,?)",
            (conversation_id, external_message_id, direction, sender_display, _now()),
        )
    return cur.lastrowid  # type: ignore[return-value]


def external_message_get_last_inbound(conversation_id: int) -> dict | None:
    """Return the most recent inbound external message for reply anchoring."""
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM external_messages "
            "WHERE conversation_id=? AND direction='inbound' "
            "ORDER BY id DESC LIMIT 1",
            (conversation_id,),
        ).fetchone()
    return _row_to_dict(row)


# ---------------------------------------------------------------------------
# Activity log
# ---------------------------------------------------------------------------

def log_activity(action: str, detail: str | None = None) -> None:
    with get_db() as conn:
        conn.execute(
            "INSERT INTO activity_log (action, detail, logged_at) VALUES (?,?,?)",
            (action, detail, _now()),
        )


def activity_list(limit: int = 200) -> list[dict]:
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM activity_log ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
    return [dict(r) for r in rows]

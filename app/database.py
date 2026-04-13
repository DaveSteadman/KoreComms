"""SQLite data-access layer for KoreComms.

Each public function creates its own connection so it is safe to call
from any thread.  WAL mode is enabled for better read concurrency.
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
    external_thread_id TEXT,
    subject            TEXT,
    created_at         TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS messages (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id     INTEGER NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    direction           TEXT NOT NULL CHECK(direction IN ('inbound','outbound')),
    status              TEXT NOT NULL DEFAULT 'queued'
                            CHECK(status IN ('queued','processing','replied','ignored')),
    subject             TEXT,
    sender              TEXT,
    recipient           TEXT,
    content             TEXT NOT NULL,
    external_message_id TEXT,
    received_at         TEXT NOT NULL,
    handled_at          TEXT
);

CREATE TABLE IF NOT EXISTS activity_log (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    action     TEXT NOT NULL,
    message_id INTEGER REFERENCES messages(id),
    detail     TEXT,
    logged_at  TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_messages_status      ON messages(status);
CREATE INDEX IF NOT EXISTS idx_messages_conv        ON messages(conversation_id);
CREATE INDEX IF NOT EXISTS idx_conversations_iface  ON conversations(interface_id);
CREATE INDEX IF NOT EXISTS idx_messages_ext_id      ON messages(external_message_id);
"""


def init_db() -> None:
    """Create tables and seed the permanent Manual interface if absent."""
    with get_db() as conn:
        conn.executescript(_SCHEMA)
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

def conversation_find_or_create(
    iface_id: int,
    external_thread_id: str | None,
    subject: str | None = None,
) -> int:
    with get_db() as conn:
        if external_thread_id:
            row = conn.execute(
                "SELECT id FROM conversations "
                "WHERE interface_id=? AND external_thread_id=?",
                (iface_id, external_thread_id),
            ).fetchone()
            if row:
                return row["id"]
        cur = conn.execute(
            "INSERT INTO conversations (interface_id, external_thread_id, subject, created_at) "
            "VALUES (?,?,?,?)",
            (iface_id, external_thread_id, subject, _now()),
        )
        return cur.lastrowid  # type: ignore[return-value]


def conversation_get(conv_id: int) -> dict | None:
    with get_db() as conn:
        row = conn.execute("SELECT * FROM conversations WHERE id=?", (conv_id,)).fetchone()
    return _row_to_dict(row)


def conversation_delete(conv_id: int) -> None:
    """Delete a conversation, its messages, and related activity log entries."""
    with get_db() as conn:
        # Null out activity_log FK refs before deleting messages (no ON DELETE CASCADE on that FK).
        msg_ids = [
            r[0] for r in conn.execute(
                "SELECT id FROM messages WHERE conversation_id=?", (conv_id,)
            ).fetchall()
        ]
        if msg_ids:
            placeholders = ",".join("?" * len(msg_ids))
            conn.execute(
                f"UPDATE activity_log SET message_id=NULL WHERE message_id IN ({placeholders})",
                msg_ids,
            )
        conn.execute("DELETE FROM conversations WHERE id=?", (conv_id,))


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


# ---------------------------------------------------------------------------
# Messages
# ---------------------------------------------------------------------------

def message_create(
    conv_id: int,
    direction: str,
    content: str,
    subject: str | None = None,
    sender: str | None = None,
    recipient: str | None = None,
    external_message_id: str | None = None,
    status: str = "queued",
) -> int:
    with get_db() as conn:
        cur = conn.execute(
            "INSERT INTO messages "
            "(conversation_id, direction, status, subject, sender, recipient, "
            " content, external_message_id, received_at) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (conv_id, direction, status, subject, sender, recipient,
             content, external_message_id, _now()),
        )
        return cur.lastrowid  # type: ignore[return-value]


def message_get(msg_id: int) -> dict | None:
    with get_db() as conn:
        row = conn.execute("SELECT * FROM messages WHERE id=?", (msg_id,)).fetchone()
    return _row_to_dict(row)


def message_set_status(msg_id: int, status: str) -> None:
    handled_at = _now() if status in ("replied", "ignored") else None
    with get_db() as conn:
        conn.execute(
            "UPDATE messages SET status=?, handled_at=? WHERE id=?",
            (status, handled_at, msg_id),
        )


def message_get_thread(conv_id: int) -> list[dict]:
    """Return all messages in conversation order (oldest first)."""
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM messages WHERE conversation_id=? ORDER BY id ASC",
            (conv_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def message_list(limit: int = 200, offset: int = 0) -> list[dict]:
    with get_db() as conn:
        rows = conn.execute(
            "SELECT m.*, c.subject AS conv_subject, c.interface_id, "
            "       i.name AS interface_name, i.type AS interface_type "
            "FROM messages m "
            "JOIN conversations c ON c.id = m.conversation_id "
            "JOIN interfaces   i ON i.id = c.interface_id "
            "ORDER BY m.id DESC LIMIT ? OFFSET ?",
            (limit, offset),
        ).fetchall()
    return [dict(r) for r in rows]


def message_external_id_exists(external_id: str) -> bool:
    with get_db() as conn:
        row = conn.execute(
            "SELECT 1 FROM messages WHERE external_message_id=? LIMIT 1",
            (external_id,),
        ).fetchone()
    return row is not None


def messages_queued_ids() -> list[int]:
    """Return IDs of all QUEUED inbound messages in arrival order."""
    with get_db() as conn:
        rows = conn.execute(
            "SELECT id FROM messages WHERE status='queued' AND direction='inbound' ORDER BY id ASC"
        ).fetchall()
    return [r["id"] for r in rows]


def messages_has_any_processing() -> bool:
    """Return True if any inbound message is currently in PROCESSING state."""
    with get_db() as conn:
        row = conn.execute(
            "SELECT 1 FROM messages WHERE status='processing' AND direction='inbound' LIMIT 1"
        ).fetchone()
    return row is not None


def messages_reset_processing() -> None:
    """On startup, re-queue any message stuck in PROCESSING (from a prior crash)."""
    with get_db() as conn:
        conn.execute(
            "UPDATE messages SET status='queued', handled_at=NULL WHERE status='processing'"
        )


# ---------------------------------------------------------------------------
# Activity log
# ---------------------------------------------------------------------------

def log_activity(action: str, message_id: int | None = None, detail: str | None = None) -> None:
    with get_db() as conn:
        conn.execute(
            "INSERT INTO activity_log (action, message_id, detail, logged_at) VALUES (?,?,?,?)",
            (action, message_id, detail, _now()),
        )


def activity_list(limit: int = 200) -> list[dict]:
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM activity_log ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
    return [dict(r) for r in rows]

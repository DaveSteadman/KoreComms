"""Thread-safe in-memory message queue backed by the SQLite database.

On startup, all QUEUED inbound messages are loaded into the in-process
queue.  The poller pushes new message IDs in.  The agent API pops them
out via :func:`next_message`.
"""
from __future__ import annotations

import queue
import threading

from app import database as db

_q: queue.Queue[int] = queue.Queue()

# Lock protecting the GET /api/next-message operation so that concurrent
# callers (unlikely but possible) cannot both dequeue the same message.
_pop_lock = threading.Lock()


def bootstrap() -> None:
    """Call once at startup: reset stale PROCESSING rows, load QUEUED ids."""
    db.messages_reset_processing()
    for msg_id in db.messages_queued_ids():
        _q.put(msg_id)


def enqueue(message_id: int) -> None:
    """Mark message QUEUED in the DB and push it onto the in-memory queue."""
    db.message_set_status(message_id, "queued")
    _q.put(message_id)


def next_message() -> dict | None:
    """Pop the next message and return it with its full conversation thread.

    Returns ``None`` if the queue is empty OR if a message is already PROCESSING.
    Enforces the single-message-at-a-time invariant: the agent must call
    POST /api/complete before a new message is released.
    """
    with _pop_lock:
        # Enforce sequential processing: block if anything is still PROCESSING.
        if db.messages_has_any_processing():
            return None

        try:
            msg_id = _q.get_nowait()
        except queue.Empty:
            return None

        # Verify the row still exists and is still QUEUED (it may have been
        # manually re-queued or deleted via the WebUI).
        msg = db.message_get(msg_id)
        if msg is None or msg["status"] != "queued":
            return None

        db.message_set_status(msg_id, "processing")
        db.log_activity("fetched", msg_id)

        thread = db.message_get_thread(msg["conversation_id"])
        conv = db.conversation_get(msg["conversation_id"])
        iface = db.interface_get(conv["interface_id"]) if conv else None
        return {
            "message": msg,
            "conversation": conv,
            "thread": thread,
            "interface": iface,
        }


def queue_size() -> int:
    return _q.qsize()

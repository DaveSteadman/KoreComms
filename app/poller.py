"""Background polling thread.

Wakes up every *poll_interval* seconds, calls poll() on every enabled
interface, and enqueues any new message IDs.
"""
from __future__ import annotations

import logging
import threading
import time

from app import database as db, queue_manager
from app.config import cfg
from app.interfaces.registry import build_adapter

logger = logging.getLogger(__name__)

_thread: threading.Thread | None = None
_stop_event = threading.Event()


def _poll_once() -> None:
    interfaces = db.interface_list()
    for row in interfaces:
        if not row["enabled"]:
            continue
        try:
            adapter = build_adapter(row)
            new_ids = adapter.poll()
            for msg_id in new_ids:
                queue_manager.enqueue(msg_id)
                db.log_activity("polled", msg_id, f"via {row['name']}")
            if new_ids:
                logger.info("Interface '%s': %d new message(s)", row["name"], len(new_ids))
        except Exception as exc:
            logger.error("Poll error on interface '%s': %s", row["name"], exc)


def _run(interval: int) -> None:
    logger.info("Poller started (interval=%ds)", interval)
    while not _stop_event.is_set():
        try:
            _poll_once()
        except Exception as exc:
            logger.error("Unexpected poller error: %s", exc)
        _stop_event.wait(interval)
    logger.info("Poller stopped")


def start() -> None:
    global _thread
    _stop_event.clear()
    interval = int(cfg.get("poll_interval", 60))
    _thread = threading.Thread(target=_run, args=(interval,), daemon=True, name="poller")
    _thread.start()


def stop() -> None:
    _stop_event.set()
    if _thread:
        _thread.join(timeout=5)

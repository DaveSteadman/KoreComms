"""Background polling thread.

Two duties run on the same interval:

  1. _poll_inbound  — calls poll() on each enabled interface, forwards new
                      messages to KoreConversation, and stores thin routing
                      records locally for deduplication and reply anchoring.

  2. _poll_outbound — drains KoreConversation's outbound_ready event queue,
                      routing each agent response back through the correct
                      external interface.
"""
from __future__ import annotations

import logging
import threading
import time

from app import database as db, kc_client
from app.config import cfg
from app.interfaces.registry import build_adapter

logger = logging.getLogger(__name__)

_thread: threading.Thread | None = None
_stop_event = threading.Event()


# ---------------------------------------------------------------------------
# Inbound: external → KoreConversation
# ---------------------------------------------------------------------------

def _forward_message(iface_row: dict, msg: dict) -> None:
    """Store routing metadata locally and push the message to KoreConversation."""
    ext_msg_id    = msg["external_message_id"]
    ext_thread_id = msg["external_thread_id"]

    if db.external_message_exists(ext_msg_id):
        return  # Already forwarded.

    # Find or create the local routing entry.
    local_conv = db.conversation_get_by_external_thread(ext_thread_id)
    if local_conv is None:
        kc_conv = kc_client.find_or_create_conversation(
            external_id  = ext_thread_id,
            channel_type = msg.get("channel_type", "email"),
            subject      = msg.get("subject"),
        )
        local_conv_id = db.conversation_create(
            interface_id       = iface_row["id"],
            kc_conversation_id = kc_conv["id"],
            external_thread_id = ext_thread_id,
            subject            = msg.get("subject"),
        )
    else:
        kc_conv       = {"id": local_conv["kc_conversation_id"]}
        local_conv_id = local_conv["id"]

    # Record the external message for deduplication and reply anchoring.
    db.external_message_create(
        conversation_id     = local_conv_id,
        external_message_id = ext_msg_id,
        direction           = "inbound",
        sender_display      = msg.get("sender", ""),
    )

    # Append to KoreConversation and raise a response_needed event.
    kc_client.append_message(
        kc_conversation_id = kc_conv["id"],
        direction          = "inbound",
        content            = msg["content"],
        sender_display     = msg.get("sender", ""),
    )
    kc_client.create_event(kc_conv["id"], "response_needed")
    db.log_activity("forwarded", f"ext={ext_msg_id} kc_conv={kc_conv['id']}")
    logger.info("Forwarded message %s → KC conv %d", ext_msg_id, kc_conv["id"])


def _poll_inbound() -> None:
    interfaces = db.interface_list()
    for row in interfaces:
        if not row["enabled"]:
            continue
        try:
            adapter  = build_adapter(row)
            messages = adapter.poll()
            for msg_data in messages:
                _forward_message(row, msg_data)
        except Exception as exc:
            logger.error("Inbound poll error on interface '%s': %s", row["name"], exc)


# ---------------------------------------------------------------------------
# Outbound: KoreConversation → external interface
# ---------------------------------------------------------------------------

def _route_outbound_for_conversation(local_conv: dict) -> None:
    """Check a KC conversation for draft outbound messages and route them."""
    kc_conv_id = local_conv["kc_conversation_id"]
    try:
        messages = kc_client.get_messages(kc_conv_id, direction="outbound")
    except RuntimeError as exc:
        logger.error("KC get_messages failed for conv %d: %s", kc_conv_id, exc)
        return

    draft_messages = [m for m in messages if m.get("status") == "draft"]
    if not draft_messages:
        return

    iface_row = db.interface_get(local_conv["interface_id"])
    if iface_row is None:
        logger.error("Interface %d not found for conv %d", local_conv["interface_id"], kc_conv_id)
        return

    adapter = build_adapter(iface_row)

    for msg in draft_messages:
        try:
            adapter.route_reply(local_conv["id"], msg["content"])
            kc_client.mark_message_sent(msg["id"])
            db.external_message_create(
                conversation_id     = local_conv["id"],
                external_message_id = f"kc:{msg['id']}",
                direction           = "outbound",
            )
            db.log_activity("routed", f"kc_msg={msg['id']} via {iface_row['name']}")
            logger.info("Routed KC message %d via '%s'", msg["id"], iface_row["name"])
        except Exception as exc:
            logger.error("route_reply failed for KC message %d: %s", msg["id"], exc)


def _poll_outbound() -> None:
    """Poll each managed conversation for draft outbound messages."""
    conversations = db.conversation_list_with_kc_id()
    for conv in conversations:
        try:
            _route_outbound_for_conversation(conv)
        except Exception as exc:
            logger.error("Outbound poll error for conv %d: %s", conv["id"], exc)


# ---------------------------------------------------------------------------
# Polling loop
# ---------------------------------------------------------------------------

def _run(interval: int) -> None:
    logger.info("Poller started (interval=%ds)", interval)
    while not _stop_event.is_set():
        try:
            _poll_inbound()
            _poll_outbound()
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

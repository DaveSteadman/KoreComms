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
from app.interfaces.common.registry import build_adapter

logger = logging.getLogger(__name__)

_thread: threading.Thread | None = None
_stop_event = threading.Event()
_OUTBOUND_EVENT_POLL_SECS = float(cfg.get("event_poll_interval", 1.0))
_MISSING_KC_POLICY = str(cfg.get("missing_kc_conversation_policy", "recreate")).strip().lower()


def _conversation_name_for(local_conv: dict) -> str:
    name = (local_conv.get("conversation_name") or "").strip()
    if name:
        return name
    fallback = local_conv.get("external_thread_id") or f"kccomms:{local_conv['id']}"
    db.conversation_set_name(local_conv["id"], fallback)
    local_conv["conversation_name"] = fallback
    return fallback


def _resolve_kc_conversation(local_conv: dict) -> dict | None:
    conversation_name = _conversation_name_for(local_conv)
    kc_conv = kc_client.find_conversation_by_external_id(conversation_name)
    if kc_conv is None:
        db.conversation_set_kc_id(local_conv["id"], None)
        local_conv["kc_conversation_id"] = None
        if _MISSING_KC_POLICY == "abort":
            db.log_activity("agent_abort", f"conv={local_conv['id']} name={conversation_name} kc_missing")
            logger.warning(
                "Skipping KC recreation for local conv %d because policy=abort and name %s is missing",
                local_conv["id"],
                conversation_name,
            )
            return None
        kc_conv = kc_client.create_conversation(
            external_id=conversation_name,
            channel_type=local_conv.get("interface_type", "manual"),
            subject=local_conv.get("subject"),
        )
        logger.info(
            "Recreated KC conversation %d for local conv %d via name %s",
            kc_conv["id"],
            local_conv["id"],
            conversation_name,
        )
    if local_conv.get("kc_conversation_id") != kc_conv.get("id"):
        db.conversation_set_kc_id(local_conv["id"], kc_conv.get("id"))
        local_conv["kc_conversation_id"] = kc_conv.get("id")
    return kc_conv


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
        local_conv_id = db.conversation_create(
            interface_id=iface_row["id"],
            external_thread_id=ext_thread_id,
            subject=msg.get("subject"),
        )
        local_conv = db.conversation_get(local_conv_id)
        assert local_conv is not None
    else:
        local_conv_id = local_conv["id"]

    local_conv["interface_type"] = iface_row["type"]
    kc_conv = _resolve_kc_conversation(local_conv)
    if kc_conv is None:
        db.external_message_create(
            conversation_id=local_conv_id,
            external_message_id=ext_msg_id,
            direction="inbound",
            sender_display=msg.get("sender", ""),
        )
        return

    # Record the external message for deduplication and reply anchoring.
    db.external_message_create(
        conversation_id     = local_conv_id,
        external_message_id = ext_msg_id,
        direction           = "inbound",
        sender_display      = msg.get("sender", ""),
    )

    # Append to KoreConversation. The newer KC API raises response_needed itself.
    kc_client.append_message(
        kc_conversation_id = kc_conv["id"],
        direction          = "inbound",
        content            = msg["content"],
        sender_display     = msg.get("sender", ""),
    )
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
    kc_conv = _resolve_kc_conversation(local_conv)
    if kc_conv is None:
        return
    kc_conv_id = kc_conv["id"]
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


def _route_outbound_event(event: dict) -> None:
    """Handle one KoreConversation event addressed to KoreComms."""
    event_type = event.get("event_type")
    conversation = event.get("conversation") or {}
    kc_conv_id = event.get("conversation_id") or conversation.get("id")
    conversation_name = (conversation.get("external_id") or "").strip()

    if event_type == "conversation_deleted":
        local_conv = None
        if conversation_name:
            local_conv = db.conversation_get_by_name(conversation_name)
        if local_conv is None and kc_conv_id is not None:
            local_conv = db.conversation_get_by_kc_id(kc_conv_id)
        if local_conv is not None:
            db.conversation_set_kc_id(local_conv["id"], None)
            logger.info("Cleared KC binding for local conversation %d after KC deletion", local_conv["id"])
        kc_client.complete_event(event["id"], status="completed")
        return

    if event_type != "outbound_ready":
        kc_client.complete_event(event["id"], status="failed")
        logger.warning("Unexpected KC event type for KoreComms: %s", event_type)
        return

    if kc_conv_id is None:
        kc_client.complete_event(event["id"], status="failed")
        logger.error("Outbound event %s has no conversation_id", event.get("id"))
        return

    local_conv = None
    if conversation_name:
        local_conv = db.conversation_get_by_name(conversation_name)
    if local_conv is None:
        local_conv = db.conversation_get_by_kc_id(kc_conv_id)
    if local_conv is None:
        kc_client.complete_event(event["id"], status="failed")
        logger.error("No local conversation linked for KC conv %s", kc_conv_id)
        return

    try:
        draft_messages = [
            m for m in conversation.get("messages", [])
            if m.get("direction") == "outbound" and m.get("status") == "draft"
        ]
        if not draft_messages:
            messages = kc_client.get_messages(kc_conv_id, direction="outbound")
            draft_messages = [m for m in messages if m.get("status") == "draft"]

        if draft_messages:
            _route_outbound_for_conversation(local_conv)
        kc_client.complete_event(event["id"], status="completed")
    except Exception as exc:
        logger.error("Outbound event handling failed for KC conv %s: %s", kc_conv_id, exc)
        kc_client.complete_event(event["id"], status="failed")


def _drain_outbound_events() -> None:
    """Claim and handle all currently pending KoreConversation outbound events."""
    while not _stop_event.is_set():
        try:
            event = kc_client.claim_next_event()
        except RuntimeError as exc:
            logger.error("KC claim_next_event failed: %s", exc)
            return
        if event is None:
            return
        try:
            _route_outbound_event(event)
        except Exception as exc:
            logger.error("Unhandled outbound event error for event %s: %s", event.get("id"), exc)


# ---------------------------------------------------------------------------
# Polling loop
# ---------------------------------------------------------------------------

def _run(interval: int) -> None:
    logger.info(
        "Poller started (inbound_interval=%ss outbound_event_poll=%ss)",
        interval,
        _OUTBOUND_EVENT_POLL_SECS,
    )
    next_inbound_poll = 0.0
    while not _stop_event.is_set():
        try:
            now = time.monotonic()
            if now >= next_inbound_poll:
                _poll_inbound()
                next_inbound_poll = now + interval
            _drain_outbound_events()
        except Exception as exc:
            logger.error("Unexpected poller error: %s", exc)
        wait_for = max(0.1, min(_OUTBOUND_EVENT_POLL_SECS, next_inbound_poll - time.monotonic()))
        _stop_event.wait(wait_for)
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

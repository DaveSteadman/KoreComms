"""HTTP client for the KoreConversation service.

Uses stdlib urllib only — no extra dependencies.
Every public function raises RuntimeError on network / HTTP errors so the
caller can catch, log, and continue without crashing the poller thread.
"""
from __future__ import annotations

import json
import logging
import urllib.error
import urllib.parse
import urllib.request

from app.config import cfg

logger = logging.getLogger(__name__)

_CLAIMED_BY = "korecomms"


# ---------------------------------------------------------------------------
# Internal transport
# ---------------------------------------------------------------------------

def _base() -> str:
    return cfg["koreconversation_url"].rstrip("/")


def _post(path: str, payload: dict) -> dict:
    url  = f"{_base()}{path}"
    data = json.dumps(payload).encode()
    req  = urllib.request.Request(
        url, data=data, method="POST",
        headers={"Content-Type": "application/json", "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        body = exc.read().decode(errors="replace")
        raise RuntimeError(f"POST {path} → HTTP {exc.code}: {body}") from exc
    except OSError as exc:
        raise RuntimeError(f"POST {path} connection error: {exc}") from exc


def _get(path: str) -> tuple[int, bytes]:
    """Return (status_code, body_bytes). Raises RuntimeError on OS errors."""
    url = f"{_base()}{path}"
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        body = exc.read()
        if exc.code in (204, 404):
            return exc.code, b""
        raise RuntimeError(
            f"GET {path} → HTTP {exc.code}: {body.decode(errors='replace')}"
        ) from exc
    except OSError as exc:
        raise RuntimeError(f"GET {path} connection error: {exc}") from exc


def _patch(path: str, payload: dict) -> dict:
    url  = f"{_base()}{path}"
    data = json.dumps(payload).encode()
    req  = urllib.request.Request(
        url, data=data, method="PATCH",
        headers={"Content-Type": "application/json", "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        body = exc.read().decode(errors="replace")
        raise RuntimeError(f"PATCH {path} → HTTP {exc.code}: {body}") from exc
    except OSError as exc:
        raise RuntimeError(f"PATCH {path} connection error: {exc}") from exc


def _delete(path: str) -> None:
    url = f"{_base()}{path}"
    req = urllib.request.Request(
        url, method="DELETE", headers={"Accept": "application/json"}
    )
    try:
        urllib.request.urlopen(req, timeout=10).close()
    except urllib.error.HTTPError as exc:
        if exc.code not in (200, 204, 404):
            body = exc.read().decode(errors="replace")
            raise RuntimeError(f"DELETE {path} → HTTP {exc.code}: {body}") from exc
    except OSError as exc:
        raise RuntimeError(f"DELETE {path} connection error: {exc}") from exc


# ---------------------------------------------------------------------------
# Conversations
# ---------------------------------------------------------------------------

def find_conversation_by_external_id(external_id: str) -> dict | None:
    """Return an existing KC conversation by external_id, or None."""
    encoded = urllib.parse.quote(external_id, safe="")
    status_code, body = _get(f"/conversations/by-external-id/{encoded}")
    if status_code == 404 or not body:
        return None
    return json.loads(body)


def create_conversation(
    external_id:  str,
    channel_type: str = "email",
    subject:      str | None = None,
) -> dict:
    """Create a new KC conversation. Returns the full conversation record."""
    return _post("/conversations", {
        "channel_type": channel_type,
        "subject":      subject,
        "external_id":  external_id,
    })


def find_or_create_conversation(
    external_id:  str,
    channel_type: str = "email",
    subject:      str | None = None,
) -> dict:
    """Return the KC conversation matching external_id, creating it if absent."""
    existing = find_conversation_by_external_id(external_id)
    if existing is not None:
        return existing
    return create_conversation(external_id, channel_type, subject)


def get_conversation(kc_conversation_id: int) -> dict | None:
    """Return the full KC conversation record (includes unsummarised messages)."""
    status_code, body = _get(f"/conversations/{kc_conversation_id}")
    if status_code == 404 or not body:
        return None
    return json.loads(body)


def get_conversation_detail(kc_conversation_id: int) -> dict | None:
    """Return conversation, messages, and events in one KC round-trip."""
    status_code, body = _get(f"/conversations/{kc_conversation_id}/detail")
    if status_code == 404 or not body:
        return None
    return json.loads(body)


def delete_conversation(kc_conversation_id: int) -> None:
    _delete(f"/conversations/{kc_conversation_id}")


# ---------------------------------------------------------------------------
# Messages
# ---------------------------------------------------------------------------

def append_message(
    kc_conversation_id: int,
    direction:          str,
    content:            str,
    sender_display:     str = "",
) -> dict:
    """Append a message to a KC conversation. Returns the new message record."""
    return _post(
        f"/conversations/{kc_conversation_id}/messages",
        {
            "direction":      direction,
            "content":        content,
            "sender_display": sender_display,
        },
    )


def get_messages(
    kc_conversation_id: int,
    direction:          str | None = None,
) -> list[dict]:
    """Return messages for a KC conversation, optionally filtered by direction."""
    qs = f"?direction={direction}" if direction else ""
    status_code, body = _get(f"/conversations/{kc_conversation_id}/messages{qs}")
    if status_code == 404 or not body:
        return []
    return json.loads(body)


def get_input_history(kc_conversation_id: int) -> list[str]:
    """Return stored input-history entries for a KC conversation."""
    status_code, body = _get(f"/conversations/{kc_conversation_id}/input-history")
    if status_code == 404 or not body:
        return []
    payload = json.loads(body)
    entries = payload.get("entries", [])
    return entries if isinstance(entries, list) else []


def append_input_history(kc_conversation_id: int, text: str) -> list[str]:
    """Append a prompt to KC input history and return the updated entries."""
    payload = _patch(f"/conversations/{kc_conversation_id}/input-history", {"text": text})
    entries = payload.get("entries", [])
    return entries if isinstance(entries, list) else []


def mark_message_sent(kc_message_id: int) -> None:
    """Mark a KC message status as 'sent'."""
    _patch(f"/messages/{kc_message_id}", {"status": "sent"})


# ---------------------------------------------------------------------------
# Events
# ---------------------------------------------------------------------------

def create_event(
    kc_conversation_id: int,
    event_type:         str,
    payload:            dict | None = None,
) -> dict:
    return _post("/events", {
        "conversation_id": kc_conversation_id,
        "event_type":      event_type,
        "payload":         payload or {},
    })


def claim_next_event() -> dict | None:
    """Atomically claim the next pending event addressed to KoreComms.

    Returns a dict with ``id``, ``event_type``, ``conversation_id`` and
    (populated by KC) ``conversation`` with its unsummarised messages.
    Returns None when the queue is empty (HTTP 204).

    NOTE: KC's /events/next does not filter by event_type. KoreComms
    should inspect the returned event_type and complete with status='failed'
    if it receives an event it cannot handle (e.g. 'response_needed' which
    belongs to KoreAgent). This is a known gap to address in KC when an
    event_type filter parameter is added to /events/next.
    """
    status_code, body = _get(f"/events/next?claimed_by={_CLAIMED_BY}")
    if status_code == 204 or not body:
        return None
    return json.loads(body)


def complete_event(event_id: int, status: str = "completed") -> None:
    _post(f"/events/{event_id}/complete", {"status": status})

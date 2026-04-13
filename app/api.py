"""FastAPI application — agent REST API + WebUI routes."""
from __future__ import annotations

import json
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from app import crypto, database as db, poller, queue_manager
from app.config import cfg
from app.interfaces.registry import REGISTRY, build_adapter
from app.version import __version__

logger = logging.getLogger(__name__)

_TEMPLATES = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(_TEMPLATES))

# ---------------------------------------------------------------------------
# Lifespan
# ---------------------------------------------------------------------------


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
    queue_manager.bootstrap()
    poller.start()
    yield
    poller.stop()


app = FastAPI(title="KoreComms", version=__version__, lifespan=lifespan)


# ---------------------------------------------------------------------------
# Template context helper
# ---------------------------------------------------------------------------

def _ctx(**extra) -> dict:
    return {"version": __version__, **extra}


# ---------------------------------------------------------------------------
# Health / status
# ---------------------------------------------------------------------------


@app.get("/status")
def status():
    return {"status": "ok", "version": __version__, "queue": queue_manager.queue_size()}


# ---------------------------------------------------------------------------
# Agent REST API
# ---------------------------------------------------------------------------


@app.get("/api/next-message")
def api_next_message():
    result = queue_manager.next_message()
    if result is None:
        return JSONResponse(status_code=204, content=None)
    return result


class ReplyRequest(BaseModel):
    message_id: int
    content: str


@app.post("/api/reply")
def api_reply(req: ReplyRequest):
    msg = db.message_get(req.message_id)
    if msg is None:
        raise HTTPException(404, "Message not found")
    if msg["status"] != "processing":
        raise HTTPException(409, f"Message is not PROCESSING (status={msg['status']})")

    iface_row = _get_interface_for_message(req.message_id)
    adapter = build_adapter(iface_row)
    out_id = adapter.send_reply(req.message_id, req.content)
    return {"outbound_message_id": out_id}


class CompleteRequest(BaseModel):
    message_id: int
    status: str  # "replied" | "ignored"


@app.post("/api/complete")
def api_complete(req: CompleteRequest):
    if req.status not in ("replied", "ignored"):
        raise HTTPException(400, "status must be 'replied' or 'ignored'")
    msg = db.message_get(req.message_id)
    if msg is None:
        raise HTTPException(404, "Message not found")
    if msg["status"] not in ("processing", "replied"):
        raise HTTPException(409, f"Cannot complete message in status '{msg['status']}'")
    db.message_set_status(req.message_id, req.status)
    db.log_activity("completed", req.message_id, req.status)
    return {"ok": True}


class SendRequest(BaseModel):
    interface_id: int
    recipient: str
    subject: str
    content: str


@app.post("/api/send")
def api_send(req: SendRequest):
    iface_row = db.interface_get(req.interface_id)
    if iface_row is None:
        raise HTTPException(404, "Interface not found")
    adapter = build_adapter(iface_row)
    out_id = adapter.send_new(req.recipient, req.subject, req.content)
    return {"outbound_message_id": out_id}


# ---------------------------------------------------------------------------
# WebUI — message view (home)
# ---------------------------------------------------------------------------


@app.get("/", response_class=HTMLResponse)
def ui_home(request: Request, offset: int = 0):
    conversations = db.conversation_list(limit=50, offset=offset)
    # Attach latest message + full thread to each conversation.
    for conv in conversations:
        thread = db.message_get_thread(conv["id"])
        conv["thread"] = thread
        conv["latest"] = thread[-1] if thread else None
    return templates.TemplateResponse(
        request,
        "home.html",
        _ctx(conversations=conversations, offset=offset),
    )


# ---------------------------------------------------------------------------
# WebUI — compose / inject
# ---------------------------------------------------------------------------


@app.get("/compose", response_class=HTMLResponse)
def ui_compose_form(request: Request):
    return templates.TemplateResponse(request, "compose.html", _ctx())


@app.post("/compose")
def ui_compose_submit(
    request: Request,
    sender: str = Form(...),
    subject: str = Form(...),
    content: str = Form(...),
):
    manual = db.interface_get_manual()
    conv_id = db.conversation_find_or_create(manual["id"], None, subject)
    msg_id = db.message_create(
        conv_id=conv_id,
        direction="inbound",
        content=content,
        subject=subject,
        sender=sender,
    )
    queue_manager.enqueue(msg_id)
    db.log_activity("injected", msg_id, f"Manual inject from {sender}")
    return RedirectResponse("/", status_code=303)


# ---------------------------------------------------------------------------
# WebUI — connections
# ---------------------------------------------------------------------------


@app.get("/connections", response_class=HTMLResponse)
def ui_connections(request: Request):
    interfaces = db.interface_list()
    available_types = [t for t in REGISTRY if t != "manual"]
    return templates.TemplateResponse(
        request,
        "connections.html",
        _ctx(interfaces=interfaces, available_types=available_types),
    )


@app.get("/connections/new", response_class=HTMLResponse)
def ui_connections_new(request: Request, type: str = "gmail"):
    if type not in REGISTRY or type == "manual":
        raise HTTPException(400, "Unsupported interface type")
    return templates.TemplateResponse(
        request,
        "connection_edit.html",
        _ctx(iface=None, iface_type=type, poll_interval=cfg.get("poll_interval", 60)),
    )


@app.post("/connections/new")
def ui_connections_create(
    request: Request,
    iface_type: str = Form(...),
    name: str = Form(...),
    client_id: str = Form(default=""),
    client_secret: str = Form(default=""),
    poll_interval: int = Form(default=60),
):
    if iface_type not in REGISTRY or iface_type == "manual":
        raise HTTPException(400, "Unsupported interface type")
    config: dict = {"poll_interval": poll_interval}
    if iface_type == "gmail":
        config["client_id"] = crypto.encrypt(client_id) if client_id else ""
        config["client_secret"] = crypto.encrypt(client_secret) if client_secret else ""
    iface_id = db.interface_create(iface_type, name, config)
    return RedirectResponse(f"/connections/{iface_id}", status_code=303)


@app.get("/connections/{iface_id}", response_class=HTMLResponse)
def ui_connections_edit(request: Request, iface_id: int):
    iface = db.interface_get(iface_id)
    if iface is None:
        raise HTTPException(404, "Interface not found")
    config = json.loads(iface.get("config_json", "{}"))
    return templates.TemplateResponse(
        request,
        "connection_edit.html",
        _ctx(
            iface=iface,
            iface_type=iface["type"],
            config=config,
            poll_interval=config.get("poll_interval", cfg.get("poll_interval", 60)),
            gmail_authorized=bool(config.get("refresh_token")),
        ),
    )


@app.post("/connections/{iface_id}")
def ui_connections_update(
    request: Request,
    iface_id: int,
    name: str = Form(...),
    client_id: str = Form(default=""),
    client_secret: str = Form(default=""),
    poll_interval: int = Form(default=60),
    enabled: str = Form(default="off"),
):
    iface = db.interface_get(iface_id)
    if iface is None:
        raise HTTPException(404, "Interface not found")
    existing = json.loads(iface.get("config_json", "{}"))
    existing["poll_interval"] = poll_interval
    if iface["type"] == "gmail":
        # Only overwrite client_id/secret if new values provided.
        if client_id:
            existing["client_id"] = crypto.encrypt(client_id)
        if client_secret:
            existing["client_secret"] = crypto.encrypt(client_secret)
    db.interface_update(iface_id, name, existing, enabled == "on")
    return RedirectResponse("/connections", status_code=303)


@app.post("/connections/{iface_id}/delete")
def ui_connections_delete(request: Request, iface_id: int):
    iface = db.interface_get(iface_id)
    if iface is None:
        raise HTTPException(404, "Interface not found")
    if iface["type"] == "manual":
        raise HTTPException(400, "Cannot delete the Manual interface")
    db.interface_delete(iface_id)
    return RedirectResponse("/connections", status_code=303)


# ---------------------------------------------------------------------------
# Gmail OAuth flow
# ---------------------------------------------------------------------------


def _gmail_redirect_uri(request: Request) -> str:
    return str(request.base_url).rstrip("/") + "/gmail-callback"


@app.get("/connections/{iface_id}/gmail-authorize")
def ui_gmail_authorize(request: Request, iface_id: int):
    from app.interfaces.gmail import build_auth_url

    iface = db.interface_get(iface_id)
    if iface is None or iface["type"] != "gmail":
        raise HTTPException(404, "Gmail interface not found")
    config = json.loads(iface.get("config_json", "{}"))
    client_id = crypto.decrypt(config["client_id"]) if config.get("client_id") else ""
    client_secret = crypto.decrypt(config["client_secret"]) if config.get("client_secret") else ""
    if not client_id or not client_secret:
        raise HTTPException(400, "Add client_id and client_secret first")
    redirect_uri = _gmail_redirect_uri(request)
    auth_url = build_auth_url(client_id, client_secret, redirect_uri, str(iface_id))
    return RedirectResponse(auth_url)


@app.get("/gmail-callback", response_class=HTMLResponse)
def ui_gmail_callback(request: Request, code: str = "", state: str = "", error: str = ""):
    if error:
        return templates.TemplateResponse(
            request,
            "connections.html",
            _ctx(
                interfaces=db.interface_list(),
                available_types=[t for t in REGISTRY if t != "manual"],
                flash=f"OAuth error: {error}",
            ),
        )
    from app.interfaces.gmail import exchange_code

    iface_id = int(state)
    iface = db.interface_get(iface_id)
    if iface is None:
        raise HTTPException(404)
    config = json.loads(iface.get("config_json", "{}"))
    client_id = crypto.decrypt(config["client_id"]) if config.get("client_id") else ""
    client_secret = crypto.decrypt(config["client_secret"]) if config.get("client_secret") else ""
    redirect_uri = _gmail_redirect_uri(request)
    refresh_token = exchange_code(client_id, client_secret, redirect_uri, code)
    config["refresh_token"] = crypto.encrypt(refresh_token)
    db.interface_update(iface_id, iface["name"], config, bool(iface["enabled"]))
    return RedirectResponse(f"/connections/{iface_id}", status_code=303)


# ---------------------------------------------------------------------------
# WebUI — state editor
# ---------------------------------------------------------------------------


@app.get("/state", response_class=HTMLResponse)
def ui_state(request: Request, offset: int = 0):
    messages = db.message_list(limit=100, offset=offset)
    return templates.TemplateResponse(
        request,
        "state_editor.html",
        _ctx(messages=messages, offset=offset),
    )


@app.post("/state/{msg_id}/requeue")
def ui_state_requeue(msg_id: int):
    msg = db.message_get(msg_id)
    if msg is None:
        raise HTTPException(404, "Message not found")
    queue_manager.enqueue(msg_id)
    return RedirectResponse("/state", status_code=303)


@app.post("/state/{msg_id}/set-status")
def ui_state_set_status(msg_id: int, new_status: str = Form(...)):
    allowed = ("queued", "processing", "replied", "ignored")
    if new_status not in allowed:
        raise HTTPException(400, f"Status must be one of: {allowed}")
    msg = db.message_get(msg_id)
    if msg is None:
        raise HTTPException(404, "Message not found")
    db.message_set_status(msg_id, new_status)
    if new_status == "queued":
        queue_manager.enqueue(msg_id)
    return RedirectResponse("/state", status_code=303)


# ---------------------------------------------------------------------------
# WebUI — activity log
# ---------------------------------------------------------------------------


@app.get("/activity", response_class=HTMLResponse)
def ui_activity(request: Request):
    entries = db.activity_list(limit=200)
    return templates.TemplateResponse(request, "activity_log.html", _ctx(entries=entries))


# ---------------------------------------------------------------------------
# WebUI — per-conversation chat view
# ---------------------------------------------------------------------------


@app.get("/api/conversation/{conv_id}")
def api_conversation(conv_id: int):
    conv = db.conversation_get(conv_id)
    if conv is None:
        raise HTTPException(404, "Conversation not found")
    thread = db.message_get_thread(conv_id)
    return {"conversation": conv, "thread": thread}


@app.get("/conversation/{conv_id}", response_class=HTMLResponse)
def ui_conversation(request: Request, conv_id: int):
    conv = db.conversation_get(conv_id)
    if conv is None:
        raise HTTPException(404, "Conversation not found")
    iface = db.interface_get(conv["interface_id"])
    thread = db.message_get_thread(conv_id)
    return templates.TemplateResponse(
        request,
        "chat.html",
        _ctx(conv=conv, iface=iface, thread=thread),
    )


@app.post("/conversation/{conv_id}/delete")
def ui_conversation_delete(request: Request, conv_id: int):
    """Delete conversation + all messages, then clear the MAF session if configured."""
    import urllib.request as _ureq
    conv = db.conversation_get(conv_id)
    if conv is None:
        raise HTTPException(404, "Conversation not found")
    # Attempt to clear the MAF session for this conversation.
    maf_url = cfg.get("maf_url", "").strip().rstrip("/")
    if maf_url:
        session_id = f"korecomms_conv_{conv_id}"
        try:
            req = _ureq.Request(
                f"{maf_url}/sessions/{session_id}",
                method="DELETE",
                headers={"Accept": "application/json"},
            )
            _ureq.urlopen(req, timeout=5)
        except Exception as exc:
            logger.warning("MAF session delete failed for %s: %s", session_id, exc)
    db.conversation_delete(conv_id)
    return RedirectResponse("/", status_code=303)


@app.post("/conversation/{conv_id}/send")
def ui_conversation_send(
    request: Request,
    conv_id: int,
    content: str = Form(...),
):
    """Human sends a new message in the chat — queued as inbound for the agent."""
    conv = db.conversation_get(conv_id)
    if conv is None:
        raise HTTPException(404, "Conversation not found")
    if not content.strip():
        return RedirectResponse(f"/conversation/{conv_id}", status_code=303)
    msg_id = db.message_create(
        conv_id=conv_id,
        direction="inbound",
        content=content.strip(),
        subject=conv.get("subject"),
        sender="Human",
        status="queued",
    )
    queue_manager.enqueue(msg_id)
    db.log_activity("injected", msg_id, "Human reply via chat UI")
    return RedirectResponse(f"/conversation/{conv_id}", status_code=303)


# ---------------------------------------------------------------------------
# Internal helper
# ---------------------------------------------------------------------------


def _get_interface_for_message(message_id: int) -> dict:
    msg = db.message_get(message_id)
    if msg is None:
        raise HTTPException(404, "Message not found")
    conv = db.conversation_get(msg["conversation_id"])
    if conv is None:
        raise HTTPException(500, "Conversation not found")
    iface = db.interface_get(conv["interface_id"])
    if iface is None:
        raise HTTPException(500, "Interface not found")
    return iface

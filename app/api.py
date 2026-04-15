"""FastAPI application â€” WebUI routes + KoreComms REST API."""
from __future__ import annotations

import json
import logging
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from app import crypto, database as db, kc_client, poller, queue_manager
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
    return {"status": "ok", "version": __version__}


# ---------------------------------------------------------------------------
# KoreComms REST API â€” outbound trigger
# ---------------------------------------------------------------------------


class SendRequest(BaseModel):
    interface_id: int
    recipient:    str
    subject:      str
    content:      str


@app.post("/api/send")
def api_send(req: SendRequest):
    """Initiate a brand-new outbound message on a specified interface."""
    iface_row = db.interface_get(req.interface_id)
    if iface_row is None:
        raise HTTPException(404, "Interface not found")

    adapter = build_adapter(iface_row)
    routing = adapter.send_new(req.recipient, req.subject, req.content)

    ext_thread_id = routing["external_thread_id"]
    ext_msg_id    = routing.get("external_message_id", ext_thread_id)

    kc_conv = kc_client.find_or_create_conversation(
        external_id  = ext_thread_id,
        channel_type = iface_row["type"],
        subject      = req.subject,
    )
    local_conv_id = db.conversation_create(
        interface_id       = req.interface_id,
        kc_conversation_id = kc_conv["id"],
        external_thread_id = ext_thread_id,
        subject            = req.subject,
    )
    kc_msg = kc_client.append_message(
        kc_conversation_id = kc_conv["id"],
        direction          = "outbound",
        content            = req.content,
        sender_display     = "KoreComms",
    )
    kc_client.mark_message_sent(kc_msg["id"])
    db.external_message_create(local_conv_id, ext_msg_id, "outbound")
    db.log_activity("send_new", f"via {iface_row['name']} to {req.recipient}")
    return {"conversation_id": local_conv_id, "kc_conversation_id": kc_conv["id"]}


# ---------------------------------------------------------------------------
# WebUI â€” home (conversation list)
# ---------------------------------------------------------------------------


@app.get("/", response_class=HTMLResponse)
def ui_home(request: Request, offset: int = 0):
    conversations = db.conversation_list(limit=50, offset=offset)
    return templates.TemplateResponse(
        request,
        "home.html",
        _ctx(conversations=conversations, offset=offset),
    )


# ---------------------------------------------------------------------------
# WebUI â€” compose / inject manual message
# ---------------------------------------------------------------------------


@app.get("/compose", response_class=HTMLResponse)
def ui_compose_form(request: Request):
    return templates.TemplateResponse(request, "compose.html", _ctx())


@app.post("/compose")
def ui_compose_submit(
    request: Request,
    sender:  str = Form(...),
    subject: str = Form(...),
    content: str = Form(...),
):
    manual = db.interface_get_manual()
    ext_thread_id = f"manual:{uuid.uuid4()}"

    kc_conv = kc_client.find_or_create_conversation(
        external_id  = ext_thread_id,
        channel_type = "manual",
        subject      = subject,
    )
    local_conv_id = db.conversation_create(
        interface_id       = manual["id"],
        kc_conversation_id = kc_conv["id"],
        external_thread_id = ext_thread_id,
        subject            = subject,
    )
    ext_msg_id = f"{ext_thread_id}:0"
    db.external_message_create(local_conv_id, ext_msg_id, "inbound", sender)

    kc_client.append_message(kc_conv["id"], "inbound", content, sender_display=sender)
    kc_client.create_event(kc_conv["id"], "response_needed")
    db.log_activity("injected", f"Manual inject from {sender}")
    return RedirectResponse(f"/conversation/{local_conv_id}", status_code=303)


# ---------------------------------------------------------------------------
# WebUI â€” connections (interface management)
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
    request:       Request,
    iface_type:    str = Form(...),
    name:          str = Form(...),
    client_id:     str = Form(default=""),
    client_secret: str = Form(default=""),
    poll_interval: int = Form(default=60),
):
    if iface_type not in REGISTRY or iface_type == "manual":
        raise HTTPException(400, "Unsupported interface type")
    config: dict = {"poll_interval": poll_interval}
    if iface_type == "gmail":
        config["client_id"]     = crypto.encrypt(client_id)     if client_id     else ""
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
            iface          = iface,
            iface_type     = iface["type"],
            config         = config,
            poll_interval  = config.get("poll_interval", cfg.get("poll_interval", 60)),
            gmail_authorized = bool(config.get("refresh_token")),
        ),
    )


@app.post("/connections/{iface_id}")
def ui_connections_update(
    request:       Request,
    iface_id:      int,
    name:          str = Form(...),
    client_id:     str = Form(default=""),
    client_secret: str = Form(default=""),
    poll_interval: int = Form(default=60),
    enabled:       str = Form(default="off"),
):
    iface = db.interface_get(iface_id)
    if iface is None:
        raise HTTPException(404, "Interface not found")
    existing = json.loads(iface.get("config_json", "{}"))
    existing["poll_interval"] = poll_interval
    if iface["type"] == "gmail":
        if client_id:
            existing["client_id"]     = crypto.encrypt(client_id)
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
    client_id     = crypto.decrypt(config["client_id"])     if config.get("client_id")     else ""
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
                interfaces      = db.interface_list(),
                available_types = [t for t in REGISTRY if t != "manual"],
                flash           = f"OAuth error: {error}",
            ),
        )
    from app.interfaces.gmail import exchange_code

    iface_id = int(state)
    iface = db.interface_get(iface_id)
    if iface is None:
        raise HTTPException(404)
    config = json.loads(iface.get("config_json", "{}"))
    client_id     = crypto.decrypt(config["client_id"])     if config.get("client_id")     else ""
    client_secret = crypto.decrypt(config["client_secret"]) if config.get("client_secret") else ""
    redirect_uri  = _gmail_redirect_uri(request)
    refresh_token = exchange_code(client_id, client_secret, redirect_uri, code)
    config["refresh_token"] = crypto.encrypt(refresh_token)
    db.interface_update(iface_id, iface["name"], config, bool(iface["enabled"]))
    return RedirectResponse(f"/connections/{iface_id}", status_code=303)


# ---------------------------------------------------------------------------
# WebUI â€” activity log
# ---------------------------------------------------------------------------


@app.get("/activity", response_class=HTMLResponse)
def ui_activity(request: Request):
    entries = db.activity_list(limit=200)
    return templates.TemplateResponse(request, "activity_log.html", _ctx(entries=entries))


# ---------------------------------------------------------------------------
# WebUI â€” per-conversation chat view
# ---------------------------------------------------------------------------


def _normalize_kc_messages(kc_messages: list[dict]) -> list[dict]:
    """Map KC message fields to the shape the chat template expects."""
    return [
        {
            "id":          m["id"],
            "direction":   m["direction"],
            "content":     m["content"],
            "sender":      m.get("sender_display", ""),
            "received_at": m.get("created_at", ""),
            "status":      m.get("status", ""),
        }
        for m in kc_messages
    ]


def _ensure_kc_conv(conv: dict) -> int:
    """Return kc_conversation_id, creating the KC conversation lazily if needed.

    Pre-refactor rows have kc_conversation_id=NULL.  Rather than failing, we
    create a KC conversation from the local metadata and link it back.
    """
    kc_id = conv.get("kc_conversation_id")
    if kc_id is not None:
        return kc_id

    ext_thread = conv.get("external_thread_id") or f"legacy:{conv['id']}"
    channel    = conv.get("interface_type", "manual")
    subject    = conv.get("subject") or ""
    kc_conv    = kc_client.find_or_create_conversation(ext_thread, channel, subject)
    db.conversation_set_kc_id(conv["id"], kc_conv["id"])
    logger.info("Lazily linked local conv %d → kc_conv %d", conv["id"], kc_conv["id"])
    return kc_conv["id"]


@app.get("/api/conversation/{conv_id}")
def api_conversation(conv_id: int):
    conv = db.conversation_get(conv_id)
    if conv is None:
        raise HTTPException(404, "Conversation not found")
    try:
        kc_conv_id = _ensure_kc_conv(conv)
        kc_data = kc_client.get_conversation(kc_conv_id)
    except RuntimeError as exc:
        raise HTTPException(502, f"KoreConversation unavailable: {exc}")
    if kc_data is None:
        return {"conversation": conv, "thread": []}
    thread = _normalize_kc_messages(kc_data.get("messages", []))
    return {"conversation": kc_data, "thread": thread}


@app.get("/conversation/{conv_id}", response_class=HTMLResponse)
def ui_conversation(request: Request, conv_id: int):
    conv = db.conversation_get(conv_id)
    if conv is None:
        raise HTTPException(404, "Conversation not found")

    iface  = db.interface_get(conv["interface_id"])
    thread: list[dict] = []
    kc_data: dict = {}

    try:
        kc_conv_id = _ensure_kc_conv(conv)
        kc_data = kc_client.get_conversation(kc_conv_id) or {}
        thread  = _normalize_kc_messages(kc_data.get("messages", []))
    except RuntimeError as exc:
        logger.warning("KC fetch failed for conv %d: %s", conv_id, exc)

    return templates.TemplateResponse(
        request,
        "chat.html",
        _ctx(conv=conv, iface=iface, thread=thread, kc_conv=kc_data),
    )


@app.post("/conversation/{conv_id}/delete")
def ui_conversation_delete(request: Request, conv_id: int):
    conv = db.conversation_get(conv_id)
    if conv is None:
        raise HTTPException(404, "Conversation not found")
    kc_conv_id = conv.get("kc_conversation_id")
    if kc_conv_id is not None:
        try:
            kc_client.delete_conversation(kc_conv_id)
        except RuntimeError as exc:
            logger.warning("KC delete failed for conv %d: %s", conv_id, exc)
    db.conversation_delete(conv_id)
    db.log_activity("deleted", f"conv={conv_id} kc_conv={kc_conv_id}")
    return RedirectResponse("/", status_code=303)


@app.post("/conversation/{conv_id}/send")
def ui_conversation_send(
    request: Request,
    conv_id: int,
    content: str = Form(...),
):
    """Human sends a message in an existing conversation â€” forwarded to KC."""
    conv = db.conversation_get(conv_id)
    if conv is None:
        raise HTTPException(404, "Conversation not found")
    if not content.strip():
        return RedirectResponse(f"/conversation/{conv_id}", status_code=303)

    try:
        kc_conv_id = _ensure_kc_conv(conv)
        kc_client.append_message(kc_conv_id, "inbound", content.strip(), "Human")
        kc_client.create_event(kc_conv_id, "response_needed")
    except RuntimeError as exc:
        raise HTTPException(502, f"KoreConversation unavailable: {exc}")

    db.log_activity("injected", f"Human reply in conv={conv_id}")
    return RedirectResponse(f"/conversation/{conv_id}", status_code=303)

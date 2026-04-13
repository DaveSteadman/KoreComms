"""Manual interface — synthetic channel used for local testing via the WebUI.

Inbound messages are injected by a human via the WebUI compose form.
Outbound replies are stored in the database only; nothing is transmitted
externally.
"""
from __future__ import annotations

from app import database as db
from app.interfaces.base import BaseInterface


class ManualInterface(BaseInterface):

    def poll(self) -> list[int]:
        # Messages come in through the WebUI compose route, not polling.
        return []

    def send_reply(self, message_id: int, content: str) -> int:
        msg = db.message_get(message_id)
        if msg is None:
            raise ValueError(f"Message {message_id} not found")
        out_id = db.message_create(
            conv_id=msg["conversation_id"],
            direction="outbound",
            content=content,
            subject=msg.get("subject"),
            sender="KoreComms (Manual)",
            recipient=msg.get("sender"),
            status="replied",
        )
        db.log_activity("replied", message_id, f"Manual reply → message {out_id}")
        return out_id

    def send_new(self, recipient: str, subject: str, content: str) -> int:
        conv_id = db.conversation_find_or_create(self.interface_id, None, subject)
        out_id = db.message_create(
            conv_id=conv_id,
            direction="outbound",
            content=content,
            subject=subject,
            sender="KoreComms (Manual)",
            recipient=recipient,
            status="replied",
        )
        db.log_activity("sent", out_id, f"Manual new message to {recipient}")
        return out_id

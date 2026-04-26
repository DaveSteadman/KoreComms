"""Manual interface — synthetic channel used for local testing via the WebUI.

Inbound messages are injected by a human via the WebUI compose form.
Outbound replies are acknowledged but not transmitted anywhere externally;
the reply content lives in KoreConversation and is visible through the chat UI.
"""
from __future__ import annotations

import uuid

from app.interfaces.common.base import BaseInterface


class ManualInterface(BaseInterface):

    def poll(self) -> list[dict]:
        return []

    def route_reply(self, conversation_id: int, content: str) -> None:
        pass

    def send_new(self, recipient: str, subject: str, content: str) -> dict:
        thread_id = f"manual:{uuid.uuid4()}"
        return {
            "external_thread_id": thread_id,
            "external_message_id": f"{thread_id}:0",
        }
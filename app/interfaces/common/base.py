"""Abstract base class for all KoreComms interface adapters.

To add a new interface type:
  1. Create a package under app/interfaces/ for that type.
  2. Subclass BaseInterface in that package.
  3. Register the type string in app/interfaces/common/registry.py.
"""
from __future__ import annotations

from abc import ABC, abstractmethod


class BaseInterface(ABC):
    """Adapter contract that every interface type must satisfy."""

    def __init__(self, interface_id: int, name: str, config: dict) -> None:
        self.interface_id = interface_id
        self.name = name
        self.config = config

    @abstractmethod
    def poll(self) -> list[dict]:
        """Fetch new inbound messages from this channel.

        Returns a list of message dicts; each must contain:
            external_message_id  str       – stable, unique ID for de-duplication
            external_thread_id   str       – groups messages into a conversation
            sender               str       – display name / address
            subject              str|None  – subject line if applicable
            content              str       – plain-text body
            channel_type         str       – e.g. "email", "manual"

        Must be idempotent (safe to call repeatedly).
        Must NOT write to the database directly.
        """
        ...

    @abstractmethod
    def route_reply(self, conversation_id: int, content: str) -> None:
        """Deliver *content* as a reply through this channel.

        *conversation_id* is the local KoreComms conversation ID. The adapter
        may read routing metadata (external thread ID, recipient, etc.) from
        the database, but must not write message content.
        """
        ...

    @abstractmethod
    def send_new(self, recipient: str, subject: str, content: str) -> dict:
        """Send a brand-new outbound message through this channel.

        Returns a dict containing at minimum:
            external_thread_id  str  – channel reference for future replies
            external_message_id str  – unique ID of the sent message
        """
        ...
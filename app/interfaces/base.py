"""Abstract base class for all KoreComms interface adapters.

To add a new interface type:
  1. Subclass BaseInterface in a new module under app/interfaces/.
  2. Implement poll(), send_reply(), and send_new().
  3. Register the type string in app/interfaces/registry.py.
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
    def poll(self) -> list[int]:
        """Fetch new inbound messages.

        Inserts discovered messages into the database and returns the list of
        new message IDs.  Must be idempotent (safe to call repeatedly).
        """
        ...

    @abstractmethod
    def send_reply(self, message_id: int, content: str) -> int:
        """Send a reply to *message_id* and return the new outbound message ID."""
        ...

    @abstractmethod
    def send_new(self, recipient: str, subject: str, content: str) -> int:
        """Send a brand-new outbound message and return the new message ID."""
        ...

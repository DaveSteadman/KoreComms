"""Queue manager — stub.

The inbound message queue has moved to KoreConversation. This module is
retained as a no-op to preserve compatibility with any remaining imports.
"""
from __future__ import annotations


def bootstrap() -> None:
    pass


def queue_size() -> int:
    return 0

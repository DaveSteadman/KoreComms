"""Interface type registry.

Maps the string type name (stored in interfaces.type) to the adapter class.
To register a new interface type, add it to REGISTRY.
"""
from __future__ import annotations

from app.interfaces.common.base import BaseInterface
from app.interfaces.discord import DiscordInterface
from app.interfaces.gmail import GmailInterface
from app.interfaces.manual import ManualInterface

REGISTRY: dict[str, type[BaseInterface]] = {
    "manual": ManualInterface,
    "discord": DiscordInterface,
    "gmail": GmailInterface,
}


def build_adapter(row: dict) -> BaseInterface:
    """Construct the correct adapter from a database interfaces row."""
    cls = REGISTRY.get(row["type"])
    if cls is None:
        raise ValueError(f"Unknown interface type: {row['type']!r}")
    return cls(
        interface_id=row["id"],
        name=row["name"],
        config=row,
    )
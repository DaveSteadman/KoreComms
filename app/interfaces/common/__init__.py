"""Shared interface abstractions and registry helpers."""

from app.interfaces.common.base import BaseInterface
from app.interfaces.common.registry import REGISTRY, build_adapter

__all__ = ["BaseInterface", "REGISTRY", "build_adapter"]
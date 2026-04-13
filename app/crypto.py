"""Encryption helpers for sensitive values stored in the database.

The Fernet key is stored in config/secret.key (git-ignored).
If the file does not exist it is generated on first use.
"""
from __future__ import annotations

import os
from pathlib import Path

from cryptography.fernet import Fernet

_KEY_PATH = Path("config/secret.key")
_fernet: Fernet | None = None


def _get_fernet() -> Fernet:
    global _fernet
    if _fernet is not None:
        return _fernet
    _KEY_PATH.parent.mkdir(parents=True, exist_ok=True)
    if _KEY_PATH.exists():
        key = _KEY_PATH.read_bytes().strip()
    else:
        key = Fernet.generate_key()
        _KEY_PATH.write_bytes(key)
        # Restrict permissions on POSIX systems.
        try:
            os.chmod(_KEY_PATH, 0o600)
        except AttributeError:
            pass  # Windows — no-op
    _fernet = Fernet(key)
    return _fernet


def encrypt(plaintext: str) -> str:
    """Encrypt *plaintext* and return a base64 token string."""
    return _get_fernet().encrypt(plaintext.encode()).decode()


def decrypt(token: str) -> str:
    """Decrypt a token produced by :func:`encrypt` and return the plaintext."""
    return _get_fernet().decrypt(token.encode()).decode()

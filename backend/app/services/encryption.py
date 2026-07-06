"""Symmetric credential encryption.

Stored inbox passwords and tenant provider keys MUST go through this
module and nowhere else.  Decrypted plaintext lives only in the local
scope of the caller — never log, return from API responses, or persist it.

Key rotation (Phase 7): ``ENCRYPTION_KEY`` is the PRIMARY (all new
encryptions); ``ENCRYPTION_KEYS_OLD`` (comma-separated) are retired keys
still accepted for decryption via MultiFernet.  Rotation runbook:
1. move the current key into ENCRYPTION_KEYS_OLD, set a fresh
   ENCRYPTION_KEY, recreate the containers;
2. run ``python scripts/rotate_encryption.py`` to re-encrypt every stored
   secret under the new primary;
3. drop the old key from ENCRYPTION_KEYS_OLD.
"""
from __future__ import annotations

from cryptography.fernet import Fernet, InvalidToken, MultiFernet

from app.config import settings

_fernet: MultiFernet | None = None


def _get_fernet() -> MultiFernet:
    global _fernet
    if _fernet is None:
        key = settings.ENCRYPTION_KEY
        if not key:
            raise RuntimeError(
                "ENCRYPTION_KEY is not configured. Generate one with: "
                'python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"'
            )
        keys = [Fernet(key.encode() if isinstance(key, str) else key)]
        for old in (settings.ENCRYPTION_KEYS_OLD or "").split(","):
            old = old.strip()
            if old:
                keys.append(Fernet(old.encode()))
        _fernet = MultiFernet(keys)
    return _fernet


def encrypt(plaintext: str) -> str:
    """Encrypt a UTF-8 string; return a URL-safe base64 Fernet token
    (always under the PRIMARY key)."""
    return _get_fernet().encrypt(plaintext.encode("utf-8")).decode("ascii")


def decrypt(token: str) -> str:
    """Decrypt a Fernet token (primary key first, then retired keys).
    Raises cryptography.fernet.InvalidToken on failure."""
    return _get_fernet().decrypt(token.encode("ascii")).decode("utf-8")


__all__ = ["encrypt", "decrypt", "InvalidToken"]

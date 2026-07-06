"""Password hashing — argon2id via argon2-cffi, used directly.

Deliberately NOT passlib (unmaintained since 2020; broken against
modern bcrypt).  The default argon2 parameters (argon2id, 64MB, t=3)
are the library's RFC-9106-derived choices — don't tune them down.
"""
from __future__ import annotations

from argon2 import PasswordHasher
from argon2.exceptions import VerificationError, VerifyMismatchError

_hasher = PasswordHasher()


def hash_password(plaintext: str) -> str:
    return _hasher.hash(plaintext)


def verify_password(password_hash: str, plaintext: str) -> bool:
    """Constant-time verify; False on mismatch or malformed hash."""
    try:
        return _hasher.verify(password_hash, plaintext)
    except (VerifyMismatchError, VerificationError):
        return False


def needs_rehash(password_hash: str) -> bool:
    """True when the stored hash predates current parameters (re-hash on
    next successful login)."""
    try:
        return _hasher.check_needs_rehash(password_hash)
    except Exception:  # malformed legacy hash — force re-hash
        return True

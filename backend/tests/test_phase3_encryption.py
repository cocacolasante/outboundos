"""Phase 3: Fernet encryption service."""
import pytest
from cryptography.fernet import InvalidToken

from app.services import encryption


def test_encrypt_decrypt_roundtrip():
    plaintext = "hunter2-app-password"
    token = encryption.encrypt(plaintext)
    assert isinstance(token, str)
    assert token != plaintext
    assert encryption.decrypt(token) == plaintext


def test_each_encrypt_call_produces_different_token():
    # Fernet embeds a random IV/nonce, so identical inputs yield distinct tokens.
    t1 = encryption.encrypt("same-input")
    t2 = encryption.encrypt("same-input")
    assert t1 != t2
    assert encryption.decrypt(t1) == encryption.decrypt(t2) == "same-input"


def test_decrypt_garbage_raises_invalid_token():
    with pytest.raises(InvalidToken):
        encryption.decrypt("not-a-real-fernet-token")


def test_decrypt_with_wrong_key_raises_invalid_token():
    from cryptography.fernet import Fernet

    other = Fernet(Fernet.generate_key())
    foreign_token = other.encrypt(b"secret").decode()
    with pytest.raises(InvalidToken):
        encryption.decrypt(foreign_token)


def test_unicode_roundtrip():
    plaintext = "пароль🔑—мнöго байт"
    assert encryption.decrypt(encryption.encrypt(plaintext)) == plaintext

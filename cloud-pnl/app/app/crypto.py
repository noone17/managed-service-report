"""
Symmetric encryption for AWS credentials stored in the database.

Uses Fernet (AES-128-CBC + HMAC-SHA256) from the `cryptography` library.
The FERNET_KEY environment variable must be set before the app starts.

Usage:
    from app.crypto import encrypt, decrypt

    cipher  = encrypt("AKIAIOSFODNN7EXAMPLE")   # → bytes, store in DB as TEXT
    plain   = decrypt(cipher)                    # → "AKIAIOSFODNN7EXAMPLE"
"""

import os
from cryptography.fernet import Fernet, InvalidToken

_fernet: Fernet | None = None


def _get_fernet() -> Fernet:
    global _fernet
    if _fernet is not None:
        return _fernet

    key = os.getenv("FERNET_KEY", "").strip()
    if not key or key == "REPLACE_WITH_GENERATED_KEY":
        raise RuntimeError(
            "FERNET_KEY is not set in .env. "
            "Generate one with: python -c \"from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())\""
        )

    _fernet = Fernet(key.encode())
    return _fernet


def encrypt(plaintext: str) -> str:
    """Encrypt a plaintext string and return a URL-safe base64 token (str)."""
    if not plaintext:
        return ""
    token: bytes = _get_fernet().encrypt(plaintext.encode("utf-8"))
    return token.decode("utf-8")


def decrypt(token: str) -> str:
    """Decrypt a Fernet token and return the original plaintext string."""
    if not token:
        return ""
    try:
        plaintext: bytes = _get_fernet().decrypt(token.encode("utf-8"))
        return plaintext.decode("utf-8")
    except InvalidToken as exc:
        raise ValueError("Failed to decrypt credential — invalid or tampered token.") from exc

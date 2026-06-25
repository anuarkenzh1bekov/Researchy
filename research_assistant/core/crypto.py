"""Small crypto helpers: API-key hashing + symmetric encryption for secrets.

Two distinct jobs, deliberately kept together as the one place that touches
crypto:
  - `hash_api_key` (one-way) — API keys are stored ONLY as a SHA-256 hash, so a
    DB leak never exposes a usable key. We compare hashes on each request.
  - `encrypt`/`decrypt` (reversible, Fernet) — bot tokens MUST be recoverable to
    start a bot, so they're encrypted at rest, not hashed. Key from settings.

NOTE (portfolio scope): if no encryption key is configured, encrypt/decrypt are
identity pass-throughs with a one-time warning — keeps local dev frictionless.
A real deployment sets API_ENCRYPTION_KEY (a Fernet key) and would also rotate
it. The seam is here; rotation is intentionally not implemented.
"""

from __future__ import annotations

import hashlib
import secrets
from functools import lru_cache

from research_assistant.core.logging import get_logger
from research_assistant.core.settings import get_settings

log = get_logger(__name__)


def generate_api_key() -> str:
    """A fresh opaque API key (the raw value shown to the user exactly once)."""
    return secrets.token_urlsafe(32)


def hash_api_key(raw_key: str) -> str:
    """Stable one-way hash for storage/lookup. SHA-256 is fine here: keys are
    high-entropy random tokens, not low-entropy passwords (no salt/KDF needed)."""
    return hashlib.sha256(raw_key.encode("utf-8")).hexdigest()


@lru_cache
def _fernet():
    """Fernet built from the configured key, or None if unset (pass-through)."""
    key = get_settings().api_encryption_key
    if not key:
        log.warning("encryption_disabled", reason="API_ENCRYPTION_KEY not set — secrets stored as plaintext")
        return None
    from cryptography.fernet import Fernet

    return Fernet(key.encode("utf-8") if isinstance(key, str) else key)


def encrypt(plaintext: str) -> str:
    f = _fernet()
    if f is None:
        return plaintext
    return f.encrypt(plaintext.encode("utf-8")).decode("utf-8")


def decrypt(token: str) -> str:
    f = _fernet()
    if f is None:
        return token
    from cryptography.fernet import InvalidToken

    try:
        return f.decrypt(token.encode("utf-8")).decode("utf-8")
    except InvalidToken:
        # Tolerate values written before a key was configured (legacy plaintext).
        log.warning("decrypt_failed", reason="value not Fernet-encrypted; returning as-is")
        return token

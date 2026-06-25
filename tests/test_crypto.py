"""Pure-logic tests for the crypto seam (no DB/Redis).

Covers the two security-critical invariants:
  - API keys hash deterministically and distinctly (lookup correctness).
  - Bot tokens survive an encrypt→decrypt round-trip; with no key configured
    both are identity (local-dev pass-through).
"""

from __future__ import annotations

import research_assistant.core.crypto as crypto


def test_hash_api_key_is_deterministic_and_distinct():
    a = crypto.hash_api_key("secret-key")
    assert a == crypto.hash_api_key("secret-key")  # stable for lookup
    assert a != crypto.hash_api_key("other-key")
    assert a != "secret-key"  # never the raw value


def test_generate_api_key_is_unique_and_nontrivial():
    k1, k2 = crypto.generate_api_key(), crypto.generate_api_key()
    assert k1 != k2
    assert len(k1) >= 32


def test_encrypt_round_trips_with_a_key():
    from cryptography.fernet import Fernet

    crypto._fernet.cache_clear()
    key = Fernet.generate_key().decode()
    # patch the cached settings accessor crypto reads through
    import research_assistant.core.settings as settings_mod

    orig = settings_mod.get_settings()
    object.__setattr__(orig, "api_encryption_key", key)
    try:
        token = crypto.encrypt("123:telegram-bot-token")
        assert token != "123:telegram-bot-token"  # actually ciphertext
        assert crypto.decrypt(token) == "123:telegram-bot-token"
    finally:
        object.__setattr__(orig, "api_encryption_key", None)
        crypto._fernet.cache_clear()


def test_encrypt_is_identity_without_a_key():
    crypto._fernet.cache_clear()
    # default settings have no key configured in the test env
    assert crypto.encrypt("plain") == "plain"
    assert crypto.decrypt("plain") == "plain"
    crypto._fernet.cache_clear()

"""Agent key generation and digesting.

Keys are at least 256 bits from a cryptographic source. Only the SHA-256 digest and a
version counter are persisted; the plaintext is returned exactly once by the create or
rotate response and never stored, logged or enqueued.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets

KEY_PREFIX = "agt_"
KEY_RANDOM_BYTES = 32


def generate_agent_key() -> str:
    """Return a fresh opaque agent key (>=256 bits of entropy)."""
    return KEY_PREFIX + secrets.token_urlsafe(KEY_RANDOM_BYTES)


def hash_agent_key(raw_key: str) -> str:
    """SHA-256 hex digest used for lookup and comparison."""
    return hashlib.sha256(raw_key.encode("utf-8")).hexdigest()


def key_digest_matches(raw_key: str, stored_hash: str) -> bool:
    return hmac.compare_digest(hash_agent_key(raw_key), stored_hash)

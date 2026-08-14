from __future__ import annotations

import base64
import hashlib
import logging

from cryptography.fernet import Fernet, InvalidToken

from .config import settings

log = logging.getLogger("aiops.crypto")


class SecretUnavailable(RuntimeError):
    """A stored secret cannot be read with the key this instance is running with."""


def _fernet() -> Fernet:
    """Derive the encryption key from the configured secret.

    Operators set a passphrase of any length, so it is hashed to the 32 bytes
    Fernet requires rather than demanding they generate base64 by hand. The
    salt is fixed: this derives a key from a secret that is already
    high-entropy, it is not password stretching.
    """
    secret = (settings.secret_key or "").strip()
    if not secret:
        raise SecretUnavailable(
            "AIOPS_SECRET_KEY is not set, so stored credentials cannot be read or written. "
            "Set it in the server's .env and restart."
        )
    digest = hashlib.sha256(b"aiops.credentials.v1|" + secret.encode()).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def encrypt(value: str | None) -> str | None:
    """Encrypt a secret for storage. None and empty stay empty."""
    if not value:
        return None
    return _fernet().encrypt(value.encode()).decode()


def decrypt(value: str | None) -> str | None:
    """Decrypt a stored secret.

    Raises rather than returning None on a bad key, because silently treating
    an unreadable credential as absent would look like "no password set" and
    send the agent off to try something else.
    """
    if not value:
        return None
    try:
        return _fernet().decrypt(value.encode()).decode()
    except InvalidToken as exc:
        raise SecretUnavailable(
            "A stored credential could not be decrypted. AIOPS_SECRET_KEY has changed "
            "since it was saved; the credential must be re-entered."
        ) from exc


def is_configured() -> bool:
    """True when this instance can store credentials at all."""
    return bool((settings.secret_key or "").strip())

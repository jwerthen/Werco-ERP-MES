"""Fernet helpers for carrier-integration secrets.

Mirrors the pattern in ``app/services/webhook_service.py`` (module-level key from
env, module-level cipher, ``.encrypt(s.encode()).decode()`` round-trip) but reads
its key from ``INTEGRATION_ENCRYPTION_KEY`` with a fallback to
``WEBHOOK_ENCRYPTION_KEY`` so existing deployments keep working.

SECURITY: plaintext carrier API keys / webhook secrets are NEVER logged,
serialized to API responses (last4 only), or placed in audit / operational-event
payloads. They are only ever decrypted in-memory at the moment a provider call is
made.

KEY MANAGEMENT (CMMC SC-28, protection of CUI/secrets at rest): the encryption key
MUST be operator-provided in production / staging. If neither
``INTEGRATION_ENCRYPTION_KEY`` nor ``WEBHOOK_ENCRYPTION_KEY`` is configured in those
environments we HARD-FAIL at import (no cipher is constructed) rather than silently
generating an ephemeral in-process key -- a generated key round-trips within one
process (so encryption *appears* to work) but is lost on restart and differs per
worker/replica, leaving stored secrets permanently undecryptable. That is fail-
SILENT, not fail-loud, and unacceptable for secrets at rest. An ephemeral generated
key is allowed ONLY in an explicitly detected dev/test environment for local
convenience.
"""

import os

from cryptography.fernet import Fernet

from app.core.config import settings

# Environments where an operator-provided key is MANDATORY (no ephemeral fallback).
_KEY_REQUIRED_ENVIRONMENTS = {"production", "staging"}


def _resolve_key() -> str:
    """Resolve the Fernet key at import time, failing loud when one is required.

    Prefer the dedicated ``INTEGRATION_ENCRYPTION_KEY``; fall back to
    ``WEBHOOK_ENCRYPTION_KEY`` so a single-secret deployment still works. In
    production / staging the absence of BOTH is a fatal misconfiguration (raise).
    Only a dev/test environment may fall back to an ephemeral generated key.
    """
    key = os.getenv("INTEGRATION_ENCRYPTION_KEY") or os.getenv("WEBHOOK_ENCRYPTION_KEY")
    if key:
        return key

    environment = (getattr(settings, "ENVIRONMENT", "") or "").strip().lower()
    if environment in _KEY_REQUIRED_ENVIRONMENTS:
        raise RuntimeError(
            "INTEGRATION_ENCRYPTION_KEY (or WEBHOOK_ENCRYPTION_KEY) must be set in "
            f"{environment}: carrier API keys / webhook secrets are encrypted at rest "
            "with it (CMMC SC-28). Refusing to start with an ephemeral key that would "
            "make stored secrets undecryptable after a restart. Generate one with: "
            'python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"'
        )

    # Dev / test only: an ephemeral key is acceptable for local convenience.
    return Fernet.generate_key().decode()


INTEGRATION_ENCRYPTION_KEY = _resolve_key()

cipher = Fernet(INTEGRATION_ENCRYPTION_KEY.encode())


def encrypt_secret(plaintext: str) -> str:
    """Encrypt a carrier secret (API key / webhook secret) for at-rest storage."""
    return cipher.encrypt(plaintext.encode()).decode()


def decrypt_secret(ciphertext: str) -> str:
    """Decrypt a stored carrier secret back to plaintext (in-memory use only)."""
    return cipher.decrypt(ciphertext.encode()).decode()

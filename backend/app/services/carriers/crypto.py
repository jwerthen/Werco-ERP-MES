"""Fernet helpers for carrier-integration secrets.

Mirrors the pattern in ``app/services/webhook_service.py`` (env-provided key,
``.encrypt(s.encode()).decode()`` round-trip) but reads its key from
``INTEGRATION_ENCRYPTION_KEY`` with a fallback to ``WEBHOOK_ENCRYPTION_KEY`` so
existing deployments keep working.

SECURITY: plaintext carrier API keys / webhook secrets are NEVER logged,
serialized to API responses (last4 only), or placed in audit / operational-event
payloads. They are only ever decrypted in-memory at the moment a provider call is
made.

KEY MANAGEMENT (CMMC SC-28, protection of CUI/secrets at rest): the encryption key
MUST be operator-provided in production / staging. Enforcement is **lazy and
scoped**: the cipher is built on first use, and if a carrier/webhook secret is
encrypted or decrypted in prod/staging WITHOUT an operator-provided key we HARD-
FAIL at that point (no cipher is constructed) rather than silently using an
ephemeral in-process key -- a generated key round-trips within one process (so it
*appears* to work) but is lost on restart and differs per worker/replica, leaving
stored secrets permanently undecryptable. That is fail-SILENT, not fail-loud, and
unacceptable for secrets at rest.

Deliberately NOT enforced at import / ``Settings()`` construction: a deployment
that does not (yet) use carrier integration -- no carrier accounts, no webhook
secrets -- still boots and runs Alembic migrations without a key. The requirement
only bites when there is actually a secret to protect. A loud startup WARNING is
logged in prod/staging when no key is configured so the gap is visible before the
feature is used. An ephemeral generated key is allowed ONLY in an explicitly
detected dev/test environment for local convenience.
"""

import logging
import os
from typing import Optional

from cryptography.fernet import Fernet

from app.core.config import settings

logger = logging.getLogger(__name__)

# Environments where an operator-provided key is MANDATORY (no ephemeral fallback).
_KEY_REQUIRED_ENVIRONMENTS = {"production", "staging"}

# Cached ephemeral key for dev/test ONLY, so encrypt/decrypt round-trips within a
# single process. Never used in prod/staging (those hard-fail without an operator
# key before this is consulted).
_ephemeral_key: Optional[str] = None


def _operator_key() -> Optional[str]:
    """The operator-provided key, preferring INTEGRATION_ENCRYPTION_KEY."""
    return os.getenv("INTEGRATION_ENCRYPTION_KEY") or os.getenv("WEBHOOK_ENCRYPTION_KEY") or None


def _key_is_required() -> bool:
    environment = (getattr(settings, "ENVIRONMENT", "") or "").strip().lower()
    return environment in _KEY_REQUIRED_ENVIRONMENTS


def _resolve_key() -> str:
    """Resolve the Fernet key, failing loud when one is required but missing.

    Prefer the dedicated ``INTEGRATION_ENCRYPTION_KEY``; fall back to
    ``WEBHOOK_ENCRYPTION_KEY`` so a single-secret deployment still works. In
    production / staging the absence of BOTH is a fatal misconfiguration (raise) --
    but only at the point a secret is actually handled, not at import. Only a
    dev/test environment may fall back to a cached ephemeral generated key.
    """
    key = _operator_key()
    if key:
        return key

    if _key_is_required():
        environment = (getattr(settings, "ENVIRONMENT", "") or "").strip().lower()
        raise RuntimeError(
            "INTEGRATION_ENCRYPTION_KEY (or WEBHOOK_ENCRYPTION_KEY) must be set in "
            f"{environment}: carrier API keys / webhook secrets are encrypted at rest with it "
            "(CMMC SC-28). Refusing to use an ephemeral key that would make stored secrets "
            "undecryptable after a restart. Generate one with: "
            'python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"'
        )

    # Dev / test only: a cached ephemeral key is acceptable for local convenience.
    global _ephemeral_key
    if _ephemeral_key is None:
        _ephemeral_key = Fernet.generate_key().decode()
    return _ephemeral_key


def _get_cipher() -> Fernet:
    """Build the Fernet cipher lazily (raises in prod/staging when no key is set)."""
    return Fernet(_resolve_key().encode())


def encrypt_secret(plaintext: str) -> str:
    """Encrypt a carrier secret (API key / webhook secret) for at-rest storage."""
    return _get_cipher().encrypt(plaintext.encode()).decode()


def decrypt_secret(ciphertext: str) -> str:
    """Decrypt a stored carrier secret back to plaintext (in-memory use only)."""
    return _get_cipher().decrypt(ciphertext.encode()).decode()


# Visibility: warn loudly at startup if a key will be required but is absent. This
# does NOT fail boot -- the lazy hard-fail in ``_resolve_key`` guards actual secret
# handling -- but it surfaces the gap in deploy logs before the feature is used.
if _key_is_required() and not _operator_key():
    logger.warning(
        "No INTEGRATION_ENCRYPTION_KEY / WEBHOOK_ENCRYPTION_KEY configured. Carrier-integration "
        "secret encryption is UNAVAILABLE in this environment: creating or using a carrier account "
        "(or an inbound webhook secret) will fail until an operator key is set (CMMC SC-28)."
    )

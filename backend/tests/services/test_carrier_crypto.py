"""Tests for carrier-secret encryption key resolution (finding 2).

The key-management contract (CMMC SC-28): in production / staging the Fernet key
MUST be operator-provided (INTEGRATION_ENCRYPTION_KEY or WEBHOOK_ENCRYPTION_KEY).
The absence of both must HARD-FAIL rather than silently generate an ephemeral key
that round-trips within one process but is lost on restart -- which would make
stored carrier secrets permanently undecryptable. A dev/test environment may fall
back to a generated key.

Enforcement is LAZY and SCOPED: it fires when a secret is actually encrypted or
decrypted (``encrypt_secret`` / ``decrypt_secret``), NOT at import or ``Settings()``
construction -- so a deployment that doesn't use carrier integration still boots
and runs migrations without a key (see tests/test_config.py).

These exercise the resolver directly (``_resolve_key``) and the public
encrypt/decrypt helpers, so they do not have to reload the module.
"""

import pytest

from app.services.carriers import crypto

pytestmark = pytest.mark.unit


def test_resolve_key_prefers_integration_env(monkeypatch):
    monkeypatch.setenv("INTEGRATION_ENCRYPTION_KEY", "integration-key-value")
    monkeypatch.setenv("WEBHOOK_ENCRYPTION_KEY", "webhook-key-value")
    assert crypto._resolve_key() == "integration-key-value"


def test_resolve_key_falls_back_to_webhook_env(monkeypatch):
    monkeypatch.delenv("INTEGRATION_ENCRYPTION_KEY", raising=False)
    monkeypatch.setenv("WEBHOOK_ENCRYPTION_KEY", "webhook-key-value")
    assert crypto._resolve_key() == "webhook-key-value"


@pytest.mark.parametrize("environment", ["production", "staging", "PRODUCTION", " Staging "])
def test_resolve_key_hard_fails_in_prod_like_env_without_a_key(monkeypatch, environment):
    monkeypatch.delenv("INTEGRATION_ENCRYPTION_KEY", raising=False)
    monkeypatch.delenv("WEBHOOK_ENCRYPTION_KEY", raising=False)
    monkeypatch.setattr(crypto.settings, "ENVIRONMENT", environment, raising=False)
    with pytest.raises(RuntimeError) as exc:
        crypto._resolve_key()
    # The error names the missing setting and never leaks a key.
    assert "INTEGRATION_ENCRYPTION_KEY" in str(exc.value)


def test_resolve_key_generates_ephemeral_in_dev(monkeypatch):
    monkeypatch.delenv("INTEGRATION_ENCRYPTION_KEY", raising=False)
    monkeypatch.delenv("WEBHOOK_ENCRYPTION_KEY", raising=False)
    monkeypatch.setattr(crypto.settings, "ENVIRONMENT", "development", raising=False)
    key = crypto._resolve_key()
    # A usable Fernet key (round-trips with a fresh cipher).
    from cryptography.fernet import Fernet

    cipher = Fernet(key.encode())
    assert cipher.decrypt(cipher.encrypt(b"x")) == b"x"


def test_encrypt_secret_hard_fails_in_prod_without_key(monkeypatch):
    """The hard-fail is LAZY: it fires when a secret is encrypted, not at import."""
    monkeypatch.delenv("INTEGRATION_ENCRYPTION_KEY", raising=False)
    monkeypatch.delenv("WEBHOOK_ENCRYPTION_KEY", raising=False)
    monkeypatch.setattr(crypto.settings, "ENVIRONMENT", "production", raising=False)
    with pytest.raises(RuntimeError) as exc:
        crypto.encrypt_secret("super-secret-api-key")
    assert "INTEGRATION_ENCRYPTION_KEY" in str(exc.value)
    # The plaintext is never leaked in the error.
    assert "super-secret-api-key" not in str(exc.value)


def test_decrypt_secret_hard_fails_in_prod_without_key(monkeypatch):
    monkeypatch.delenv("INTEGRATION_ENCRYPTION_KEY", raising=False)
    monkeypatch.delenv("WEBHOOK_ENCRYPTION_KEY", raising=False)
    monkeypatch.setattr(crypto.settings, "ENVIRONMENT", "production", raising=False)
    with pytest.raises(RuntimeError):
        crypto.decrypt_secret("gAAAAA-not-a-real-token")


def test_encrypt_decrypt_roundtrip_with_operator_key_in_prod(monkeypatch):
    """With an operator key set, prod encrypt/decrypt works (no ephemeral involved)."""
    from cryptography.fernet import Fernet

    monkeypatch.setenv("INTEGRATION_ENCRYPTION_KEY", Fernet.generate_key().decode())
    monkeypatch.delenv("WEBHOOK_ENCRYPTION_KEY", raising=False)
    monkeypatch.setattr(crypto.settings, "ENVIRONMENT", "production", raising=False)
    token = crypto.encrypt_secret("carrier-api-key-123")
    assert token != "carrier-api-key-123"  # actually ciphertext, not plaintext
    assert crypto.decrypt_secret(token) == "carrier-api-key-123"


def test_encrypt_decrypt_roundtrip_in_dev_without_key(monkeypatch):
    """Dev/test round-trips via a cached ephemeral key (same key for both calls)."""
    monkeypatch.delenv("INTEGRATION_ENCRYPTION_KEY", raising=False)
    monkeypatch.delenv("WEBHOOK_ENCRYPTION_KEY", raising=False)
    monkeypatch.setattr(crypto.settings, "ENVIRONMENT", "development", raising=False)
    monkeypatch.setattr(crypto, "_ephemeral_key", None, raising=False)  # clean slate
    token = crypto.encrypt_secret("dev-secret")
    assert crypto.decrypt_secret(token) == "dev-secret"

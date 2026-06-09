"""Tests for carrier-secret encryption key resolution (finding 2).

The key-management contract (CMMC SC-28): in production / staging the Fernet key
MUST be operator-provided (INTEGRATION_ENCRYPTION_KEY or WEBHOOK_ENCRYPTION_KEY).
The absence of both must HARD-FAIL rather than silently generate an ephemeral key
that round-trips within one process but is lost on restart -- which would make
stored carrier secrets permanently undecryptable. A dev/test environment may fall
back to a generated key.

These exercise the resolver directly (``_resolve_key``) so they do not have to
reload the module or mutate the process-wide ``settings`` object.
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

"""Unit tests for the CompanyPrintProfile encrypted-key helpers.

NO DB: exercise the model methods directly. The dev/test crypto fallback generates
an ephemeral Fernet key, so encrypt/decrypt round-trips within the process. These
pin down that the ProxyBox API key is stored encrypted (NOT plaintext), the last-4
mask is captured for display, and decrypt returns the original.
"""

import pytest

from app.models.print_profile import CompanyPrintProfile

pytestmark = pytest.mark.unit


def test_set_api_key_encrypts_and_masks(monkeypatch):
    monkeypatch.setattr("app.core.config.settings.ENVIRONMENT", "development", raising=False)
    profile = CompanyPrintProfile()
    secret = "PBX_LIVE_abcdef1234"
    profile.set_api_key(secret)

    # Stored ciphertext is NOT the plaintext, and last4 is the display mask.
    assert profile.encrypted_api_key
    assert profile.encrypted_api_key != secret
    assert profile.api_key_last4 == "1234"
    # Round-trips back to the original.
    assert profile.get_api_key() == secret


def test_clear_api_key_removes_secret_and_mask(monkeypatch):
    monkeypatch.setattr("app.core.config.settings.ENVIRONMENT", "development", raising=False)
    profile = CompanyPrintProfile()
    profile.set_api_key("PBX_xyz0000")
    profile.clear_api_key()
    assert profile.encrypted_api_key is None
    assert profile.api_key_last4 is None
    with pytest.raises(ValueError):
        profile.get_api_key()


def test_set_empty_api_key_clears(monkeypatch):
    monkeypatch.setattr("app.core.config.settings.ENVIRONMENT", "development", raising=False)
    profile = CompanyPrintProfile()
    profile.set_api_key("PBX_keep1111")
    profile.set_api_key("   ")  # whitespace-only -> treated as clear
    assert profile.encrypted_api_key is None
    assert profile.api_key_last4 is None

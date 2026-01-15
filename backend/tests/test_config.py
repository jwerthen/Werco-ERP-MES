"""
Tests for configuration validation.
Ensures that security-critical settings fail fast if misconfigured.
"""
import pytest
import os
from unittest import mock


class TestSecretKeyValidation:
    """Test SECRET_KEY validation in Settings."""

    def test_insecure_default_secret_key_rejected(self):
        """Test that the default insecure SECRET_KEY is rejected."""
        from pydantic import ValidationError
        
        with mock.patch.dict(os.environ, {
            "SECRET_KEY": "CHANGE-THIS-IN-PRODUCTION",
            "REFRESH_TOKEN_SECRET_KEY": "a" * 64,  # Valid refresh key
        }, clear=False):
            # Need to re-import to trigger validation
            with pytest.raises(ValidationError) as exc_info:
                from app.core.config import Settings
                Settings()
            
            assert "SECRET_KEY is set to an insecure default value" in str(exc_info.value)

    def test_short_secret_key_rejected(self):
        """Test that a SECRET_KEY less than 32 characters is rejected."""
        from pydantic import ValidationError
        
        with mock.patch.dict(os.environ, {
            "SECRET_KEY": "tooshort",  # Less than 32 characters
            "REFRESH_TOKEN_SECRET_KEY": "a" * 64,  # Valid refresh key
        }, clear=False):
            with pytest.raises(ValidationError) as exc_info:
                from app.core.config import Settings
                Settings()
            
            assert "must be at least 32 characters" in str(exc_info.value)

    def test_valid_secret_key_accepted(self):
        """Test that a valid SECRET_KEY is accepted."""
        valid_key = "a" * 64  # 64 character key
        
        with mock.patch.dict(os.environ, {
            "SECRET_KEY": valid_key,
            "REFRESH_TOKEN_SECRET_KEY": "b" * 64,
        }, clear=False):
            from app.core.config import Settings
            settings = Settings()
            assert settings.SECRET_KEY == valid_key


class TestRefreshTokenSecretKeyValidation:
    """Test REFRESH_TOKEN_SECRET_KEY validation in Settings."""

    def test_insecure_default_refresh_key_rejected(self):
        """Test that the default insecure REFRESH_TOKEN_SECRET_KEY is rejected."""
        from pydantic import ValidationError
        
        with mock.patch.dict(os.environ, {
            "SECRET_KEY": "a" * 64,  # Valid secret key
            "REFRESH_TOKEN_SECRET_KEY": "CHANGE-THIS-REFRESH-SECRET",
        }, clear=False):
            with pytest.raises(ValidationError) as exc_info:
                from app.core.config import Settings
                Settings()
            
            assert "REFRESH_TOKEN_SECRET_KEY is set to an insecure default value" in str(exc_info.value)

    def test_short_refresh_key_rejected(self):
        """Test that a REFRESH_TOKEN_SECRET_KEY less than 32 characters is rejected."""
        from pydantic import ValidationError
        
        with mock.patch.dict(os.environ, {
            "SECRET_KEY": "a" * 64,  # Valid secret key
            "REFRESH_TOKEN_SECRET_KEY": "short",  # Less than 32 characters
        }, clear=False):
            with pytest.raises(ValidationError) as exc_info:
                from app.core.config import Settings
                Settings()
            
            assert "must be at least 32 characters" in str(exc_info.value)

    def test_valid_refresh_key_accepted(self):
        """Test that a valid REFRESH_TOKEN_SECRET_KEY is accepted."""
        valid_key = "b" * 64  # 64 character key
        
        with mock.patch.dict(os.environ, {
            "SECRET_KEY": "a" * 64,
            "REFRESH_TOKEN_SECRET_KEY": valid_key,
        }, clear=False):
            from app.core.config import Settings
            settings = Settings()
            assert settings.REFRESH_TOKEN_SECRET_KEY == valid_key


class TestInsecureKeyPatterns:
    """Test various insecure key patterns are rejected."""

    @pytest.mark.parametrize("insecure_key", [
        "",
        "secret",
        "password",
        "changeme",
        "change-this-to-a-random-string-at-least-32-characters",
        "change-this-different-key-for-refresh-tokens",
    ])
    def test_common_insecure_keys_rejected(self, insecure_key):
        """Test that common insecure key patterns are rejected."""
        from pydantic import ValidationError
        
        with mock.patch.dict(os.environ, {
            "SECRET_KEY": insecure_key,
            "REFRESH_TOKEN_SECRET_KEY": "b" * 64,
        }, clear=False):
            with pytest.raises(ValidationError):
                from app.core.config import Settings
                Settings()

"""
Tests for configuration validation.
Ensures that security-critical settings fail fast if misconfigured.
"""

import os
from unittest import mock

import pytest


class TestSecretKeyValidation:
    """Test SECRET_KEY validation in Settings."""

    def test_insecure_default_secret_key_rejected(self):
        """Test that the default insecure SECRET_KEY is rejected."""
        from pydantic import ValidationError

        with mock.patch.dict(
            os.environ,
            {
                "SECRET_KEY": "CHANGE-THIS-IN-PRODUCTION",
                "REFRESH_TOKEN_SECRET_KEY": "a" * 64,  # Valid refresh key
            },
            clear=True,
        ):
            # Need to re-import to trigger validation
            with pytest.raises(ValidationError) as exc_info:
                from app.core.config import Settings

                Settings()

            assert "SECRET_KEY is set to an insecure value" in str(exc_info.value)

    def test_short_secret_key_rejected(self):
        """Test that a SECRET_KEY less than 32 characters is rejected."""
        from pydantic import ValidationError

        with mock.patch.dict(
            os.environ,
            {
                "SECRET_KEY": "tooshort",  # Less than 32 characters
                "REFRESH_TOKEN_SECRET_KEY": "a" * 64,  # Valid refresh key
            },
            clear=True,
        ):
            with pytest.raises(ValidationError) as exc_info:
                from app.core.config import Settings

                Settings()

            assert "must be at least 32 characters" in str(exc_info.value)

    def test_valid_secret_key_accepted(self):
        """Test that a valid SECRET_KEY is accepted."""
        valid_key = "a" * 64  # 64 character key

        with mock.patch.dict(
            os.environ,
            {
                "SECRET_KEY": valid_key,
                "REFRESH_TOKEN_SECRET_KEY": "b" * 64,
            },
            clear=True,
        ):
            from app.core.config import Settings

            settings = Settings()
            assert settings.SECRET_KEY == valid_key


class TestRefreshTokenSecretKeyValidation:
    """Test REFRESH_TOKEN_SECRET_KEY validation in Settings."""

    def test_insecure_default_refresh_key_rejected(self):
        """Test that the default insecure REFRESH_TOKEN_SECRET_KEY is rejected."""
        from pydantic import ValidationError

        with mock.patch.dict(
            os.environ,
            {
                "SECRET_KEY": "a" * 64,  # Valid secret key
                "REFRESH_TOKEN_SECRET_KEY": "CHANGE-THIS-REFRESH-SECRET",
            },
            clear=True,
        ):
            with pytest.raises(ValidationError) as exc_info:
                from app.core.config import Settings

                Settings()

            assert "REFRESH_TOKEN_SECRET_KEY is set to an insecure value" in str(
                exc_info.value
            )

    def test_short_refresh_key_rejected(self):
        """Test that a REFRESH_TOKEN_SECRET_KEY less than 32 characters is rejected."""
        from pydantic import ValidationError

        with mock.patch.dict(
            os.environ,
            {
                "SECRET_KEY": "a" * 64,  # Valid secret key
                "REFRESH_TOKEN_SECRET_KEY": "short",  # Less than 32 characters
            },
            clear=True,
        ):
            with pytest.raises(ValidationError) as exc_info:
                from app.core.config import Settings

                Settings()

            assert "must be at least 32 characters" in str(exc_info.value)

    def test_valid_refresh_key_accepted(self):
        """Test that a valid REFRESH_TOKEN_SECRET_KEY is accepted."""
        valid_key = "b" * 64  # 64 character key

        with mock.patch.dict(
            os.environ,
            {
                "SECRET_KEY": "a" * 64,
                "REFRESH_TOKEN_SECRET_KEY": valid_key,
            },
            clear=True,
        ):
            from app.core.config import Settings

            settings = Settings()
            assert settings.REFRESH_TOKEN_SECRET_KEY == valid_key


class TestInsecureKeyPatterns:
    """Test various insecure key patterns are rejected."""

    @pytest.mark.parametrize(
        "insecure_key",
        [
            "",
            "secret",
            "password",
            "changeme",
            "change-this-to-a-random-string-at-least-32-characters",
            "change-this-different-key-for-refresh-tokens",
        ],
    )
    def test_common_insecure_keys_rejected(self, insecure_key):
        """Test that common insecure key patterns are rejected."""
        from pydantic import ValidationError

        with mock.patch.dict(
            os.environ,
            {
                "SECRET_KEY": insecure_key,
                "REFRESH_TOKEN_SECRET_KEY": "b" * 64,
            },
            clear=True,
        ):
            with pytest.raises(ValidationError):
                from app.core.config import Settings

                Settings()


class TestSupabaseDatabaseConfiguration:
    """Test Supabase database URL normalization and production enforcement."""

    def test_supabase_database_url_is_normalized_for_sqlalchemy(self):
        with mock.patch.dict(
            os.environ,
            {
                "DATABASE_URL": "postgresql://postgres:secret@db.example.supabase.co:5432/postgres",
                "SECRET_KEY": "a" * 64,
                "REFRESH_TOKEN_SECRET_KEY": "b" * 64,
            },
            clear=True,
        ):
            from app.core.config import Settings

            settings = Settings()

            assert settings.SQLALCHEMY_DATABASE_URL.startswith("postgresql+psycopg2://")
            assert "sslmode=require" in settings.SQLALCHEMY_DATABASE_URL
            assert (
                "application_name=werco_erp_supabase"
                in settings.SQLALCHEMY_DATABASE_URL
            )
            assert settings.database_provider == "supabase"

    def test_supabase_database_url_can_be_built_from_project_ref(self):
        with mock.patch.dict(
            os.environ,
            {
                "DATABASE_URL": "",
                "SUPABASE_PROJECT_REF": "abc123",
                "SUPABASE_DB_PASSWORD": "db-password",
                "SECRET_KEY": "a" * 64,
                "REFRESH_TOKEN_SECRET_KEY": "b" * 64,
            },
            clear=True,
        ):
            from app.core.config import Settings

            settings = Settings()

            assert (
                "db.abc123.supabase.co:5432/postgres"
                in settings.SQLALCHEMY_DATABASE_URL
            )
            assert settings.safe_database_host == "db.abc123.supabase.co"

    def test_supabase_pooler_url_uses_project_qualified_user(self):
        with mock.patch.dict(
            os.environ,
            {
                "DATABASE_URL": "",
                "SUPABASE_PROJECT_REF": "abc123",
                "SUPABASE_DB_HOST": "aws-1-us-west-2.pooler.supabase.com",
                "SUPABASE_DB_PASSWORD": "db-password",
                "SECRET_KEY": "a" * 64,
                "REFRESH_TOKEN_SECRET_KEY": "b" * 64,
            },
            clear=True,
        ):
            from app.core.config import Settings

            settings = Settings()

            assert "postgres.abc123" in settings.SQLALCHEMY_DATABASE_URL
            assert settings.safe_database_host == "aws-1-us-west-2.pooler.supabase.com"

    def test_supabase_settings_override_injected_non_supabase_database_url(self):
        with mock.patch.dict(
            os.environ,
            {
                "DATABASE_URL": "postgresql://postgres:secret@localhost:5432/werco_erp",
                "SUPABASE_PROJECT_REF": "abc123",
                "SUPABASE_DB_HOST": "aws-1-us-west-2.pooler.supabase.com",
                "SUPABASE_DB_PASSWORD": "db-password",
                "SECRET_KEY": "a" * 64,
                "REFRESH_TOKEN_SECRET_KEY": "b" * 64,
                "ENVIRONMENT": "production",
                "DEBUG": "false",
                "CORS_ORIGINS": "https://erp.example.com",
            },
            clear=True,
        ):
            from app.core.config import Settings

            settings = Settings()

            assert settings.safe_database_host == "aws-1-us-west-2.pooler.supabase.com"
            assert settings.database_provider == "supabase"

    def test_sqlite_database_url_is_not_overridden_by_supabase_settings(self):
        with mock.patch.dict(
            os.environ,
            {
                "DATABASE_URL": "sqlite:///./test.db",
                "SUPABASE_PROJECT_REF": "abc123",
                "SUPABASE_DB_PASSWORD": "db-password",
                "SECRET_KEY": "a" * 64,
                "REFRESH_TOKEN_SECRET_KEY": "b" * 64,
                "ENVIRONMENT": "test",
            },
            clear=True,
        ):
            from app.core.config import Settings

            settings = Settings()

            assert settings.SQLALCHEMY_DATABASE_URL == "sqlite:///./test.db"
            assert settings.database_provider == "sqlite"

    def test_production_rejects_non_supabase_database_by_default(self):
        from pydantic import ValidationError

        with mock.patch.dict(
            os.environ,
            {
                "DATABASE_URL": "postgresql://postgres:secret@localhost:5432/werco_erp",
                "SUPABASE_URL": "",
                "SUPABASE_PROJECT_REF": "",
                "SUPABASE_DB_HOST": "",
                "SUPABASE_DB_PASSWORD": "",
                "DB_PASSWORD": "",
                "SECRET_KEY": "a" * 64,
                "REFRESH_TOKEN_SECRET_KEY": "b" * 64,
                "ENVIRONMENT": "production",
                "DEBUG": "false",
                "CORS_ORIGINS": "https://erp.example.com",
            },
            clear=True,
        ):
            with pytest.raises(ValidationError) as exc_info:
                from app.core.config import Settings

                Settings()

            assert "Production must use Supabase" in str(exc_info.value)

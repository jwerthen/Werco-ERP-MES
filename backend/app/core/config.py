from pydantic_settings import BaseSettings
from pydantic import field_validator
from typing import List, Optional
import os


# List of known insecure default secret key values that should be rejected
INSECURE_SECRET_KEYS = {
    "CHANGE-THIS-IN-PRODUCTION",
    "CHANGE-THIS-REFRESH-SECRET",
    "change-this-to-a-random-string-at-least-32-characters",
    "change-this-different-key-for-refresh-tokens",
    "secret",
    "password",
    "changeme",
    "",
}


class Settings(BaseSettings):
    # Application
    APP_NAME: str = "Werco ERP"
    DEBUG: bool = False
    ENVIRONMENT: str = "development"
    API_V1_PREFIX: str = "/api/v1"
    
    # Railway provides PORT environment variable
    PORT: int = 8000
    
    # Database - Railway provides DATABASE_URL automatically for PostgreSQL plugin
    DATABASE_URL: str = "postgresql://user:pass@localhost:5432/werco_erp"
    DB_PASSWORD: Optional[str] = None
    
    # Database Connection Pool Settings
    DB_POOL_SIZE: int = 5  # Number of connections to keep open
    DB_MAX_OVERFLOW: int = 10  # Max additional connections when pool is exhausted
    DB_POOL_TIMEOUT: int = 30  # Seconds to wait for connection from pool
    DB_POOL_RECYCLE: int = 1800  # Recycle connections after 30 minutes
    DB_POOL_PRE_PING: bool = True  # Test connections before use (handles stale connections)
    
    # Security - MUST be overridden via environment variables
    SECRET_KEY: str = "CHANGE-THIS-IN-PRODUCTION"
    REFRESH_TOKEN_SECRET_KEY: str = "CHANGE-THIS-REFRESH-SECRET"
    ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 15  # Short-lived access tokens
    REFRESH_TOKEN_EXPIRE_DAYS: int = 7  # Refresh tokens valid for 7 days
    SESSION_ABSOLUTE_TIMEOUT_HOURS: int = 24  # Force re-login after 24 hours regardless
    
    @field_validator("SECRET_KEY")
    @classmethod
    def validate_secret_key(cls, v: str) -> str:
        """Validate SECRET_KEY is secure - reject known insecure defaults."""
        if v in INSECURE_SECRET_KEYS:
            raise ValueError(
                "SECRET_KEY is set to an insecure default value. "
                "Please set a secure SECRET_KEY environment variable. "
                "Generate one with: python -c \"import secrets; print(secrets.token_urlsafe(64))\""
            )
        if len(v) < 32:
            raise ValueError(
                f"SECRET_KEY must be at least 32 characters long (got {len(v)}). "
                "Generate one with: python -c \"import secrets; print(secrets.token_urlsafe(64))\""
            )
        return v
    
    @field_validator("REFRESH_TOKEN_SECRET_KEY")
    @classmethod
    def validate_refresh_token_secret_key(cls, v: str) -> str:
        """Validate REFRESH_TOKEN_SECRET_KEY is secure - reject known insecure defaults."""
        if v in INSECURE_SECRET_KEYS:
            raise ValueError(
                "REFRESH_TOKEN_SECRET_KEY is set to an insecure default value. "
                "Please set a secure REFRESH_TOKEN_SECRET_KEY environment variable. "
                "Generate one with: python -c \"import secrets; print(secrets.token_urlsafe(64))\""
            )
        if len(v) < 32:
            raise ValueError(
                f"REFRESH_TOKEN_SECRET_KEY must be at least 32 characters long (got {len(v)}). "
                "Generate one with: python -c \"import secrets; print(secrets.token_urlsafe(64))\""
            )
        return v
    
    # Rate Limiting
    RATE_LIMIT_ENABLED: bool = True
    RATE_LIMIT_TIMES: int = 100
    RATE_LIMIT_SECONDS: int = 60
    RATE_LIMIT_EXEMPT_PATHS: str = "/health,/api/docs,/api/openapi.json,/api/redoc"
    
    @property
    def rate_limit_exempt_paths_list(self) -> List[str]:
        return [path.strip() for path in self.RATE_LIMIT_EXEMPT_PATHS.split(",")]
    
    # CORS - Include localhost for dev, Railway URLs added via env var in production
    CORS_ORIGINS: str = "http://localhost:3000,http://localhost:3001,http://localhost:8000"
    CORS_ALLOW_CREDENTIALS: bool = True
    CORS_ALLOW_METHODS: str = "GET,POST,PUT,PATCH,DELETE,OPTIONS"
    CORS_ALLOW_HEADERS: str = "Authorization,Content-Type,X-Requested-With,Accept,Origin,If-None-Match,If-Match"
    
    @property
    def cors_origins_list(self) -> List[str]:
        return [origin.strip() for origin in self.CORS_ORIGINS.split(",")]
    
    # File Storage
    AWS_ACCESS_KEY_ID: str = ""
    AWS_SECRET_ACCESS_KEY: str = ""
    AWS_REGION: str = "us-east-1"
    S3_BUCKET_NAME: str = "werco-erp-documents"
    
    # Monitoring
    SENTRY_DSN: Optional[str] = None
    LOG_LEVEL: str = "INFO"
    
    # Redis / Job Queue
    REDIS_URL: Optional[str] = None
    REDIS_HOST: str = "localhost"
    REDIS_PORT: int = 6379
    REDIS_DB: int = 0
    
    # LLM Integration
    ANTHROPIC_API_KEY: Optional[str] = None
    NOTION_TOKEN: Optional[str] = None

    # Email Configuration
    SMTP_HOST: str = "smtp.gmail.com"
    SMTP_PORT: int = 587
    SMTP_USER: str = ""
    SMTP_PASSWORD: str = ""
    SMTP_FROM: str = "noreply@werco.com"
    SMTP_FROM_NAME: str = "Werco ERP System"

    # Webhook Configuration
    WEBHOOK_ENCRYPTION_KEY: str = ""

    class Config:
        env_file = ".env"
        case_sensitive = True


settings = Settings()

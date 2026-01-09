from pydantic_settings import BaseSettings
from typing import List, Optional
import os


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
    
    # Security - MUST be overridden in production
    SECRET_KEY: str = "CHANGE-THIS-IN-PRODUCTION"
    REFRESH_TOKEN_SECRET_KEY: str = "CHANGE-THIS-REFRESH-SECRET"
    ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 15  # Short-lived access tokens
    REFRESH_TOKEN_EXPIRE_DAYS: int = 7  # Refresh tokens valid for 7 days
    SESSION_ABSOLUTE_TIMEOUT_HOURS: int = 24  # Force re-login after 24 hours regardless
    
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

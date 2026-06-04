from typing import List, Optional
from urllib.parse import parse_qsl, quote, urlencode, urlparse, urlunparse

from pydantic import field_validator, model_validator
from pydantic_settings import BaseSettings

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

    # Container platforms provide PORT environment variable
    PORT: int = 8000

    # Database - Supabase Postgres is the canonical backend data store.
    # Use DATABASE_URL when it is already a Supabase connection string. If a
    # platform injects its own non-Supabase DATABASE_URL, explicit Supabase URL
    # aliases or SUPABASE_DB_* settings take precedence.
    DATABASE_URL: Optional[str] = None
    DB_PASSWORD: Optional[str] = None
    DATABASE_PROVIDER: str = "supabase"
    ALLOW_NON_SUPABASE_DATABASE: bool = False

    # Supabase Postgres connection settings
    SUPABASE_URL: Optional[str] = None
    SUPABASE_DATABASE_URL: Optional[str] = None
    SUPABASE_POSTGRES_URL: Optional[str] = None
    SUPABASE_PROJECT_REF: Optional[str] = None
    SUPABASE_DB_HOST: Optional[str] = None
    SUPABASE_DB_PORT: int = 5432
    SUPABASE_DB_NAME: str = "postgres"
    SUPABASE_DB_USER: Optional[str] = None
    SUPABASE_DB_PASSWORD: Optional[str] = None
    SUPABASE_DB_SSLMODE: str = "require"
    POSTGRES_URL: Optional[str] = None
    POSTGRES_PRISMA_URL: Optional[str] = None
    POSTGRES_URL_NON_POOLING: Optional[str] = None
    POSTGRES_URL_NO_SSL: Optional[str] = None
    DIRECT_URL: Optional[str] = None
    POSTGRES_HOST: Optional[str] = None
    POSTGRES_PORT: int = 5432
    POSTGRES_USER: Optional[str] = None
    POSTGRES_PASSWORD: Optional[str] = None
    POSTGRES_DATABASE: Optional[str] = None
    POSTGRES_DB: Optional[str] = None

    # Database Connection Pool Settings
    DB_POOL_SIZE: int = 5  # Number of connections to keep open
    DB_MAX_OVERFLOW: int = 10  # Max additional connections when pool is exhausted
    DB_POOL_TIMEOUT: int = 30  # Seconds to wait for connection from pool
    DB_POOL_RECYCLE: int = 1800  # Recycle connections after 30 minutes
    DB_POOL_PRE_PING: bool = (
        True  # Test connections before use (handles stale connections)
    )

    # Security - MUST be overridden via environment variables (no defaults - app fails fast if missing)
    SECRET_KEY: str
    REFRESH_TOKEN_SECRET_KEY: str
    ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 15  # Short-lived access tokens
    REFRESH_TOKEN_EXPIRE_DAYS: int = 7  # Refresh tokens valid for 7 days
    SESSION_ABSOLUTE_TIMEOUT_HOURS: int = 24  # Force re-login after 24 hours regardless

    @field_validator("SECRET_KEY")
    @classmethod
    def validate_secret_key(cls, v: str) -> str:
        """Validate SECRET_KEY is secure - reject known insecure values."""
        if v in INSECURE_SECRET_KEYS:
            raise ValueError(
                "SECRET_KEY is set to an insecure value. "
                'Generate a secure key with: python -c "import secrets; print(secrets.token_urlsafe(64))"'
            )
        if len(v) < 32:
            raise ValueError(
                f"SECRET_KEY must be at least 32 characters long (got {len(v)}). "
                'Generate one with: python -c "import secrets; print(secrets.token_urlsafe(64))"'
            )
        return v

    @field_validator("REFRESH_TOKEN_SECRET_KEY")
    @classmethod
    def validate_refresh_token_secret_key(cls, v: str) -> str:
        """Validate REFRESH_TOKEN_SECRET_KEY is secure - reject known insecure values."""
        if v in INSECURE_SECRET_KEYS:
            raise ValueError(
                "REFRESH_TOKEN_SECRET_KEY is set to an insecure value. "
                'Generate a secure key with: python -c "import secrets; print(secrets.token_urlsafe(64))"'
            )
        if len(v) < 32:
            raise ValueError(
                f"REFRESH_TOKEN_SECRET_KEY must be at least 32 characters long (got {len(v)}). "
                'Generate one with: python -c "import secrets; print(secrets.token_urlsafe(64))"'
            )
        return v

    @model_validator(mode="after")
    def validate_production_settings(self) -> "Settings":
        """Harden production settings."""
        if self.ENVIRONMENT == "production":
            if self.DEBUG:
                raise ValueError("DEBUG must be false in production.")
            if not self.CORS_ORIGINS:
                raise ValueError("CORS_ORIGINS must be set in production.")
            if "localhost" in self.CORS_ORIGINS:
                raise ValueError(
                    "CORS_ORIGINS must not include localhost in production."
                )
            if not self.ALLOW_NON_SUPABASE_DATABASE and not self.is_supabase_database:
                raise ValueError(
                    "Production must use Supabase as the backend database. "
                    "Set DATABASE_URL, SUPABASE_DATABASE_URL, or POSTGRES_URL to the Supabase "
                    "Postgres connection string, or configure SUPABASE_PROJECT_REF/SUPABASE_DB_PASSWORD. "
                    "Set ALLOW_NON_SUPABASE_DATABASE=true only for an intentional break-glass migration."
                )
        return self

    @property
    def SQLALCHEMY_DATABASE_URL(self) -> str:
        """Return the normalized SQLAlchemy URL used by the API, worker, and Alembic."""
        url = self._select_database_url()
        return self._normalize_database_url(url)

    @property
    def database_provider(self) -> str:
        if self.is_sqlite_database:
            return "sqlite"
        if self.is_supabase_database:
            return "supabase"
        if self.SQLALCHEMY_DATABASE_URL.startswith("postgresql"):
            return "postgresql"
        return self.DATABASE_PROVIDER

    @property
    def is_sqlite_database(self) -> bool:
        return self.SQLALCHEMY_DATABASE_URL.startswith("sqlite")

    @property
    def is_supabase_database(self) -> bool:
        parsed = urlparse(self.SQLALCHEMY_DATABASE_URL)
        host = parsed.hostname or ""
        return (
            host.endswith(".supabase.co")
            or host.endswith(".pooler.supabase.com")
            or "supabase.com" in host
        )

    @property
    def safe_database_host(self) -> str:
        parsed = urlparse(self.SQLALCHEMY_DATABASE_URL)
        return parsed.hostname or "local"

    def _build_supabase_database_url(self) -> str:
        project_ref = self.SUPABASE_PROJECT_REF or self._project_ref_from_supabase_url()
        host = self.SUPABASE_DB_HOST or (
            f"db.{project_ref}.supabase.co" if project_ref else None
        )
        password = self.SUPABASE_DB_PASSWORD or self.DB_PASSWORD
        if not host or not password:
            raise ValueError(
                "DATABASE_URL is not set. Configure the Supabase database with either DATABASE_URL "
                "or SUPABASE_PROJECT_REF/SUPABASE_DB_PASSWORD."
            )
        user = self.SUPABASE_DB_USER or self._default_supabase_db_user(
            host, project_ref
        )
        return (
            f"postgresql://{quote(user, safe='')}:{quote(password, safe='')}@{host}:{self.SUPABASE_DB_PORT}/"
            f"{quote(self.SUPABASE_DB_NAME, safe='')}"
        )

    def _select_database_url(self) -> str:
        if self.DATABASE_URL and self._url_is_sqlite(self.DATABASE_URL):
            return self.DATABASE_URL
        for url in self._candidate_database_urls():
            if self._url_is_supabase(url):
                return url
        if self._can_build_supabase_postgres_url():
            return self._build_supabase_postgres_url()
        if self._can_build_supabase_database_url():
            return self._build_supabase_database_url()
        if self.DATABASE_URL:
            return self.DATABASE_URL
        return self._build_supabase_database_url()

    def _candidate_database_urls(self) -> list[str]:
        return [
            url
            for url in (
                self.DATABASE_URL,
                self.SUPABASE_DATABASE_URL,
                self.SUPABASE_POSTGRES_URL,
                self.POSTGRES_URL,
                self.POSTGRES_PRISMA_URL,
                self.POSTGRES_URL_NON_POOLING,
                self.POSTGRES_URL_NO_SSL,
                self.DIRECT_URL,
            )
            if url
        ]

    def _can_build_supabase_database_url(self) -> bool:
        project_ref = self.SUPABASE_PROJECT_REF or self._project_ref_from_supabase_url()
        host = self.SUPABASE_DB_HOST or (
            f"db.{project_ref}.supabase.co" if project_ref else None
        )
        password = self.SUPABASE_DB_PASSWORD or self.DB_PASSWORD
        return bool(host and password)

    def _can_build_supabase_postgres_url(self) -> bool:
        return bool(
            self.POSTGRES_HOST
            and self._host_is_supabase(self.POSTGRES_HOST)
            and self.POSTGRES_USER
            and self.POSTGRES_PASSWORD
        )

    def _build_supabase_postgres_url(self) -> str:
        database = self.POSTGRES_DATABASE or self.POSTGRES_DB or "postgres"
        return (
            f"postgresql://{quote(self.POSTGRES_USER or '', safe='')}:"
            f"{quote(self.POSTGRES_PASSWORD or '', safe='')}@"
            f"{self.POSTGRES_HOST}:{self.POSTGRES_PORT}/{quote(database, safe='')}"
        )

    def _default_supabase_db_user(self, host: str, project_ref: Optional[str]) -> str:
        if host.endswith(".pooler.supabase.com") and project_ref:
            return f"postgres.{project_ref}"
        return "postgres"

    def _project_ref_from_supabase_url(self) -> Optional[str]:
        if not self.SUPABASE_URL:
            return None
        host = urlparse(self.SUPABASE_URL).hostname or ""
        if host.endswith(".supabase.co"):
            return host.split(".")[0]
        return None

    def _normalize_database_url(self, url: str) -> str:
        normalized = url
        if normalized.startswith("postgres://"):
            normalized = f"postgresql://{normalized[len('postgres://'):]}"
        if normalized.startswith("postgresql://"):
            normalized = f"postgresql+psycopg2://{normalized[len('postgresql://'):]}"

        parsed = urlparse(normalized)
        if parsed.scheme.startswith("postgresql"):
            query = dict(parse_qsl(parsed.query, keep_blank_values=True))
            host = parsed.hostname or ""
            if self._host_requires_ssl(host):
                query.setdefault("sslmode", self.SUPABASE_DB_SSLMODE)
            if self._host_is_supabase(host):
                query.setdefault("application_name", "werco_erp_supabase")
            normalized = urlunparse(parsed._replace(query=urlencode(query)))
        return normalized

    def _url_is_supabase(self, url: str) -> bool:
        parsed = urlparse(url)
        return self._host_is_supabase(parsed.hostname or "")

    def _url_is_sqlite(self, url: str) -> bool:
        return url.startswith("sqlite")

    def _host_requires_ssl(self, host: str) -> bool:
        return self._host_is_supabase(host) or self.ENVIRONMENT == "production"

    def _host_is_supabase(self, host: str) -> bool:
        return (
            host.endswith(".supabase.co")
            or host.endswith(".pooler.supabase.com")
            or "supabase.com" in host
        )

    # Rate Limiting
    RATE_LIMIT_ENABLED: bool = True
    RATE_LIMIT_TIMES: int = 100
    RATE_LIMIT_SECONDS: int = 60
    RATE_LIMIT_EXEMPT_PATHS: str = "/health,/api/docs,/api/openapi.json,/api/redoc"

    @property
    def rate_limit_exempt_paths_list(self) -> List[str]:
        return [path.strip() for path in self.RATE_LIMIT_EXEMPT_PATHS.split(",")]

    # CORS - Include localhost for dev; production origins must be set via env var
    CORS_ORIGINS: str = (
        "http://localhost:3000,http://localhost:3001,http://localhost:5173,http://localhost:8000"
    )
    CORS_ALLOW_CREDENTIALS: bool = True
    CORS_ALLOW_METHODS: str = "GET,POST,PUT,PATCH,DELETE,OPTIONS"
    CORS_ALLOW_HEADERS: str = (
        "Authorization,Content-Type,X-Requested-With,Accept,Origin,If-None-Match,If-Match"
    )

    @property
    def cors_origins_list(self) -> List[str]:
        origins = [
            origin.strip() for origin in self.CORS_ORIGINS.split(",") if origin.strip()
        ]
        validated = []
        for origin in origins:
            # Reject wildcard origins
            if origin == "*":
                raise ValueError(
                    "Wildcard '*' CORS origin is not allowed. Specify explicit origins."
                )
            # Require http:// or https:// scheme
            if not origin.startswith("http://") and not origin.startswith("https://"):
                raise ValueError(
                    f"CORS origin must start with http:// or https://: {origin}"
                )
            validated.append(origin)
        return validated

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
    ANTHROPIC_MODEL_SELECTION: str = "auto"
    ANTHROPIC_FAST_MODEL: str = "claude-haiku-4-5-20251001"
    ANTHROPIC_DEFAULT_MODEL: str = "claude-sonnet-4-6"
    ANTHROPIC_REASONING_MODEL: str = "claude-opus-4-8"
    ANTHROPIC_BOM_MODEL: Optional[str] = None
    ANTHROPIC_PO_MODEL: Optional[str] = None
    ANTHROPIC_ROUTING_MODEL: Optional[str] = None
    ANTHROPIC_QMS_MODEL: Optional[str] = None
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

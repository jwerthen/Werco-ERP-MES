from pathlib import Path
from typing import List, Optional
from urllib.parse import parse_qsl, quote, urlencode, urlparse, urlunparse

from pydantic import AliasChoices, Field, field_validator, model_validator
from pydantic_settings import BaseSettings

# Release stamp shipped INSIDE the deployed artifact. CI writes the commit SHA here
# immediately before `railway up` uploads backend/ (see .github/workflows/ci-cd.yml), and
# the Dockerfile's `COPY . .` carries it into the image at /app/RELEASE. Absent in local
# dev and in the test suite, which is fine — APP_RELEASE simply stays None.
RELEASE_FILE = Path(__file__).resolve().parents[2] / "RELEASE"

# Upper bound on the stamp we will echo from the unauthenticated /health/detailed
# endpoint. A git SHA is 40 chars; anything longer is a stray file, not a release.
MAX_RELEASE_LENGTH = 200


def read_release_file() -> Optional[str]:
    """Return the trimmed contents of :data:`RELEASE_FILE`, or ``None`` if unusable.

    Never raises: a missing, unreadable, empty, multi-line or oversized file degrades to
    "no release" rather than failing app boot or leaking a payload into a health check.
    """
    try:
        value = RELEASE_FILE.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return None
    if not value or len(value) > MAX_RELEASE_LENGTH:
        return None
    # Printable ASCII only. Rejects a multi-line file, and the UTF-16 mush Windows
    # PowerShell 5.1's `>` writes -- which decodes to NULs and U+FFFD replacement chars,
    # neither of which `.strip()` removes (the runbook says `Out-File -Encoding ascii`
    # for exactly this reason). /health/detailed is unauthenticated, so whatever
    # survives here is public: an unusable stamp must read as "no release", not as
    # garbage attributed to a build.
    if not value.isascii() or not value.isprintable():
        return None
    return value


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
    DB_POOL_PRE_PING: bool = True  # Test connections before use (handles stale connections)

    # Security - MUST be overridden via environment variables (no defaults - app fails fast if missing)
    SECRET_KEY: str
    REFRESH_TOKEN_SECRET_KEY: str
    ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 15  # Short-lived access tokens
    REFRESH_TOKEN_EXPIRE_DAYS: int = 7  # Refresh tokens valid for 7 days
    # Baked into each refresh token as an `absolute_timeout` claim at mint time and enforced
    # in verify_refresh_token. NOT a hard ceiling on total session life, despite the name:
    # POST /auth/refresh re-mints the refresh token through create_refresh_token, which takes
    # no parameter to carry the original claim forward and recomputes it from utcnow()
    # (security.py:66). Every refresh therefore RESETS the clock, so what this actually bounds
    # is an IDLE window — a user who touches the app once per window is never forced to
    # re-authenticate. That has been true since the setting was introduced and is unchanged
    # here. A change to this value takes effect on the next mint (refresh or login).
    #
    # Relaxed 24h -> 168h (7 days) on 2026-07-29. The 24h value was an IA-family
    # (NIST SP 800-171 3.1.11) session-termination rule carried over from the CMMC L2
    # effort that is no longer being pursued; its practical effect was forcing every
    # shop user idle for a day to re-authenticate. At 168h it equals REFRESH_TOKEN_EXPIRE_DAYS,
    # so the refresh window itself becomes the binding limit. The mechanism is kept,
    # not deleted: lower this env var to re-arm a tighter idle window without a code change.
    SESSION_ABSOLUTE_TIMEOUT_HOURS: int = 168  # Idle window (see above); 168h = the 7-day refresh window

    # Audit-log hash chain. When True (the default and the historical behavior) every
    # audited write takes ONE GLOBAL transaction-scoped Postgres advisory lock, held
    # until the caller's transaction commits, reads the chain tail, and SHA-256s the
    # full row including old/new payloads. That lock is a system-wide serialization
    # point across all tenants on every audited request.
    #
    # Set False to PAUSE the chain: rows are still written (same table, same columns,
    # same audit content) and the 008/060 database immutability triggers still block
    # UPDATE/DELETE, but sequence_number comes from a Postgres sequence instead of a
    # locked tail read, previous_hash is NULL, and integrity_hash is the
    # 'LEGACY_CHAIN_PAUSED' placeholder that the verifier already knows to skip.
    #
    # READ BEFORE FLIPPING — this is not fully reversible. Re-enabling relinks new
    # rows off whatever tail exists, so the chain resumes cleanly, but rows written
    # while paused can NEVER be made verifiable retroactively, and gap detection is
    # permanently lost across the paused window (a sequence legitimately leaves gaps
    # whenever a caller's transaction rolls back). What you keep while paused: the
    # audit rows themselves, the DB-level append-only triggers, and every read path.
    # What you lose: cryptographic proof that a row was not altered out of band.
    # See docs/AUDIT_LOG_RETENTION_RUNBOOK.md.
    AUDIT_HASH_CHAIN_ENABLED: bool = True

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
        # NOTE: the carrier-secret encryption key (INTEGRATION_ENCRYPTION_KEY /
        # WEBHOOK_ENCRYPTION_KEY) is intentionally NOT required here. It is enforced
        # LAZILY, at the point a carrier/webhook secret is actually encrypted or
        # decrypted (see app/services/carriers/crypto.py), so a deployment that does
        # not (yet) use carrier integration still boots and runs Alembic migrations
        # without a key. In production/staging, attempting to store or use a carrier
        # secret without an operator-provided key fails loudly there (CMMC SC-28)
        # rather than silently using an ephemeral key.
        if self.ENVIRONMENT == "production":
            if self.DEBUG:
                raise ValueError("DEBUG must be false in production.")
            if not self.CORS_ORIGINS:
                raise ValueError("CORS_ORIGINS must be set in production.")
            if "localhost" in self.CORS_ORIGINS:
                raise ValueError("CORS_ORIGINS must not include localhost in production.")
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
        return host.endswith(".supabase.co") or host.endswith(".pooler.supabase.com") or "supabase.com" in host

    @property
    def safe_database_host(self) -> str:
        parsed = urlparse(self.SQLALCHEMY_DATABASE_URL)
        return parsed.hostname or "local"

    def _build_supabase_database_url(self) -> str:
        project_ref = self.SUPABASE_PROJECT_REF or self._project_ref_from_supabase_url()
        host = self.SUPABASE_DB_HOST or (f"db.{project_ref}.supabase.co" if project_ref else None)
        password = self.SUPABASE_DB_PASSWORD or self.DB_PASSWORD
        if not host or not password:
            raise ValueError(
                "DATABASE_URL is not set. Configure the Supabase database with either DATABASE_URL "
                "or SUPABASE_PROJECT_REF/SUPABASE_DB_PASSWORD."
            )
        user = self.SUPABASE_DB_USER or self._default_supabase_db_user(host, project_ref)
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
        host = self.SUPABASE_DB_HOST or (f"db.{project_ref}.supabase.co" if project_ref else None)
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
        return host.endswith(".supabase.co") or host.endswith(".pooler.supabase.com") or "supabase.com" in host

    # Rate Limiting
    RATE_LIMIT_ENABLED: bool = True
    RATE_LIMIT_TIMES: int = 100
    RATE_LIMIT_SECONDS: int = 60
    RATE_LIMIT_EXEMPT_PATHS: str = "/health,/api/docs,/api/openapi.json,/api/redoc"

    @property
    def rate_limit_exempt_paths_list(self) -> List[str]:
        return [path.strip() for path in self.RATE_LIMIT_EXEMPT_PATHS.split(",")]

    # Maximum size (bytes) of an "application/json" request body the app will
    # accept. Over the cap the `limit_json_body_size` middleware in app/main.py
    # returns HTTP 413. General request-size hygiene: the middleware runs ahead
    # of every route's auth dependency, so without a cap an unauthenticated
    # caller decides how many bytes the app buffers into memory and hands to
    # json.loads, and how large the resulting Python object graph gets. Only
    # JSON is gated: multipart/UploadFile paths (every CSV/XLSX bulk import) are
    # untouched and carry their own per-endpoint caps, as are the carrier
    # webhooks, which HMAC-verify raw bytes and skip the middleware entirely.
    #
    # 256 KB clears the largest realistic bodies measured (laser-nest import at
    # 170 nests = 183 KB; BOM create at 1000 line items = 201 KB). The known
    # ceiling is a BOM create above roughly 1300 line items — which is why this
    # is env-overridable rather than a constant: ops can raise it without a
    # deploy.
    #
    # MAX_SANITIZED_JSON_BODY_BYTES is the pre-rename name, accepted as a
    # deprecated fallback so an env var set while the old name shipped keeps
    # taking effect. The "SANITIZED" was accurate only while this middleware
    # also bleach-stripped bodies; it no longer does (see the middleware
    # docstring). When both names are set the new one wins. Remove the fallback
    # once no environment sets the old name.
    MAX_JSON_BODY_BYTES: int = Field(
        default=262144,  # 256 KB
        validation_alias=AliasChoices("MAX_JSON_BODY_BYTES", "MAX_SANITIZED_JSON_BODY_BYTES"),
    )

    # CORS - Include localhost for dev; production origins must be set via env var
    CORS_ORIGINS: str = "http://localhost:3000,http://localhost:3001,http://localhost:5173,http://localhost:8000"
    CORS_ALLOW_CREDENTIALS: bool = True
    CORS_ALLOW_METHODS: str = "GET,POST,PUT,PATCH,DELETE,OPTIONS"
    CORS_ALLOW_HEADERS: str = "Authorization,Content-Type,X-Requested-With,Accept,Origin,If-None-Match,If-Match"

    @property
    def cors_origins_list(self) -> List[str]:
        origins = [origin.strip() for origin in self.CORS_ORIGINS.split(",") if origin.strip()]
        validated = []
        for origin in origins:
            # Reject wildcard origins
            if origin == "*":
                raise ValueError("Wildcard '*' CORS origin is not allowed. Specify explicit origins.")
            # Require http:// or https:// scheme
            if not origin.startswith("http://") and not origin.startswith("https://"):
                raise ValueError(f"CORS origin must start with http:// or https://: {origin}")
            validated.append(origin)
        return validated

    # Trusted Hosts (HTTP Host-header allowlist) — defense-in-depth against Host-header
    # poisoning (the Starlette CVE-2026-48710 class). Default "*" allows any Host for dev
    # convenience; LOCK THIS DOWN IN PRODUCTION to the API's real hostnames. When you do,
    # you MUST also include the health-check probe hosts or the deploy's health checks get a
    # 400: "localhost" (container HEALTHCHECK) and, on Railway, "healthcheck.railway.app".
    # e.g. ALLOWED_HOSTS="api.werco.com,erp.werco.com,localhost,healthcheck.railway.app".
    # An unexpected Host is otherwise only mitigated by Starlette's URL parsing; an explicit
    # allowlist rejects it with HTTP 400. Supports exact hosts and "*.example.com" wildcard
    # subdomains (a wildcard does NOT match the apex). Enforced by TrustedHostMiddleware in
    # app/main.py. See docs/ENVIRONMENT_VARIABLES.md.
    ALLOWED_HOSTS: str = "*"

    @property
    def allowed_hosts_list(self) -> List[str]:
        hosts = [h.strip() for h in self.ALLOWED_HOSTS.split(",") if h.strip()]
        return hosts or ["*"]

    # File Storage. STORAGE_BACKEND selects where PERSISTENT document bytes live
    # (quality documents, carrier labels/BOLs, RFQ package files, uploaded PO source
    # documents -- see app/services/storage_service.py):
    #   - "local": today's on-disk layout under UPLOAD_DIR (default; byte-for-byte
    #     unchanged behavior, but NOT durable on hosts without a volume).
    #   - "s3": AWS S3 or any S3-compatible store. Requires S3_BUCKET_NAME +
    #     AWS_ACCESS_KEY_ID + AWS_SECRET_ACCESS_KEY (validated at startup); set
    #     S3_ENDPOINT_URL for non-AWS stores (Railway buckets, Cloudflare R2).
    # Stored refs are dispatched per-row ("s3://..." vs local path), so existing
    # local rows remain servable after flipping to "s3".
    STORAGE_BACKEND: str = "local"
    AWS_ACCESS_KEY_ID: str = ""
    AWS_SECRET_ACCESS_KEY: str = ""
    AWS_REGION: str = "us-east-1"
    S3_BUCKET_NAME: str = "werco-erp-documents"
    S3_ENDPOINT_URL: Optional[str] = None

    @model_validator(mode="after")
    def validate_storage_backend(self) -> "Settings":
        """Fail fast on a misconfigured durable-storage backend (record retention)."""
        backend = (self.STORAGE_BACKEND or "local").lower()
        if backend not in ("local", "s3"):
            raise ValueError(f"STORAGE_BACKEND must be 'local' or 's3' (got {self.STORAGE_BACKEND!r}).")
        if backend == "s3":
            missing = [
                name
                for name, value in (
                    ("S3_BUCKET_NAME", self.S3_BUCKET_NAME),
                    ("AWS_ACCESS_KEY_ID", self.AWS_ACCESS_KEY_ID),
                    ("AWS_SECRET_ACCESS_KEY", self.AWS_SECRET_ACCESS_KEY),
                )
                if not value
            ]
            if missing:
                raise ValueError(
                    "STORAGE_BACKEND=s3 requires object-storage credentials; missing: "
                    + ", ".join(missing)
                    + ". For S3-compatible stores (Railway buckets, Cloudflare R2) also set S3_ENDPOINT_URL."
                )
        return self

    # Monitoring
    SENTRY_DSN: Optional[str] = None

    # Fraction of requests sampled as Sentry PERFORMANCE TRANSACTIONS (0.0-1.0).
    #
    # This does NOT affect error capture. Errors are governed by a different Sentry
    # setting (``sample_rate``, left at its 1.0 default), so every exception is still
    # sent no matter how low this goes. Lowering it loses performance traces only.
    #
    # Default 0.1 (10%) rather than 1.0 because this API is polled continuously by two
    # 30-second clients that generate no useful trace data: Railway's platform health
    # check (``/health``) and the shop-floor wallboard. Those two alone are ~5,800
    # transactions/day before a single human touches the app, and transaction volume
    # burns the same Sentry quota/on-demand budget that real errors are billed against
    # — once it is exhausted Sentry rate-limits *incoming events*, which is how a noisy
    # trace stream ends up costing you the error reports you actually turned Sentry on
    # for. 10% keeps a representative latency sample at ~1/10th the ingest.
    #
    # Raise it temporarily (e.g. 1.0) while chasing a specific latency regression.
    SENTRY_TRACES_SAMPLE_RATE: float = 0.1

    # Version/commit identifier reported to Sentry (``release``) and by
    # ``/health/detailed`` (``checks.application.release``) so an error or a running
    # container can be tied to the exact deployed commit.
    #
    # Normally NOT set by hand: CI writes the commit SHA to a ``RELEASE`` file that
    # ships inside the deployed artifact, and the validator below picks it up. Setting
    # the environment variable overrides the file. Unset (local dev, tests) it stays
    # None and both consumers simply report no release.
    APP_RELEASE: Optional[str] = None

    LOG_LEVEL: str = "INFO"

    @field_validator("SENTRY_TRACES_SAMPLE_RATE")
    @classmethod
    def validate_sentry_traces_sample_rate(cls, v: float) -> float:
        """Reject an out-of-range rate instead of silently sampling everything.

        Sentry treats any value >= 1.0 as "sample everything", so a config of ``10``
        written meaning "10 percent" would quietly do the opposite of what was intended
        — the exact quota-exhaustion failure this setting exists to prevent.
        """
        if not 0.0 <= v <= 1.0:
            raise ValueError(f"SENTRY_TRACES_SAMPLE_RATE must be between 0.0 and 1.0 (got {v!r}).")
        return v

    @model_validator(mode="after")
    def resolve_app_release(self) -> "Settings":
        """Fall back to the ``RELEASE`` file that CI ships inside the deployed artifact.

        The SHA is bound to the *artifact*, not to a platform-level environment
        variable, on purpose: a service variable is set independently of the build, so
        it keeps reporting the last SHA CI attempted even when that deploy failed or an
        older image was rolled back — reporting the wrong commit is worse than
        reporting none, since the whole point is answering "what is actually running?".
        A file that travels inside the image cannot drift from the code around it.

        NOTE: ``backend/RELEASE`` must stay OUT of ``.gitignore`` — ``railway up``
        honors it when choosing what to upload, so ignoring the file would silently
        stop shipping it.
        """
        if not self.APP_RELEASE:
            self.APP_RELEASE = read_release_file()
        return self

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

    # Base URL of the SPA, used to build absolute deep links in notification emails
    # (e.g. "https://app.werco.com"). Empty in dev/test -> emails render relative-less
    # links gracefully. No trailing slash.
    FRONTEND_BASE_URL: str = ""

    # SMS Configuration (Twilio) -- the notification SMS channel (PR 4).
    #
    # ALL values come from the environment; nothing is ever hardcoded. Every field is
    # optional and empty by default, so an unconfigured environment SOFT-SKIPS SMS
    # (logged, no raise) exactly like the unconfigured SMTP path.
    #
    # Two auth modes are supported; ``sms_service`` prefers the FIRST that resolves:
    #   1. API-key auth (PREFERRED): TWILIO_ACCOUNT_SID + TWILIO_API_KEY_SID (SK...)
    #      + TWILIO_API_KEY_SECRET. Revocable per key without rotating the account
    #      credential -- the CMMC-friendlier posture.
    #   2. Legacy auth-token auth: TWILIO_ACCOUNT_SID + TWILIO_AUTH_TOKEN.
    #
    # Sending additionally requires a sender: TWILIO_MESSAGING_SERVICE_SID (preferred
    # -- it carries the pool/compliance registration) or TWILIO_FROM_NUMBER (E.164).
    #
    # NOTE: these credentials only permit egress for companies whose
    # ``Company.allow_sms_egress`` CUI kill switch is ON (fail-closed in sms_service).
    TWILIO_ACCOUNT_SID: str = ""
    TWILIO_API_KEY_SID: str = ""
    TWILIO_API_KEY_SECRET: str = ""
    TWILIO_AUTH_TOKEN: str = ""
    TWILIO_FROM_NUMBER: str = ""
    TWILIO_MESSAGING_SERVICE_SID: str = ""
    # Default region used to parse a phone number typed without a country code
    # (shop-local: US). Stored values are always normalized to E.164.
    SMS_DEFAULT_REGION: str = "US"

    # Webhook Configuration
    WEBHOOK_ENCRYPTION_KEY: str = ""

    # Integration secret encryption (carrier-account API keys / webhook secrets,
    # and the ProxyBox thermal-printer API key). Fernet key; falls back to
    # WEBHOOK_ENCRYPTION_KEY when unset (see app/services/carriers/crypto.py).
    INTEGRATION_ENCRYPTION_KEY: str = ""

    # ProxyBox (pbxz.io) thermal-label print bridge tunables. Per-company connection
    # details (base URL / target / API key / egress toggle) live on
    # CompanyPrintProfile, NOT here -- these are only the HTTP timing knobs.
    PROXYBOX_TIMEOUT_SECONDS: float = 30.0  # per-request httpx timeout
    PROXYBOX_POLL_INTERVAL_SECONDS: float = 1.0  # job-status poll cadence
    PROXYBOX_MAX_WAIT_SECONDS: float = 30.0  # max wait for a terminal job state

    # Labor-cost / hour rollup on work-order completion (Batch 7 / rank 10).
    #
    # OPT-IN, DEFAULT OFF (product decision). When False (the default) work-order
    # completion preserves the pre-Batch-7 behavior: it does NOT auto-populate
    # ``WorkOrder.actual_hours`` / ``actual_cost`` and does NOT auto-sync a linked
    # ``JobCost`` -- the on-demand ``POST /job-costs/{id}/calculate`` endpoint remains
    # the only way to materialize cost actuals, so no untrusted labor-check-in data
    # surfaces as cost truth before a shop validates it. When True, completion
    # auto-rolls labor hours into the operation/WO actuals, computes
    # ``WorkOrder.actual_cost`` (labor + issued material + overhead), and syncs the
    # linked ``JobCost`` (status -> COMPLETED) in the same unit of work.
    #
    # The ``no_labor_recorded`` data-quality signal and the labor-hour rollup's
    # downstream-rate consistency (COST-5) are independent of this flag where noted.
    #
    # TODO(per-company): this is a GLOBAL flag because the Company model currently has
    # no settings/feature-flags JSON column (see app/models/company.py). When a
    # per-company settings field is added, promote this to a per-tenant flag so a
    # trusted shop can enable cost rollup without forcing it on every tenant; the
    # resolution helper (services/labor_cost_service.is_labor_cost_rollup_enabled)
    # is the single chokepoint to repoint at that field.
    LABOR_COST_ROLLUP_ENABLED: bool = False

    # Whether labor-cost rollups count ONLY supervisor-approved TimeEntries (G5-A).
    #
    # OPT-IN, default OFF. When OFF (the default) every CLOSED TimeEntry feeds the labor
    # cost legs exactly as before this flag existed -- byte-identical behavior. When ON,
    # the three labor-cost consumers (job_costing recompute, completion cost rollup, and
    # the analytics cost leg) additionally require ``TimeEntry.approved IS NOT NULL`` so
    # un-approved shop-floor labor is excluded from cost until a supervisor/quality lead
    # signs off (the approve/unapprove endpoints set ``approved`` / ``approved_by``).
    #
    # GLOBAL for the same reason as ``LABOR_COST_ROLLUP_ENABLED`` (no per-company
    # settings column yet); resolved through the SAME chokepoint
    # (services/labor_cost_service.is_approved_labor_required) so it can be promoted to a
    # per-tenant flag in one place later.
    REQUIRE_APPROVED_LABOR_FOR_COST: bool = False

    # Default labor rate ($/hour) used when a work center has no ``hourly_rate``
    # (COST-5). The shared rate resolver prefers ``WorkCenter.hourly_rate`` so labor
    # cost reflects WHERE the work happened, and falls back to this single
    # configurable value -- used by BOTH the completion rollup and the analytics
    # cost report so the two views can never disagree (replaces the old hardcoded
    # $45 / $50 split).
    DEFAULT_LABOR_RATE: float = 75.0
    # Default overhead/burden rate ($/hour) charged on labor when a work center
    # carries no overhead rate. Applied to actual labor hours to populate the
    # overhead leg of ``actual_cost`` / JobCost overhead.
    DEFAULT_OVERHEAD_RATE: float = 0.0

    # Shop-floor dashboard reconcile cap (PERF-3 / rank 12). The dashboard read
    # path reconciles every RELEASED/IN_PROGRESS/ON_HOLD WO from durable shop-floor
    # evidence; that scan is unbounded and write-amplifying as the open-WO set grows.
    # Bound it to the most-recently-touched N WOs (most likely to carry new
    # completion evidence). Reconcile is best-effort and idempotent -- any WO beyond
    # the cap is still reconciled when opened in its detail/operations-list views;
    # the durable fix is the deferred ARQ reconcile job. When a run hits this cap the
    # handler logs a WARNING that the shop has outgrown read-path reconcile.
    SHOP_FLOOR_DASHBOARD_RECONCILE_LIMIT: int = 250

    # Audit Log Retention / Archival (CMMC AU-3.3.8 + AS9100D)
    # Audit logs are immutable (DB triggers block UPDATE/DELETE) and must NOT be
    # row-deleted by maintenance jobs. Aged rows are exported to cold storage by
    # the archival job; physical removal from the online DB, if ever needed, is a
    # deliberate DBA partition-drop, never an automated row delete. See
    # docs/AUDIT_LOG_RETENTION_RUNBOOK.md.
    AUDIT_ARCHIVE_ENABLED: bool = True
    # Cold-storage destination for exported audit segments. In production point
    # this at a mounted, backed-up volume or object-store mount.
    AUDIT_ARCHIVE_DIR: str = "/var/lib/werco/audit-archive"
    # Fallback retention window (days) when a company has no active
    # security_audit_record RetentionPolicy row. 1095 = 3 years, matching the
    # seeded security_audit_record policy minimum.
    AUDIT_RETENTION_DAYS_DEFAULT: int = 1095
    # Safety cap on rows exported per company per run (large backlogs drain over
    # successive runs via the ExportEvent high-water mark).
    AUDIT_ARCHIVE_MAX_ROWS_PER_RUN: int = 50000

    class Config:
        env_file = ".env"
        case_sensitive = True


settings = Settings()

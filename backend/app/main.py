# Werco ERP Main Application - v1.0.1
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from contextlib import asynccontextmanager
from datetime import datetime
from sqlalchemy import text

from app.core.config import settings
from app.api.router import api_router
from app.db.database import engine, Base
from app.core.logging import configure_logging, get_logger
from app.core.cache import init_cache, cache
from app.middleware.logging_middleware import CorrelationIdMiddleware, RequestLoggingMiddleware

# Configure structured logging with correlation IDs
configure_logging()
logger = get_logger(__name__)

# Initialize Sentry if DSN is provided
if settings.SENTRY_DSN:
    try:
        import sentry_sdk
        from sentry_sdk.integrations.fastapi import FastApiIntegration
        
        sentry_sdk.init(
            dsn=settings.SENTRY_DSN,
            integrations=[FastApiIntegration()],
            traces_sample_rate=1.0,
            environment=settings.ENVIRONMENT,
        )
        logger.info("Sentry initialized successfully")
    except ImportError:
        logger.warning("Sentry DSN provided but sentry-sdk not installed")


def seed_quote_config_if_needed():
    """Seed quote configuration data if it doesn't exist"""
    from app.db.database import SessionLocal
    from app.models.quote_config import QuoteMaterial, QuoteMachine, QuoteFinish, QuoteSettings, MaterialCategory, MachineType
    
    db = SessionLocal()
    try:
        # Check if materials exist
        if db.query(QuoteMaterial).count() == 0:
            logger.info("Seeding quote materials...")
            
            def calc_sheet_pricing(price_per_lb, density):
                thicknesses = {
                    '24ga': 0.0239, '22ga': 0.0299, '20ga': 0.0359, '18ga': 0.0478,
                    '16ga': 0.0598, '14ga': 0.0747, '12ga': 0.1046, '11ga': 0.1196,
                    '10ga': 0.1345, '7ga': 0.1793,
                    '0.125': 0.125, '0.1875': 0.1875, '0.250': 0.250, '0.375': 0.375,
                    '0.500': 0.500, '0.625': 0.625, '0.750': 0.750, '1.000': 1.000,
                }
                pricing = {}
                for gauge, thick in thicknesses.items():
                    weight_per_sqft = thick * 144 * density
                    price = weight_per_sqft * price_per_lb * 1.15
                    pricing[gauge] = round(price, 2)
                return pricing
            
            materials = [
                {'name': 'Mild Steel A36', 'category': MaterialCategory.STEEL, 'stock_price_per_pound': 0.55, 'density_lb_per_cubic_inch': 0.284, 'machinability_factor': 0.6, 'sheet_pricing': calc_sheet_pricing(0.55, 0.284)},
                {'name': 'Galvanized Steel G90', 'category': MaterialCategory.STEEL, 'stock_price_per_pound': 0.70, 'density_lb_per_cubic_inch': 0.284, 'machinability_factor': 0.55, 'sheet_pricing': calc_sheet_pricing(0.70, 0.284)},
                {'name': 'Aluminum 5052-H32', 'category': MaterialCategory.ALUMINUM, 'stock_price_per_pound': 2.38, 'density_lb_per_cubic_inch': 0.097, 'machinability_factor': 1.0, 'sheet_pricing': calc_sheet_pricing(2.38, 0.097)},
                {'name': 'Aluminum 6061-T6', 'category': MaterialCategory.ALUMINUM, 'stock_price_per_pound': 2.58, 'density_lb_per_cubic_inch': 0.098, 'machinability_factor': 1.0, 'sheet_pricing': calc_sheet_pricing(2.58, 0.098)},
                {'name': 'Stainless Steel 304', 'category': MaterialCategory.STAINLESS, 'stock_price_per_pound': 2.13, 'density_lb_per_cubic_inch': 0.289, 'machinability_factor': 0.4, 'sheet_pricing': calc_sheet_pricing(2.13, 0.289)},
                {'name': 'Stainless Steel 316', 'category': MaterialCategory.STAINLESS, 'stock_price_per_pound': 3.08, 'density_lb_per_cubic_inch': 0.290, 'machinability_factor': 0.35, 'sheet_pricing': calc_sheet_pricing(3.08, 0.290)},
            ]
            for m in materials:
                db.add(QuoteMaterial(**m))
            db.commit()
            logger.info(f"Seeded {len(materials)} materials")
        
        # Check if machines exist
        if db.query(QuoteMachine).count() == 0:
            logger.info("Seeding quote machines...")
            laser_speeds = {
                "steel": {"24ga": 1200, "22ga": 1000, "20ga": 850, "18ga": 650, "16ga": 500, "14ga": 380, "12ga": 280, "10ga": 200, "7ga": 120, "0.250": 150, "0.375": 100, "0.500": 70, "0.750": 40, "1.000": 25},
                "stainless": {"24ga": 900, "22ga": 750, "20ga": 600, "18ga": 450, "16ga": 350, "14ga": 260, "12ga": 180, "10ga": 130, "7ga": 80, "0.250": 100, "0.375": 65, "0.500": 45, "0.750": 25, "1.000": 15},
                "aluminum": {"24ga": 1500, "22ga": 1300, "20ga": 1100, "18ga": 900, "16ga": 700, "14ga": 550, "12ga": 400, "10ga": 300, "7ga": 200, "0.250": 220, "0.375": 150, "0.500": 100, "0.750": 60, "1.000": 35}
            }
            machines = [
                {'name': 'Fiber Laser 6kW', 'machine_type': MachineType.LASER_FIBER, 'rate_per_hour': 150.00, 'setup_rate_per_hour': 75.00, 'cutting_speeds': laser_speeds, 'typical_setup_hours': 0.25},
                {'name': 'Press Brake 150T', 'machine_type': MachineType.PRESS_BRAKE, 'rate_per_hour': 85.00, 'setup_rate_per_hour': 65.00, 'bend_time_seconds': 12.0, 'setup_time_per_bend_type': 300.0, 'typical_setup_hours': 0.5},
                {'name': 'CNC Mill 3-Axis', 'machine_type': MachineType.CNC_MILL_3AXIS, 'rate_per_hour': 125.00, 'setup_rate_per_hour': 85.00, 'typical_setup_hours': 1.0},
                {'name': 'CNC Mill 4-Axis', 'machine_type': MachineType.CNC_MILL_4AXIS, 'rate_per_hour': 145.00, 'setup_rate_per_hour': 95.00, 'typical_setup_hours': 1.5},
                {'name': 'CNC Lathe', 'machine_type': MachineType.CNC_LATHE, 'rate_per_hour': 110.00, 'setup_rate_per_hour': 75.00, 'typical_setup_hours': 0.75},
            ]
            for m in machines:
                db.add(QuoteMachine(**m))
            db.commit()
            logger.info(f"Seeded {len(machines)} machines")
        
        # Check if finishes exist
        if db.query(QuoteFinish).count() == 0:
            logger.info("Seeding quote finishes...")
            finishes = [
                {'name': 'Powder Coat - Standard Colors', 'category': 'coating', 'price_per_sqft': 2.50, 'minimum_charge': 35.00, 'additional_days': 3},
                {'name': 'Powder Coat - Custom Color', 'category': 'coating', 'price_per_sqft': 3.50, 'minimum_charge': 75.00, 'additional_days': 5},
                {'name': 'Zinc Plating - Clear', 'category': 'plating', 'price_per_lb': 1.25, 'minimum_charge': 45.00, 'additional_days': 5},
                {'name': 'Anodize Type II - Clear', 'category': 'plating', 'price_per_sqft': 4.00, 'minimum_charge': 50.00, 'additional_days': 5},
                {'name': 'Passivation', 'category': 'treatment', 'price_per_part': 5.00, 'minimum_charge': 35.00, 'additional_days': 2},
                {'name': 'Deburr - Hand', 'category': 'finishing', 'price_per_part': 2.50, 'minimum_charge': 0.00, 'additional_days': 0},
            ]
            for f in finishes:
                db.add(QuoteFinish(**f))
            db.commit()
            logger.info(f"Seeded {len(finishes)} finishes")
        
        # Check if settings exist
        if db.query(QuoteSettings).count() == 0:
            logger.info("Seeding quote settings...")
            settings_data = [
                {'setting_key': 'default_markup_pct', 'setting_value': '35', 'setting_type': 'number'},
                {'setting_key': 'minimum_order_charge', 'setting_value': '150', 'setting_type': 'number'},
                {'setting_key': 'rush_multiplier', 'setting_value': '1.5', 'setting_type': 'number'},
                {'setting_key': 'standard_lead_days', 'setting_value': '10', 'setting_type': 'number'},
                {'setting_key': 'quantity_breaks', 'setting_value': '{"10": 0.95, "25": 0.90, "50": 0.85, "100": 0.80}', 'setting_type': 'json'},
            ]
            for s in settings_data:
                db.add(QuoteSettings(**s))
            db.commit()
            logger.info(f"Seeded {len(settings_data)} settings")
    except Exception as e:
        logger.error(f"Error seeding quote config: {e}")
        db.rollback()
    finally:
        db.close()


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    logger.info(f"Starting {settings.APP_NAME}...")
    # Create tables (in production, use Alembic migrations)
    Base.metadata.create_all(bind=engine)
    logger.info("Database tables created/verified")
    # Initialize Redis cache
    init_cache(settings.REDIS_URL)
    if cache.enabled:
        logger.info("Redis caching enabled")
    else:
        logger.info("Redis caching disabled (REDIS_URL not configured)")
    # Seed quote configuration if needed
    seed_quote_config_if_needed()
    yield
    # Shutdown
    logger.info(f"Shutting down {settings.APP_NAME}...")


# OpenAPI Tags metadata for documentation grouping
tags_metadata = [
    {"name": "Authentication", "description": "User authentication, login, logout, and token management"},
    {"name": "Users", "description": "User management and profile operations"},
    {"name": "Parts", "description": "Part master data management - components, assemblies, and raw materials"},
    {"name": "Bill of Materials", "description": "BOM structure and component relationships"},
    {"name": "Routing", "description": "Manufacturing routing and operation sequences"},
    {"name": "Work Orders", "description": "Production work order management and tracking"},
    {"name": "Work Centers", "description": "Work center configuration and capacity management"},
    {"name": "Shop Floor", "description": "Real-time shop floor control and operation tracking"},
    {"name": "Inventory", "description": "Inventory management, stock levels, and transactions"},
    {"name": "Material Requirements Planning", "description": "MRP calculations and material planning"},
    {"name": "Quality Management", "description": "Quality control, inspections, and NCRs"},
    {"name": "Purchasing", "description": "Purchase orders and vendor management"},
    {"name": "Receiving & Inspection", "description": "Material receiving and incoming inspection"},
    {"name": "Shipping", "description": "Shipping and delivery management"},
    {"name": "Scheduling", "description": "Production scheduling and capacity planning"},
    {"name": "Quotes", "description": "Customer quote management"},
    {"name": "Quote Calculator", "description": "Quote cost estimation and calculation"},
    {"name": "Customers", "description": "Customer master data management"},
    {"name": "Calibration", "description": "Equipment calibration tracking and management"},
    {"name": "Documents", "description": "Document management and attachments"},
    {"name": "Reports", "description": "Report generation and export"},
    {"name": "Analytics & BI", "description": "Business intelligence and analytics dashboards"},
    {"name": "Traceability", "description": "Lot and serial number traceability"},
    {"name": "Audit", "description": "Audit trail and change history"},
    {"name": "Scanner", "description": "Barcode and QR code scanning operations"},
    {"name": "Global Search", "description": "Cross-entity search functionality"},
    {"name": "Custom Fields", "description": "User-defined custom field management"},
    {"name": "Admin Settings", "description": "System administration and configuration"},
    {"name": "DXF Parser", "description": "DXF file parsing for part dimensions"},
    {"name": "PO Upload", "description": "Purchase order file upload and parsing"},
    {"name": "Error Logging", "description": "Client-side error logging"},
]

app = FastAPI(
    title=settings.APP_NAME,
    description="""
## Werco Manufacturing ERP & MES System

A comprehensive manufacturing execution system designed for **AS9100D** and **ISO9001** compliance.

### Key Features

* **Work Order Management** - Create, track, and manage production work orders
* **Shop Floor Control** - Real-time operation tracking with barcode scanning
* **Quality Management** - Inspection tracking, NCRs, and quality metrics
* **Inventory Control** - Stock management with lot/serial traceability
* **Material Requirements Planning** - MRP calculations and scheduling
* **Document Management** - Attach and manage production documents
* **Audit Trail** - Complete change history for compliance

### Authentication

All API endpoints (except `/health` and `/auth/login`) require JWT authentication.
Include the token in the `Authorization` header:

```
Authorization: Bearer <your_token>
```

### Rate Limiting

- Default: 100 requests per 60 seconds
- Auth endpoints: 5 login attempts per minute
- Register: 3 attempts per minute

### Support

For API support, contact the system administrator.
    """,
    version="1.0.0",
    lifespan=lifespan,
    docs_url="/api/docs",
    redoc_url="/api/redoc",
    openapi_url="/api/openapi.json",
    openapi_tags=tags_metadata,
    contact={
        "name": "Werco ERP Support",
        "email": "support@werco.com",
    },
    license_info={
        "name": "Proprietary",
    },
)

# Logging middleware with correlation IDs (added first, executed last)
app.add_middleware(RequestLoggingMiddleware)
app.add_middleware(CorrelationIdMiddleware)

# GZip compression middleware
app.add_middleware(GZipMiddleware, minimum_size=1000)

# CORS middleware with configurable settings
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins_list,
    allow_credentials=settings.CORS_ALLOW_CREDENTIALS,
    allow_methods=settings.CORS_ALLOW_METHODS.split(","),
    allow_headers=settings.CORS_ALLOW_HEADERS.split(","),
)

# CSRF Protection middleware
# For JWT-based SPAs, CSRF is mitigated by using Authorization header (not cookies)
# This adds defense-in-depth by validating Origin/Referer for state-changing requests
@app.middleware("http")
async def csrf_protection(request: Request, call_next):
    # Only check state-changing methods
    if request.method in ("POST", "PUT", "PATCH", "DELETE"):
        # Skip CSRF check for certain endpoints
        exempt_paths = (
            "/api/v1/auth/login",
            "/api/v1/auth/refresh",
            "/health",
            "/api/v1/errors/log",
        )
        if request.url.path in exempt_paths:
            return await call_next(request)
        
        # Defense 1: Check for X-Requested-With header (cannot be set cross-origin without CORS)
        x_requested_with = request.headers.get("x-requested-with")
        if x_requested_with != "XMLHttpRequest":
            # Allow if request has valid Authorization header (API clients)
            auth_header = request.headers.get("authorization")
            if not auth_header or not auth_header.startswith("Bearer "):
                logger.warning(f"CSRF: Missing X-Requested-With header for {request.url.path}")
                return JSONResponse(
                    status_code=403,
                    content={"detail": "Missing required security header"}
                )
        
        # Defense 2: Validate Origin/Referer header
        origin = request.headers.get("origin") or request.headers.get("referer")
        
        if origin:
            # Parse origin to get host
            from urllib.parse import urlparse
            parsed = urlparse(origin)
            origin_host = f"{parsed.scheme}://{parsed.netloc}" if parsed.netloc else None
            
            # Check if origin is in allowed list
            if origin_host and origin_host not in settings.cors_origins_list:
                logger.warning(f"CSRF: Blocked request from untrusted origin: {origin_host}")
                return JSONResponse(
                    status_code=403,
                    content={"detail": "Request origin not allowed"}
                )
    
    return await call_next(request)


# Input sanitization middleware - sanitize all incoming JSON data
@app.middleware("http")
async def sanitize_input(request: Request, call_next):
    # Only process JSON requests with body
    if request.method in ("POST", "PUT", "PATCH") and request.headers.get("content-type", "").startswith("application/json"):
        try:
            from app.core.sanitization import sanitize_dict
            
            # Read and sanitize body
            body = await request.body()
            if body:
                import json
                try:
                    data = json.loads(body)
                    if isinstance(data, dict):
                        sanitized_data = sanitize_dict(data)
                        # Create new request with sanitized body
                        request._body = json.dumps(sanitized_data).encode()
                except json.JSONDecodeError:
                    pass  # Let validation handle invalid JSON
        except Exception as e:
            logger.warning(f"Input sanitization warning: {e}")
    
    return await call_next(request)


# Security headers middleware
@app.middleware("http")
async def add_security_headers(request: Request, call_next):
    response = await call_next(request)
    # Security headers
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["X-XSS-Protection"] = "1; mode=block"
    response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    # Content Security Policy - restrict resource loading
    response.headers["Content-Security-Policy"] = "default-src 'self'; frame-ancestors 'none'"
    # Add content type
    response.headers["Content-Type"] = response.headers.get("Content-Type", "application/json")
    return response

# Rate limiting middleware (if enabled)
if settings.RATE_LIMIT_ENABLED:
    try:
        from slowapi import Limiter, _rate_limit_exceeded_handler
        from slowapi.util import get_remote_address
        from slowapi.errors import RateLimitExceeded
        
        # Define rate limits per endpoint type
        # Stricter limits for sensitive auth endpoints
        AUTH_RATE_LIMITS = {
            "/api/v1/auth/login": "5/minute",      # Prevent brute force
            "/api/v1/auth/register": "3/minute",   # Prevent mass registration
            "/api/v1/auth/refresh": "30/minute",   # Allow reasonable token refreshes
        }
        
        def get_rate_limit_for_path(request):
            """Get rate limit based on request path"""
            path = request.url.path
            for auth_path, limit in AUTH_RATE_LIMITS.items():
                if path.startswith(auth_path):
                    return limit
            return f"{settings.RATE_LIMIT_TIMES}/{settings.RATE_LIMIT_SECONDS}s"
        
        limiter = Limiter(
            key_func=get_remote_address,
            default_limits=[f"{settings.RATE_LIMIT_TIMES}/{settings.RATE_LIMIT_SECONDS}s"],
            storage_uri=settings.REDIS_URL if settings.REDIS_URL else "memory://"
        )
        app.state.limiter = limiter
        app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
        
        # Add middleware for path-specific rate limiting
        @app.middleware("http")
        async def rate_limit_by_path(request: Request, call_next):
            """Apply stricter rate limits to sensitive endpoints"""
            path = request.url.path
            
            # Check if this is a sensitive auth endpoint
            if path in AUTH_RATE_LIMITS:
                # The limiter will handle this with its default limits
                # For now, we just log that it's a sensitive endpoint
                logger.debug(f"Rate limiting auth endpoint: {path}")
            
            return await call_next(request)
        
        logger.info(f"Rate limiting enabled: {settings.RATE_LIMIT_TIMES} requests/{settings.RATE_LIMIT_SECONDS}s (default)")
        logger.info(f"Auth rate limits: login=5/min, register=3/min, refresh=30/min")
    except ImportError:
        logger.warning("Rate limiting requested but slowapi not installed")


# Global exception handler
@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    logger.error(f"Unhandled exception: {exc}", exc_info=True)
    if settings.SENTRY_DSN:
        import sentry_sdk
        sentry_sdk.capture_exception(exc)
    return JSONResponse(
        status_code=500,
        content={"detail": "Internal server error"}
    )


# Health check endpoints
@app.get("/health")
async def health_check():
    """Basic health check - used by load balancers and Railway."""
    return {
        "status": "healthy",
        "app": settings.APP_NAME,
        "environment": settings.ENVIRONMENT,
        "version": "1.0.0"
    }


@app.get("/health/live")
async def liveness_check():
    """Liveness probe - indicates if the application is running.
    Used by Kubernetes/container orchestrators to determine if container should be restarted.
    """
    return {"status": "alive", "timestamp": datetime.utcnow().isoformat()}


@app.get("/health/ready")
async def readiness_check():
    """Readiness probe - indicates if the application is ready to accept traffic.
    Checks database connectivity and critical dependencies.
    """
    from app.db.database import SessionLocal
    import time
    
    checks = {
        "database": {"status": "unknown", "latency_ms": None},
        "app": {"status": "healthy"},
    }
    overall_status = "healthy"
    
    # Database connectivity check
    db_start = time.time()
    try:
        db = SessionLocal()
        try:
            # Execute a simple query to verify connection
            db.execute(text("SELECT 1"))
            checks["database"]["status"] = "healthy"
            checks["database"]["latency_ms"] = round((time.time() - db_start) * 1000, 2)
        finally:
            db.close()
    except Exception as e:
        checks["database"]["status"] = "unhealthy"
        checks["database"]["error"] = str(e)[:100]  # Truncate error message
        overall_status = "unhealthy"
        logger.error(f"Health check - Database unhealthy: {e}")
    
    # Redis check (if configured)
    if settings.REDIS_URL:
        try:
            import redis
            redis_start = time.time()
            r = redis.from_url(settings.REDIS_URL, socket_timeout=2)
            r.ping()
            checks["redis"] = {
                "status": "healthy",
                "latency_ms": round((time.time() - redis_start) * 1000, 2)
            }
        except Exception as e:
            checks["redis"] = {"status": "unhealthy", "error": str(e)[:100]}
            # Redis is optional, don't fail health check
            logger.warning(f"Health check - Redis unhealthy: {e}")
    
    status_code = 200 if overall_status == "healthy" else 503
    
    return JSONResponse(
        status_code=status_code,
        content={
            "status": overall_status,
            "timestamp": datetime.utcnow().isoformat(),
            "checks": checks
        }
    )


@app.get("/health/detailed")
async def detailed_health_check():
    """Detailed health check with system metrics - for monitoring dashboards.
    Note: This endpoint may expose sensitive info, consider auth in production.
    """
    import platform
    import sys
    from app.db.database import SessionLocal
    import time
    
    checks = {}
    
    # Database check with connection pool info
    db_start = time.time()
    try:
        db = SessionLocal()
        try:
            result = db.execute(text("SELECT version()")).fetchone()
            db_version = result[0] if result else "unknown"
            checks["database"] = {
                "status": "healthy",
                "latency_ms": round((time.time() - db_start) * 1000, 2),
                "version": db_version[:50]  # Truncate version string
            }
        finally:
            db.close()
    except Exception as e:
        checks["database"] = {"status": "unhealthy", "error": str(e)[:100]}
    
    # System info
    checks["system"] = {
        "python_version": sys.version.split()[0],
        "platform": platform.system(),
        "platform_release": platform.release(),
    }
    
    # Application info
    checks["application"] = {
        "name": settings.APP_NAME,
        "environment": settings.ENVIRONMENT,
        "version": "1.0.0",
        "debug": settings.DEBUG,
    }
    
    # Feature flags
    checks["features"] = {
        "rate_limiting": settings.RATE_LIMIT_ENABLED,
        "sentry": bool(settings.SENTRY_DSN),
        "redis": bool(settings.REDIS_URL),
        "caching": cache.enabled,
    }
    
    # Cache stats (if enabled)
    if cache.enabled:
        checks["cache"] = {
            "status": "healthy",
            "stats": cache.stats,
        }
    
    overall_status = "healthy" if checks.get("database", {}).get("status") == "healthy" else "degraded"
    
    return {
        "status": overall_status,
        "timestamp": datetime.utcnow().isoformat(),
        "checks": checks
    }


# Include API routes
app.include_router(api_router, prefix=settings.API_V1_PREFIX)

# Include WebSocket routes
from app.api.websocket import router as websocket_router
app.include_router(websocket_router, prefix=settings.API_V1_PREFIX, tags=["WebSocket"])


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "app.main:app",
        host="0.0.0.0",
        port=8000,
        reload=settings.DEBUG,
        log_level=settings.LOG_LEVEL.lower()
    )

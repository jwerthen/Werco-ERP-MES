from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from contextlib import asynccontextmanager
import logging
import sys

from app.core.config import settings
from app.api.router import api_router
from app.db.database import engine, Base

# Configure structured logging
logging.basicConfig(
    level=getattr(logging, settings.LOG_LEVEL),
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger(__name__)

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
    # Seed quote configuration if needed
    seed_quote_config_if_needed()
    yield
    # Shutdown
    logger.info(f"Shutting down {settings.APP_NAME}...")


app = FastAPI(
    title=settings.APP_NAME,
    description="Werco Manufacturing ERP & MES System - AS9100D & ISO9001 Compliant",
    version="1.0.0",
    lifespan=lifespan,
    docs_url="/api/docs",
    redoc_url="/api/redoc",
    openapi_url="/api/openapi.json"
)

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
        
        limiter = Limiter(
            key_func=get_remote_address,
            default_limits=[f"{settings.RATE_LIMIT_TIMES}/{settings.RATE_LIMIT_SECONDS}s"],
            storage_uri=settings.REDIS_URL if settings.REDIS_URL else "memory://"
        )
        app.state.limiter = limiter
        app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
        logger.info(f"Rate limiting enabled: {settings.RATE_LIMIT_TIMES} requests/{settings.RATE_LIMIT_SECONDS}s")
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


# Enhanced health check
@app.get("/health")
async def health_check():
    """Health check endpoint with system status."""
    return {
        "status": "healthy",
        "app": settings.APP_NAME,
        "environment": settings.ENVIRONMENT,
        "version": "1.0.0"
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

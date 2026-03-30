"""
Logging Middleware for Request/Response Tracking.

Features:
- Correlation ID generation and propagation
- Request/response logging with timing
- User context extraction
- Error logging with stack traces
"""
import time
from typing import Callable
from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.types import ASGIApp

from app.core.logging import (
    get_logger,
    generate_correlation_id,
    set_correlation_id,
    set_request_context,
    get_correlation_id,
)

logger = get_logger(__name__)


class CorrelationIdMiddleware(BaseHTTPMiddleware):
    """
    Middleware to generate and propagate correlation IDs.
    
    - Checks for existing correlation ID in X-Correlation-ID header
    - Generates new ID if not present
    - Adds correlation ID to response headers
    - Sets correlation ID in context for logging
    """
    
    HEADER_NAME = "X-Correlation-ID"
    
    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        # Get or generate correlation ID
        correlation_id = request.headers.get(self.HEADER_NAME)
        if not correlation_id:
            correlation_id = generate_correlation_id()
        
        # Set in context for logging
        set_correlation_id(correlation_id)
        
        # Process request
        response = await call_next(request)
        
        # Add correlation ID to response headers
        response.headers[self.HEADER_NAME] = correlation_id
        
        return response


class RequestLoggingMiddleware(BaseHTTPMiddleware):
    """
    Middleware to log requests and responses.
    
    Logs:
    - Request method, path, query params
    - User ID (if authenticated)
    - Response status code
    - Request duration
    """
    
    # Paths to skip logging (health checks, static files)
    SKIP_PATHS = {"/health", "/health/live", "/health/ready", "/health/detailed", "/favicon.ico"}
    
    # Headers to exclude from logging (sensitive)
    EXCLUDED_HEADERS = {"authorization", "cookie", "x-api-key"}
    
    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        # Skip logging for certain paths
        if request.url.path in self.SKIP_PATHS or request.method in ("OPTIONS", "HEAD"):
            return await call_next(request)
        
        start_time = time.perf_counter()
        
        # Extract request context with sensitive parameter redaction
        SENSITIVE_PARAMS = {"password", "token", "secret", "key", "api_key", "refresh_token", "access_token"}
        query_str = None
        if request.query_params:
            redacted_params = []
            for k, v in request.query_params.items():
                if k.lower() in SENSITIVE_PARAMS:
                    redacted_params.append(f"{k}=[REDACTED]")
                else:
                    redacted_params.append(f"{k}={v}")
            query_str = "&".join(redacted_params)

        request_context = {
            "method": request.method,
            "path": request.url.path,
            "query": query_str,
            "client_ip": self._get_client_ip(request),
            "user_agent": request.headers.get("user-agent", "")[:100],
        }
        
        # Set request context for logging
        set_request_context(request_context)
        
        # Log request start only in debug to reduce log volume in production
        if logger.isEnabledFor(10):
            logger.debug(f"Request started: {request.method} {request.url.path}")
        
        try:
            response = await call_next(request)
            
            # Calculate duration
            duration_ms = (time.perf_counter() - start_time) * 1000
            
            # Log request completion
            log_data = {
                "status_code": response.status_code,
                "duration_ms": round(duration_ms, 2),
            }
            
            # Log level based on status code
            if response.status_code >= 500:
                logger.error(f"Request failed: {request.method} {request.url.path}", data=log_data)
            elif response.status_code >= 400:
                logger.warning(f"Request error: {request.method} {request.url.path}", data=log_data)
            else:
                if logger.isEnabledFor(10):
                    logger.debug(f"Request completed: {request.method} {request.url.path}", data=log_data)
            
            return response
            
        except Exception as e:
            duration_ms = (time.perf_counter() - start_time) * 1000
            logger.error(
                f"Request exception: {request.method} {request.url.path}",
                data={"duration_ms": round(duration_ms, 2), "error": str(e)},
                exc_info=True
            )
            raise
    
    def _get_client_ip(self, request: Request) -> str:
        """Extract client IP, considering proxy headers.

        Only trusts proxy headers when the direct connection is from a known
        private/loopback address (i.e. a trusted reverse proxy).
        """
        import ipaddress

        direct_ip = request.client.host if request.client else "unknown"

        # Only trust proxy headers if the direct connection is from a private/loopback IP
        is_trusted_proxy = False
        try:
            addr = ipaddress.ip_address(direct_ip)
            is_trusted_proxy = addr.is_private or addr.is_loopback
        except ValueError:
            pass

        if is_trusted_proxy:
            forwarded = request.headers.get("x-forwarded-for")
            if forwarded:
                # Take the first (client) IP from the chain
                client_ip = forwarded.split(",")[0].strip()
                # Validate it looks like an IP address
                try:
                    ipaddress.ip_address(client_ip)
                    return client_ip
                except ValueError:
                    pass

            real_ip = request.headers.get("x-real-ip")
            if real_ip:
                try:
                    ipaddress.ip_address(real_ip)
                    return real_ip
                except ValueError:
                    pass

        return direct_ip


def setup_logging_middleware(app: ASGIApp) -> None:
    """
    Add logging middleware to FastAPI app.
    
    Order matters - CorrelationIdMiddleware should be added first
    so correlation ID is available for RequestLoggingMiddleware.
    """
    # Add in reverse order (last added = first executed)
    app.add_middleware(RequestLoggingMiddleware)
    app.add_middleware(CorrelationIdMiddleware)

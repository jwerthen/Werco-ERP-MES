"""
Structured Logging with Correlation IDs for Production Debugging.

Features:
- JSON-formatted logs for log aggregation (ELK, CloudWatch, etc.)
- Correlation ID tracking across request lifecycle
- Request/response logging with timing
- Context propagation for background jobs
"""
import logging
import sys
import json
import time
import uuid
from datetime import datetime
from typing import Optional, Any, Dict
from contextvars import ContextVar
from functools import wraps

from app.core.config import settings

# Context variable for correlation ID - thread-safe across async operations
correlation_id_var: ContextVar[Optional[str]] = ContextVar('correlation_id', default=None)
request_context_var: ContextVar[Dict[str, Any]] = ContextVar('request_context', default={})


def get_correlation_id() -> Optional[str]:
    """Get current correlation ID from context."""
    return correlation_id_var.get()


def set_correlation_id(correlation_id: str) -> None:
    """Set correlation ID in context."""
    correlation_id_var.set(correlation_id)


def generate_correlation_id() -> str:
    """Generate a new correlation ID."""
    return str(uuid.uuid4())[:8]  # Short ID for readability


def get_request_context() -> Dict[str, Any]:
    """Get current request context."""
    return request_context_var.get()


def set_request_context(context: Dict[str, Any]) -> None:
    """Set request context."""
    request_context_var.set(context)


class StructuredLogFormatter(logging.Formatter):
    """
    JSON formatter for structured logging.
    Includes correlation ID and request context in every log entry.
    """
    
    def format(self, record: logging.LogRecord) -> str:
        # Base log entry
        log_entry = {
            "timestamp": datetime.utcnow().isoformat() + "Z",
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        
        # Add correlation ID if present
        correlation_id = get_correlation_id()
        if correlation_id:
            log_entry["correlation_id"] = correlation_id
        
        # Add request context if present
        request_context = get_request_context()
        if request_context:
            log_entry["request"] = request_context
        
        # Add exception info if present
        if record.exc_info:
            log_entry["exception"] = self.formatException(record.exc_info)
        
        # Add extra fields from record
        if hasattr(record, 'extra_data'):
            log_entry["data"] = record.extra_data
        
        # Add source location for errors
        if record.levelno >= logging.ERROR:
            log_entry["source"] = {
                "file": record.pathname,
                "line": record.lineno,
                "function": record.funcName,
            }
        
        # Add environment
        log_entry["environment"] = settings.ENVIRONMENT
        
        return json.dumps(log_entry)


class SimpleLogFormatter(logging.Formatter):
    """
    Human-readable formatter for development.
    Includes correlation ID prefix for easy tracing.
    """
    
    def format(self, record: logging.LogRecord) -> str:
        correlation_id = get_correlation_id()
        prefix = f"[{correlation_id}] " if correlation_id else ""
        
        timestamp = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
        return f"{timestamp} - {prefix}{record.levelname} - {record.name} - {record.getMessage()}"


class CorrelatedLogger(logging.LoggerAdapter):
    """
    Logger adapter that automatically includes correlation ID and extra context.
    """
    
    def process(self, msg: str, kwargs: Dict[str, Any]) -> tuple:
        # Add correlation ID to extra
        extra = kwargs.get('extra', {})
        
        correlation_id = get_correlation_id()
        if correlation_id:
            extra['correlation_id'] = correlation_id
        
        # Handle extra_data for structured logging
        if 'data' in kwargs:
            extra['extra_data'] = kwargs.pop('data')
        
        kwargs['extra'] = extra
        return msg, kwargs


def get_logger(name: str) -> CorrelatedLogger:
    """
    Get a logger with correlation ID support.
    
    Usage:
        logger = get_logger(__name__)
        logger.info("Processing order", data={"order_id": 123})
    """
    base_logger = logging.getLogger(name)
    return CorrelatedLogger(base_logger, {})


def configure_logging() -> None:
    """
    Configure application logging based on environment.
    
    - Production: JSON structured logging
    - Development: Human-readable format
    """
    root_logger = logging.getLogger()
    root_logger.setLevel(getattr(logging, settings.LOG_LEVEL))
    
    # Remove existing handlers
    for handler in root_logger.handlers[:]:
        root_logger.removeHandler(handler)
    
    # Create handler
    handler = logging.StreamHandler(sys.stdout)
    handler.setLevel(getattr(logging, settings.LOG_LEVEL))
    
    # Choose formatter based on environment
    if settings.ENVIRONMENT == "production":
        handler.setFormatter(StructuredLogFormatter())
    else:
        handler.setFormatter(SimpleLogFormatter())
    
    root_logger.addHandler(handler)
    
    # Reduce noise from third-party libraries
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
    logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


def log_execution_time(logger: Optional[CorrelatedLogger] = None):
    """
    Decorator to log function execution time.
    
    Usage:
        @log_execution_time()
        def slow_function():
            ...
    """
    def decorator(func):
        @wraps(func)
        async def async_wrapper(*args, **kwargs):
            nonlocal logger
            if logger is None:
                logger = get_logger(func.__module__)
            
            start = time.perf_counter()
            try:
                result = await func(*args, **kwargs)
                duration = (time.perf_counter() - start) * 1000
                logger.debug(f"{func.__name__} completed", data={"duration_ms": round(duration, 2)})
                return result
            except Exception as e:
                duration = (time.perf_counter() - start) * 1000
                logger.error(f"{func.__name__} failed", data={"duration_ms": round(duration, 2), "error": str(e)})
                raise
        
        @wraps(func)
        def sync_wrapper(*args, **kwargs):
            nonlocal logger
            if logger is None:
                logger = get_logger(func.__module__)
            
            start = time.perf_counter()
            try:
                result = func(*args, **kwargs)
                duration = (time.perf_counter() - start) * 1000
                logger.debug(f"{func.__name__} completed", data={"duration_ms": round(duration, 2)})
                return result
            except Exception as e:
                duration = (time.perf_counter() - start) * 1000
                logger.error(f"{func.__name__} failed", data={"duration_ms": round(duration, 2), "error": str(e)})
                raise
        
        import asyncio
        if asyncio.iscoroutinefunction(func):
            return async_wrapper
        return sync_wrapper
    
    return decorator

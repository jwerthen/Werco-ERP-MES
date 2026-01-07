"""
Frontend Error Logging Endpoint

Receives error logs from the frontend for monitoring and debugging.
Stores errors in audit log and can trigger alerts for critical errors.
"""

from fastapi import APIRouter, BackgroundTasks, Request
from pydantic import BaseModel
from typing import List, Optional
from datetime import datetime
import logging

router = APIRouter(prefix="/errors", tags=["errors"])
logger = logging.getLogger(__name__)


class ErrorLogEntry(BaseModel):
    id: str
    message: str
    stack: Optional[str] = None
    componentStack: Optional[str] = None
    boundaryName: Optional[str] = None
    boundaryLevel: Optional[str] = None
    url: str
    timestamp: str
    userAgent: str
    userId: Optional[str] = None
    sessionId: Optional[str] = None
    metadata: Optional[dict] = None


class ErrorLogRequest(BaseModel):
    errors: List[ErrorLogEntry]


class ErrorLogResponse(BaseModel):
    status: str
    count: int


@router.post("/log", response_model=ErrorLogResponse)
async def log_errors(
    request: ErrorLogRequest,
    background_tasks: BackgroundTasks,
    http_request: Request
):
    """
    Log frontend errors for monitoring and debugging.
    
    Errors are processed in the background to avoid blocking the client.
    Critical errors (global boundary level) trigger immediate alerts.
    """
    # Get client IP for additional context
    client_ip = http_request.client.host if http_request.client else "unknown"
    
    # Process errors in background
    background_tasks.add_task(
        process_error_logs, 
        request.errors, 
        client_ip
    )
    
    return ErrorLogResponse(
        status="queued", 
        count=len(request.errors)
    )


async def process_error_logs(errors: List[ErrorLogEntry], client_ip: str):
    """Process and store error logs."""
    for error in errors:
        try:
            # Log to application logger
            log_level = logging.ERROR if error.boundaryLevel == "global" else logging.WARNING
            
            logger.log(
                log_level,
                f"Frontend Error [{error.id}]: {error.message}",
                extra={
                    "error_id": error.id,
                    "boundary_name": error.boundaryName,
                    "boundary_level": error.boundaryLevel,
                    "url": error.url,
                    "user_id": error.userId,
                    "session_id": error.sessionId,
                    "client_ip": client_ip,
                    "user_agent": error.userAgent,
                    "stack": error.stack[:1000] if error.stack else None,
                    "component_stack": error.componentStack[:500] if error.componentStack else None,
                }
            )
            
            # Store in database (audit log)
            await store_error_log(error, client_ip)
            
            # Alert on critical errors
            if error.boundaryLevel == "global":
                await send_error_alert(error)
                
        except Exception as e:
            # Don't let error logging errors crash the system
            logger.exception(f"Failed to process error log: {e}")


async def store_error_log(error: ErrorLogEntry, client_ip: str):
    """
    Store error in database for analysis.
    
    Uses the existing AuditLog model to store frontend errors.
    """
    from app.db.database import SessionLocal
    from app.models.audit_log import AuditLog
    
    try:
        db = SessionLocal()
        
        audit_log = AuditLog(
            action="FRONTEND_ERROR",
            resource_type="frontend",
            resource_identifier=error.id,
            description=f"{error.boundaryName or 'Unknown'}: {error.message[:500]}",
            user_id=int(error.userId) if error.userId and error.userId.isdigit() else None,
            ip_address=client_ip,
            user_agent=error.userAgent[:500] if error.userAgent else None,
            session_id=error.sessionId,
            extra_data={
                "error_id": error.id,
                "boundary_level": error.boundaryLevel,
                "url": error.url,
                "stack": error.stack[:2000] if error.stack else None,
                "component_stack": error.componentStack[:1000] if error.componentStack else None,
                "metadata": error.metadata,
            },
            success="false",
            error_message=error.message[:1000],
        )
        
        db.add(audit_log)
        db.commit()
        db.close()
        
    except Exception as e:
        logger.exception(f"Failed to store error log in database: {e}")


async def send_error_alert(error: ErrorLogEntry):
    """
    Send alert for critical errors.
    
    Could integrate with:
    - Slack
    - Email
    - PagerDuty
    - Sentry
    """
    # Log critical error prominently
    logger.critical(
        f"CRITICAL FRONTEND ERROR [{error.id}]: {error.message}\n"
        f"URL: {error.url}\n"
        f"User: {error.userId or 'anonymous'}\n"
        f"Boundary: {error.boundaryName}"
    )
    
    # TODO: Add integration with alerting service
    # Examples:
    # - await slack_client.send_message(...)
    # - await email_service.send_alert(...)
    # - sentry_sdk.capture_message(...)


@router.get("/health")
async def error_logging_health():
    """Health check for error logging service."""
    return {"status": "healthy", "timestamp": datetime.utcnow().isoformat()}

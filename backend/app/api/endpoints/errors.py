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
    from app.models.user import User
    from app.services.audit_service import AuditService
    
    try:
        db = SessionLocal()
        
        user = None
        if error.userId and error.userId.isdigit():
            user = db.query(User).filter(User.id == int(error.userId)).first()

        AuditService(db, user).log(
            action="FRONTEND_ERROR",
            resource_type="frontend",
            resource_identifier=error.id,
            description=f"{error.boundaryName or 'Unknown'}: {error.message[:500]}",
            success=False,
            error_message=error.message[:1000],
            extra_data={
                "error_id": error.id,
                "boundary_level": error.boundaryLevel,
                "url": error.url,
                "client_ip": client_ip,
                "user_agent": error.userAgent[:500] if error.userAgent else None,
                "session_id": error.sessionId,
                "stack": error.stack[:2000] if error.stack else None,
                "component_stack": error.componentStack[:1000] if error.componentStack else None,
                "metadata": error.metadata,
            }
        )
        db.commit()
        db.close()
        
    except Exception as e:
        logger.exception(f"Failed to store error log in database: {e}")


async def send_error_alert(error: ErrorLogEntry):
    """
    Send alert for critical errors.
    
    Current implementation:
    - Logs critical errors prominently to server logs
    - If Sentry DSN is configured, errors are captured via Sentry
    
    Future integration options (configure via environment variables):
    - Slack: Set SLACK_WEBHOOK_URL for Slack notifications
    - Email: Use SMTP settings for email alerts  
    - PagerDuty: Set PAGERDUTY_API_KEY for incident management
    
    The error boundary information helps identify where in the React
    component tree the error occurred, aiding in debugging.
    """
    from app.core.config import settings
    
    # Log critical error prominently to server logs
    logger.critical(
        f"CRITICAL FRONTEND ERROR [{error.id}]: {error.message}\n"
        f"URL: {error.url}\n"
        f"User: {error.userId or 'anonymous'}\n"
        f"Boundary: {error.boundaryName}"
    )
    
    # If Sentry is configured, capture the error there
    # Sentry integration is already handled in main.py lifespan
    # Critical errors are automatically captured by Sentry's logging integration
    if settings.SENTRY_DSN:
        try:
            import sentry_sdk
            sentry_sdk.capture_message(
                f"Frontend Error [{error.boundaryName}]: {error.message}",
                level="error",
                extras={
                    "error_id": error.id,
                    "url": error.url,
                    "user_id": error.userId,
                    "boundary_name": error.boundaryName,
                    "boundary_level": error.boundaryLevel,
                    "stack": error.stack,
                    "component_stack": error.componentStack,
                }
            )
            logger.info(f"Error {error.id} sent to Sentry")
        except ImportError:
            logger.debug("Sentry SDK not installed, skipping Sentry capture")
        except Exception as e:
            logger.warning(f"Failed to send error to Sentry: {e}")
    
    # NOTE: Additional alerting integrations can be added here as needed:
    #
    # Slack Integration (future):
    # if settings.SLACK_WEBHOOK_URL:
    #     await slack_client.send_webhook(settings.SLACK_WEBHOOK_URL, {
    #         "text": f"🚨 Frontend Error: {error.message}",
    #         "blocks": [...error details...]
    #     })
    #
    # Email Integration (future):
    # if settings.ALERT_EMAIL:
    #     await email_service.send_alert(
    #         to=settings.ALERT_EMAIL,
    #         subject=f"Frontend Error Alert: {error.boundaryName}",
    #         body=f"Error: {error.message}\nURL: {error.url}"
    #     )


@router.get("/health")
async def error_logging_health():
    """Health check for error logging service."""
    return {"status": "healthy", "timestamp": datetime.utcnow().isoformat()}

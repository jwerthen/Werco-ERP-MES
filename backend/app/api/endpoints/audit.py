from typing import List, Optional
from datetime import datetime, timedelta
from fastapi import APIRouter, Depends, Query, HTTPException
from sqlalchemy.orm import Session
from sqlalchemy import desc
from app.db.database import get_db
from app.api.deps import get_current_user, require_role
from app.models.user import User, UserRole
from app.models.audit_log import AuditLog
from app.services.audit_integrity_service import AuditIntegrityService
from pydantic import BaseModel

router = APIRouter()


class AuditLogResponse(BaseModel):
    id: int
    sequence_number: Optional[int] = None
    integrity_hash: Optional[str] = None
    timestamp: datetime
    user_id: Optional[int]
    user_email: Optional[str]
    user_name: Optional[str]
    action: str
    resource_type: str
    resource_id: Optional[int]
    resource_identifier: Optional[str]
    description: Optional[str]
    old_values: Optional[dict]
    new_values: Optional[dict]
    ip_address: Optional[str]
    success: str
    error_message: Optional[str]
    
    class Config:
        from_attributes = True


@router.get("/", response_model=List[AuditLogResponse])
def list_audit_logs(
    action: Optional[str] = None,
    resource_type: Optional[str] = None,
    user_id: Optional[int] = None,
    start_date: Optional[datetime] = None,
    end_date: Optional[datetime] = None,
    search: Optional[str] = None,
    limit: int = Query(default=100, le=500),
    offset: int = 0,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role([UserRole.ADMIN, UserRole.MANAGER]))
):
    """
    List audit logs with filtering.
    Only accessible to Admin and Manager roles for security.
    """
    query = db.query(AuditLog)
    
    if action:
        query = query.filter(AuditLog.action == action)
    
    if resource_type:
        query = query.filter(AuditLog.resource_type == resource_type)
    
    if user_id:
        query = query.filter(AuditLog.user_id == user_id)
    
    if start_date:
        query = query.filter(AuditLog.timestamp >= start_date)
    
    if end_date:
        query = query.filter(AuditLog.timestamp <= end_date)
    
    if search:
        search_term = f"%{search}%"
        query = query.filter(
            (AuditLog.description.ilike(search_term)) |
            (AuditLog.resource_identifier.ilike(search_term)) |
            (AuditLog.user_name.ilike(search_term))
        )
    
    logs = query.order_by(desc(AuditLog.timestamp)).offset(offset).limit(limit).all()
    return logs


@router.get("/summary")
def get_audit_summary(
    days: int = 30,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role([UserRole.ADMIN, UserRole.MANAGER]))
):
    """Get summary of audit activity"""
    from sqlalchemy import func
    
    cutoff = datetime.utcnow() - timedelta(days=days)
    
    # Actions by type
    actions = db.query(
        AuditLog.action,
        func.count(AuditLog.id)
    ).filter(
        AuditLog.timestamp >= cutoff
    ).group_by(AuditLog.action).all()
    
    # Resources modified
    resources = db.query(
        AuditLog.resource_type,
        func.count(AuditLog.id)
    ).filter(
        AuditLog.timestamp >= cutoff
    ).group_by(AuditLog.resource_type).all()
    
    # Active users
    users = db.query(
        AuditLog.user_name,
        func.count(AuditLog.id)
    ).filter(
        AuditLog.timestamp >= cutoff,
        AuditLog.user_name != None
    ).group_by(AuditLog.user_name).order_by(desc(func.count(AuditLog.id))).limit(10).all()
    
    # Failed actions
    failed = db.query(func.count(AuditLog.id)).filter(
        AuditLog.timestamp >= cutoff,
        AuditLog.success == "false"
    ).scalar() or 0
    
    # Total count
    total = db.query(func.count(AuditLog.id)).filter(
        AuditLog.timestamp >= cutoff
    ).scalar() or 0
    
    return {
        "period_days": days,
        "total_events": total,
        "failed_events": failed,
        "by_action": {a: c for a, c in actions},
        "by_resource": {r: c for r, c in resources},
        "top_users": [{"name": u, "count": c} for u, c in users]
    }


@router.get("/actions")
def get_action_types(
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role([UserRole.ADMIN, UserRole.MANAGER]))
):
    """Get distinct action types for filtering"""
    actions = db.query(AuditLog.action).distinct().all()
    return [a[0] for a in actions if a[0]]


@router.get("/resource-types")
def get_resource_types(
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role([UserRole.ADMIN, UserRole.MANAGER]))
):
    """Get distinct resource types for filtering"""
    types = db.query(AuditLog.resource_type).distinct().all()
    return [t[0] for t in types if t[0]]


# =============================================================================
# CMMC Level 2 AU-3.3.8 - Audit Log Integrity Verification
# =============================================================================

@router.get("/integrity/status", 
    summary="Get audit log integrity status",
    description="Quick status check of the audit log integrity chain (CMMC AU-3.3.8)")
def get_integrity_status(
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role([UserRole.ADMIN]))
):
    """
    Get the current status of the audit log integrity chain.
    
    Returns basic statistics without performing full verification.
    Admin access only.
    
    **CMMC Level 2 Control**: AU-3.3.8 - Protect audit information
    """
    service = AuditIntegrityService(db)
    return service.get_chain_status()


@router.get("/integrity/verify",
    summary="Verify audit log integrity",
    description="Full verification of audit log hash chain (CMMC AU-3.3.8)")
def verify_audit_integrity(
    start_sequence: Optional[int] = Query(None, description="Starting sequence number"),
    end_sequence: Optional[int] = Query(None, description="Ending sequence number"),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role([UserRole.ADMIN]))
):
    """
    Perform full verification of the audit log integrity chain.
    
    Checks:
    - Hash chain integrity (each record links to previous)
    - Individual record hashes (content hasn't been modified)
    - Sequence gaps (no records have been deleted)
    
    Admin access only.
    
    **CMMC Level 2 Control**: AU-3.3.8 - Protect audit information
    
    **Warning**: For large audit logs, this operation may take some time.
    Consider using start_sequence/end_sequence to verify specific ranges.
    """
    service = AuditIntegrityService(db)
    report = service.verify_full_chain(
        start_sequence=start_sequence,
        end_sequence=end_sequence
    )
    return report.to_dict()


@router.get("/integrity/verify-recent",
    summary="Verify recent audit logs",
    description="Quick verification of most recent audit entries (CMMC AU-3.3.8)")
def verify_recent_audit_logs(
    count: int = Query(100, ge=1, le=1000, description="Number of recent records to verify"),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role([UserRole.ADMIN]))
):
    """
    Verify the integrity of the most recent audit log entries.
    
    This is a quick health check suitable for regular monitoring.
    For full compliance verification, use /integrity/verify.
    
    Admin access only.
    
    **CMMC Level 2 Control**: AU-3.3.8 - Protect audit information
    """
    service = AuditIntegrityService(db)
    report = service.verify_recent(count=count)
    return report.to_dict()


@router.get("/integrity/record/{sequence_number}",
    summary="Verify single audit record",
    description="Verify integrity of a specific audit log entry")
def verify_single_record(
    sequence_number: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role([UserRole.ADMIN]))
):
    """
    Verify the integrity of a single audit log record.
    
    Returns the record details along with verification status.
    Admin access only.
    """
    record = db.query(AuditLog).filter(
        AuditLog.sequence_number == sequence_number
    ).first()
    
    if not record:
        raise HTTPException(status_code=404, detail="Audit record not found")
    
    service = AuditIntegrityService(db)
    is_valid, issue = service.verify_single_record(record)
    
    # Get previous record for chain verification
    previous = db.query(AuditLog).filter(
        AuditLog.sequence_number == sequence_number - 1
    ).first() if sequence_number > 1 else None
    
    chain_valid, chain_issue = service.verify_chain_link(record, previous)
    
    return {
        "sequence_number": record.sequence_number,
        "id": record.id,
        "timestamp": record.timestamp.isoformat(),
        "action": record.action,
        "resource_type": record.resource_type,
        "resource_identifier": record.resource_identifier,
        "user_email": record.user_email,
        "integrity_hash": record.integrity_hash,
        "previous_hash": record.previous_hash,
        "is_legacy": record.integrity_hash.startswith('LEGACY_') if record.integrity_hash else False,
        "hash_valid": is_valid,
        "chain_valid": chain_valid,
        "issues": [
            issue.__dict__ if issue else None,
            chain_issue.__dict__ if chain_issue else None
        ]
    }


# =============================================================================
# Legacy Helper Function (kept for backward compatibility)
# =============================================================================

# Helper function to create audit log entries
def create_audit_log(
    db: Session,
    user: Optional[User],
    action: str,
    resource_type: str,
    resource_id: Optional[int] = None,
    resource_identifier: Optional[str] = None,
    description: Optional[str] = None,
    old_values: Optional[dict] = None,
    new_values: Optional[dict] = None,
    ip_address: Optional[str] = None,
    success: bool = True,
    error_message: Optional[str] = None,
    extra_data: Optional[dict] = None
):
    """
    Helper to create audit log entries.
    
    NOTE: For new code, use AuditService instead which provides
    proper hash chain integrity for CMMC compliance.
    """
    from app.services.audit_service import AuditService
    
    # Use AuditService for proper integrity tracking
    audit_service = AuditService(db, user)
    return audit_service.log(
        action=action,
        resource_type=resource_type,
        resource_id=resource_id,
        resource_identifier=resource_identifier,
        description=description,
        old_values=old_values,
        new_values=new_values,
        success=success,
        error_message=error_message,
        extra_data=extra_data
    )

from typing import List, Optional
from datetime import datetime, timedelta
from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session
from sqlalchemy import desc
from app.db.database import get_db
from app.api.deps import get_current_user, require_role
from app.models.user import User, UserRole
from app.models.audit_log import AuditLog
from pydantic import BaseModel

router = APIRouter()


class AuditLogResponse(BaseModel):
    id: int
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
    """Helper to create audit log entries"""
    log = AuditLog(
        user_id=user.id if user else None,
        user_email=user.email if user else None,
        user_name=user.full_name if user else None,
        action=action,
        resource_type=resource_type,
        resource_id=resource_id,
        resource_identifier=resource_identifier,
        description=description,
        old_values=old_values,
        new_values=new_values,
        ip_address=ip_address,
        success="true" if success else "false",
        error_message=error_message,
        extra_data=extra_data
    )
    db.add(log)
    db.commit()
    return log

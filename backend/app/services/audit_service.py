"""
Comprehensive Audit Service for AS9100D Compliance.
Provides centralized audit logging for all entity changes.
"""
from typing import Optional, Any, Dict
from datetime import datetime
from sqlalchemy.orm import Session
from sqlalchemy import inspect
from fastapi import Request

from app.models.audit_log import AuditLog
from app.models.user import User
from app.core.logging import get_logger, get_correlation_id

logger = get_logger(__name__)


class AuditService:
    """
    Centralized audit logging service for AS9100D compliance.
    
    Usage:
        audit = AuditService(db, current_user, request)
        audit.log_create("part", part.id, part.part_number, new_values=part_dict)
        audit.log_update("work_order", wo.id, wo.work_order_number, old_values, new_values)
        audit.log_delete("bom", bom.id, bom_identifier)
    """
    
    # Actions that require audit logging
    ACTIONS = {
        "CREATE": "CREATE",
        "UPDATE": "UPDATE",
        "DELETE": "DELETE",
        "VIEW": "VIEW",
        "EXPORT": "EXPORT",
        "IMPORT": "IMPORT",
        "LOGIN": "LOGIN",
        "LOGOUT": "LOGOUT",
        "LOGIN_FAILED": "LOGIN_FAILED",
        "PASSWORD_CHANGE": "PASSWORD_CHANGE",
        "ROLE_CHANGE": "ROLE_CHANGE",
        "STATUS_CHANGE": "STATUS_CHANGE",
        "APPROVE": "APPROVE",
        "REJECT": "REJECT",
        "RELEASE": "RELEASE",
        "COMPLETE": "COMPLETE",
        "CANCEL": "CANCEL",
    }
    
    # Resource types for categorization
    RESOURCE_TYPES = {
        "part": "part",
        "work_order": "work_order",
        "work_order_operation": "work_order_operation",
        "bom": "bom",
        "bom_line": "bom_line",
        "routing": "routing",
        "routing_operation": "routing_operation",
        "user": "user",
        "customer": "customer",
        "vendor": "vendor",
        "purchase_order": "purchase_order",
        "purchase_order_line": "purchase_order_line",
        "receipt": "receipt",
        "inventory": "inventory",
        "quality_record": "quality_record",
        "calibration": "calibration",
        "document": "document",
        "quote": "quote",
        "shipment": "shipment",
        "time_entry": "time_entry",
        "authentication": "authentication",
        "system": "system",
    }
    
    def __init__(
        self,
        db: Session,
        user: Optional[User] = None,
        request: Optional[Request] = None
    ):
        self.db = db
        self.user = user
        self.request = request
        self._ip_address = self._get_ip_address()
        self._user_agent = self._get_user_agent()
    
    def _get_ip_address(self) -> Optional[str]:
        """Extract IP address from request."""
        if not self.request:
            return None
        # Check for forwarded headers (behind proxy)
        forwarded = self.request.headers.get("x-forwarded-for")
        if forwarded:
            return forwarded.split(",")[0].strip()
        if self.request.client:
            return self.request.client.host
        return None
    
    def _get_user_agent(self) -> Optional[str]:
        """Extract user agent from request."""
        if not self.request:
            return None
        return self.request.headers.get("user-agent", "")[:500]
    
    def _serialize_value(self, value: Any) -> Any:
        """Serialize a value for JSON storage."""
        if value is None:
            return None
        if isinstance(value, datetime):
            return value.isoformat()
        if isinstance(value, (int, float, str, bool)):
            return value
        if isinstance(value, (list, tuple)):
            return [self._serialize_value(v) for v in value]
        if isinstance(value, dict):
            return {k: self._serialize_value(v) for k, v in value.items()}
        # For SQLAlchemy models or other objects
        return str(value)
    
    def _model_to_dict(self, model: Any, exclude_fields: set = None) -> Dict:
        """Convert SQLAlchemy model to dictionary."""
        if model is None:
            return {}
        
        exclude = exclude_fields or {"hashed_password", "password"}
        
        if hasattr(model, "__table__"):
            return {
                c.key: self._serialize_value(getattr(model, c.key))
                for c in inspect(model).mapper.column_attrs
                if c.key not in exclude
            }
        elif isinstance(model, dict):
            return {k: self._serialize_value(v) for k, v in model.items() if k not in exclude}
        return {}
    
    def _get_changes(self, old_values: Dict, new_values: Dict) -> Dict:
        """Get only the changed fields between old and new values."""
        changes = {}
        all_keys = set(old_values.keys()) | set(new_values.keys())
        
        for key in all_keys:
            old_val = old_values.get(key)
            new_val = new_values.get(key)
            if old_val != new_val:
                changes[key] = {"old": old_val, "new": new_val}
        
        return changes
    
    def log(
        self,
        action: str,
        resource_type: str,
        resource_id: Optional[int] = None,
        resource_identifier: Optional[str] = None,
        description: Optional[str] = None,
        old_values: Optional[Dict] = None,
        new_values: Optional[Dict] = None,
        success: bool = True,
        error_message: Optional[str] = None,
        extra_data: Optional[Dict] = None
    ) -> AuditLog:
        """Create an audit log entry."""
        try:
            # Include correlation ID for request tracing
            correlation_id = get_correlation_id()
            
            log_entry = AuditLog(
                user_id=self.user.id if self.user else None,
                user_email=self.user.email if self.user else None,
                user_name=getattr(self.user, 'full_name', None) if self.user else None,
                action=action,
                resource_type=resource_type,
                resource_id=resource_id,
                resource_identifier=resource_identifier,
                description=description,
                old_values=old_values,
                new_values=new_values,
                ip_address=self._ip_address,
                user_agent=self._user_agent,
                session_id=correlation_id,  # Store correlation ID for request tracing
                success="true" if success else "false",
                error_message=error_message,
                extra_data=extra_data
            )
            self.db.add(log_entry)
            self.db.flush()  # Don't commit - let the caller handle transaction
            return log_entry
        except Exception as e:
            logger.error(f"Failed to create audit log: {e}")
            # Don't raise - audit logging should not break the main operation
            return None
    
    def log_create(
        self,
        resource_type: str,
        resource_id: int,
        resource_identifier: str,
        new_values: Any = None,
        description: Optional[str] = None,
        extra_data: Optional[Dict] = None
    ) -> AuditLog:
        """Log a CREATE action."""
        new_dict = self._model_to_dict(new_values) if new_values else None
        desc = description or f"Created {resource_type}: {resource_identifier}"
        
        return self.log(
            action=self.ACTIONS["CREATE"],
            resource_type=resource_type,
            resource_id=resource_id,
            resource_identifier=resource_identifier,
            description=desc,
            new_values=new_dict,
            extra_data=extra_data
        )
    
    def log_update(
        self,
        resource_type: str,
        resource_id: int,
        resource_identifier: str,
        old_values: Any = None,
        new_values: Any = None,
        description: Optional[str] = None,
        extra_data: Optional[Dict] = None
    ) -> AuditLog:
        """Log an UPDATE action with change tracking."""
        old_dict = self._model_to_dict(old_values) if old_values else {}
        new_dict = self._model_to_dict(new_values) if new_values else {}
        
        # Calculate changes
        changes = self._get_changes(old_dict, new_dict)
        
        if not changes:
            # No actual changes - skip logging
            return None
        
        desc = description or f"Updated {resource_type}: {resource_identifier}"
        
        return self.log(
            action=self.ACTIONS["UPDATE"],
            resource_type=resource_type,
            resource_id=resource_id,
            resource_identifier=resource_identifier,
            description=desc,
            old_values=old_dict,
            new_values=new_dict,
            extra_data={"changes": changes, **(extra_data or {})}
        )
    
    def log_delete(
        self,
        resource_type: str,
        resource_id: int,
        resource_identifier: str,
        old_values: Any = None,
        description: Optional[str] = None,
        extra_data: Optional[Dict] = None
    ) -> AuditLog:
        """Log a DELETE action."""
        old_dict = self._model_to_dict(old_values) if old_values else None
        desc = description or f"Deleted {resource_type}: {resource_identifier}"
        
        return self.log(
            action=self.ACTIONS["DELETE"],
            resource_type=resource_type,
            resource_id=resource_id,
            resource_identifier=resource_identifier,
            description=desc,
            old_values=old_dict,
            extra_data=extra_data
        )
    
    def log_status_change(
        self,
        resource_type: str,
        resource_id: int,
        resource_identifier: str,
        old_status: str,
        new_status: str,
        description: Optional[str] = None,
        extra_data: Optional[Dict] = None
    ) -> AuditLog:
        """Log a STATUS_CHANGE action."""
        desc = description or f"Changed {resource_type} status: {resource_identifier} from '{old_status}' to '{new_status}'"
        
        return self.log(
            action=self.ACTIONS["STATUS_CHANGE"],
            resource_type=resource_type,
            resource_id=resource_id,
            resource_identifier=resource_identifier,
            description=desc,
            old_values={"status": old_status},
            new_values={"status": new_status},
            extra_data=extra_data
        )
    
    def log_view(
        self,
        resource_type: str,
        resource_id: int,
        resource_identifier: str,
        extra_data: Optional[Dict] = None
    ) -> AuditLog:
        """Log a VIEW action (for sensitive data access tracking)."""
        return self.log(
            action=self.ACTIONS["VIEW"],
            resource_type=resource_type,
            resource_id=resource_id,
            resource_identifier=resource_identifier,
            description=f"Viewed {resource_type}: {resource_identifier}",
            extra_data=extra_data
        )
    
    def log_export(
        self,
        resource_type: str,
        export_format: str,
        record_count: int,
        description: Optional[str] = None,
        extra_data: Optional[Dict] = None
    ) -> AuditLog:
        """Log an EXPORT action."""
        desc = description or f"Exported {record_count} {resource_type} records as {export_format}"
        
        return self.log(
            action=self.ACTIONS["EXPORT"],
            resource_type=resource_type,
            description=desc,
            extra_data={"format": export_format, "count": record_count, **(extra_data or {})}
        )
    
    def log_error(
        self,
        action: str,
        resource_type: str,
        error_message: str,
        resource_id: Optional[int] = None,
        resource_identifier: Optional[str] = None,
        extra_data: Optional[Dict] = None
    ) -> AuditLog:
        """Log a failed action."""
        return self.log(
            action=action,
            resource_type=resource_type,
            resource_id=resource_id,
            resource_identifier=resource_identifier,
            description=f"Failed: {error_message}",
            success=False,
            error_message=error_message,
            extra_data=extra_data
        )


def get_audit_service(
    db: Session,
    user: Optional[User] = None,
    request: Optional[Request] = None
) -> AuditService:
    """Factory function to create AuditService instance."""
    return AuditService(db, user, request)

"""
Optimistic Locking Service for concurrent edit handling.

Implements version-based optimistic locking to prevent lost updates
when multiple users edit the same record simultaneously.
"""
from typing import TypeVar, Type, Dict, Any, Optional
from sqlalchemy.orm import Session
from sqlalchemy import select, update
from fastapi import HTTPException
from datetime import datetime

T = TypeVar('T')


class ConflictDetail:
    """Details about a version conflict."""
    
    def __init__(
        self,
        current_version: int,
        submitted_version: int,
        current_data: Dict[str, Any],
        submitted_changes: Dict[str, Any],
        updated_by: Optional[str] = None,
        updated_at: Optional[datetime] = None
    ):
        self.current_version = current_version
        self.submitted_version = submitted_version
        self.current_data = current_data
        self.submitted_changes = submitted_changes
        self.updated_by = updated_by
        self.updated_at = updated_at
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "current_version": self.current_version,
            "submitted_version": self.submitted_version,
            "current_data": self.current_data,
            "submitted_changes": self.submitted_changes,
            "updated_by": self.updated_by,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
            "message": f"Record was modified by another user. Your version: {self.submitted_version}, Current version: {self.current_version}"
        }


class OptimisticLockService:
    """
    Service for handling optimistic locking operations.
    
    Usage:
        # In your API endpoint:
        result = OptimisticLockService.update_with_lock(
            db=db,
            model_class=Part,
            record_id=part_id,
            submitted_version=update_data.version,
            updates=update_data.dict(exclude_unset=True, exclude={'version'}),
            serialize_fn=lambda p: PartResponse.from_orm(p).dict()
        )
    """
    
    @staticmethod
    def update_with_lock(
        db: Session,
        model_class: Type[T],
        record_id: int,
        submitted_version: int,
        updates: Dict[str, Any],
        serialize_fn: Optional[callable] = None,
        updated_by: Optional[str] = None
    ) -> T:
        """
        Update a record with optimistic locking.
        
        Args:
            db: Database session
            model_class: SQLAlchemy model class
            record_id: Primary key of record to update
            submitted_version: Version from client (must match current)
            updates: Dictionary of field updates
            serialize_fn: Optional function to serialize current data for conflict response
            updated_by: Optional user identifier for conflict message
        
        Returns:
            Updated model instance with incremented version
        
        Raises:
            HTTPException 404: Record not found
            HTTPException 409: Version conflict (record was modified)
        """
        # Get current record
        record = db.query(model_class).filter(model_class.id == record_id).first()
        
        if not record:
            raise HTTPException(
                status_code=404,
                detail=f"{model_class.__name__} with id {record_id} not found"
            )
        
        # Check version
        current_version = getattr(record, 'version', 1)
        
        if current_version != submitted_version:
            # Version mismatch - conflict!
            current_data = {}
            if serialize_fn:
                try:
                    current_data = serialize_fn(record)
                except Exception:
                    # If serialization fails, just use basic fields
                    current_data = {"id": record.id, "version": current_version}
            else:
                current_data = {"id": record.id, "version": current_version}
            
            conflict = ConflictDetail(
                current_version=current_version,
                submitted_version=submitted_version,
                current_data=current_data,
                submitted_changes=updates,
                updated_by=updated_by,
                updated_at=getattr(record, 'updated_at', None)
            )
            
            raise HTTPException(
                status_code=409,
                detail={
                    "error": "CONFLICT",
                    "message": "This record has been modified by another user since you loaded it.",
                    "conflict": conflict.to_dict()
                }
            )
        
        # Version matches - apply updates and increment version
        for key, value in updates.items():
            if hasattr(record, key) and key not in ('id', 'version', 'created_at'):
                setattr(record, key, value)
        
        # Increment version
        record.version = current_version + 1
        record.updated_at = datetime.utcnow()
        
        db.commit()
        db.refresh(record)
        
        return record
    
    @staticmethod
    def update_with_lock_bulk(
        db: Session,
        model_class: Type[T],
        updates: list,  # List of {id, version, ...updates}
        serialize_fn: Optional[callable] = None
    ) -> Dict[str, Any]:
        """
        Bulk update with optimistic locking.
        
        Returns dict with 'updated' list and 'conflicts' list.
        """
        updated = []
        conflicts = []
        
        for update_item in updates:
            record_id = update_item.pop('id')
            submitted_version = update_item.pop('version')
            
            try:
                result = OptimisticLockService.update_with_lock(
                    db=db,
                    model_class=model_class,
                    record_id=record_id,
                    submitted_version=submitted_version,
                    updates=update_item,
                    serialize_fn=serialize_fn
                )
                updated.append(result)
            except HTTPException as e:
                if e.status_code == 409:
                    conflicts.append({
                        "id": record_id,
                        "conflict": e.detail
                    })
                else:
                    raise
        
        return {
            "updated": updated,
            "conflicts": conflicts
        }
    
    @staticmethod
    def get_with_version(
        db: Session,
        model_class: Type[T],
        record_id: int
    ) -> T:
        """
        Get a record ensuring version is included.
        
        Raises:
            HTTPException 404: Record not found
        """
        record = db.query(model_class).filter(model_class.id == record_id).first()
        
        if not record:
            raise HTTPException(
                status_code=404,
                detail=f"{model_class.__name__} with id {record_id} not found"
            )
        
        return record


def create_conflict_response(
    current_version: int,
    submitted_version: int,
    current_data: Dict[str, Any],
    submitted_changes: Dict[str, Any],
    updated_by: Optional[str] = None,
    updated_at: Optional[datetime] = None
) -> Dict[str, Any]:
    """Helper function to create a standardized conflict response."""
    conflict = ConflictDetail(
        current_version=current_version,
        submitted_version=submitted_version,
        current_data=current_data,
        submitted_changes=submitted_changes,
        updated_by=updated_by,
        updated_at=updated_at
    )
    return {
        "error": "CONFLICT",
        "message": "This record has been modified by another user since you loaded it.",
        "conflict": conflict.to_dict()
    }

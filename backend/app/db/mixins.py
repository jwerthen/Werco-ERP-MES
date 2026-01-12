"""
Database mixins for common functionality.
"""
from sqlalchemy import Column, Integer, DateTime, Boolean, String, event
from sqlalchemy.orm import declared_attr
from datetime import datetime


class SoftDeleteMixin:
    """
    Mixin that adds soft delete support.
    
    Usage:
        class MyModel(Base, SoftDeleteMixin):
            __tablename__ = "my_table"
            ...
    
    Records are not physically deleted, but marked with:
    - is_deleted: Boolean flag
    - deleted_at: Timestamp when deleted
    - deleted_by: User ID who deleted (optional)
    
    Use filter(Model.is_deleted == False) in queries to exclude deleted records.
    """
    
    @declared_attr
    def is_deleted(cls):
        return Column(Boolean, nullable=False, default=False, server_default='false', index=True)
    
    @declared_attr
    def deleted_at(cls):
        return Column(DateTime(timezone=True), nullable=True)
    
    @declared_attr
    def deleted_by(cls):
        return Column(Integer, nullable=True)
    
    def soft_delete(self, user_id: int = None):
        """Mark record as deleted."""
        self.is_deleted = True
        self.deleted_at = datetime.utcnow()
        self.deleted_by = user_id
    
    def restore(self):
        """Restore a soft-deleted record."""
        self.is_deleted = False
        self.deleted_at = None
        self.deleted_by = None


class OptimisticLockMixin:
    """
    Mixin that adds optimistic locking support via version column.
    
    Usage:
        class MyModel(Base, OptimisticLockMixin):
            __tablename__ = "my_table"
            ...
    
    The version column auto-increments on each update when using
    OptimisticLockService.update_with_lock()
    """
    
    @declared_attr
    def version(cls):
        return Column(Integer, nullable=False, default=1, server_default='1')
    
    @declared_attr
    def updated_at(cls):
        return Column(
            DateTime(timezone=True),
            nullable=False,
            default=datetime.utcnow,
            onupdate=datetime.utcnow,
            server_default='now()'
        )


class TimestampMixin:
    """
    Mixin that adds created_at and updated_at timestamps.
    """
    
    @declared_attr
    def created_at(cls):
        return Column(
            DateTime(timezone=True),
            nullable=False,
            default=datetime.utcnow,
            server_default='now()'
        )
    
    @declared_attr
    def updated_at(cls):
        return Column(
            DateTime(timezone=True),
            nullable=False,
            default=datetime.utcnow,
            onupdate=datetime.utcnow,
            server_default='now()'
        )
